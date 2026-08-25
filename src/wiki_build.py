"""Obsidian wiki 자동 생성 페이지 빌더.

과제가 종료되어 내용이 더 이상 변하지 않으므로, RAG를 최종 인터페이스가 아니라
**wiki 생성 파이프라인**으로 쓴다. 매번 검색·생성하는 대신 한 번 정리한 문서를
산출물로 남기고, 수치는 사람이 검증한 뒤 고정한다.

이 스크립트가 만드는 것 (wiki/ 볼트 안):

    자료 색인/   원본 문서 전체의 지도 — 경로 메타데이터(metadata.py)에서 생성
    성과/        perf.sqlite에서 연차별 요약·성능지표 이력·논문/특허/발표 목록

자동 생성 페이지는 frontmatter의 `generated: true`로 표시되며, 재실행 시
그 페이지만 지우고 다시 만든다. **사람이 쓴 페이지는 건드리지 않는다.**

    python src/wiki_build.py
"""

import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import perf_query as pq
from ingest import load_config
from metadata import DocMeta, extract
from project import get_profile

_P = get_profile()

WIKI = Path("wiki")
EXTS = {".pdf", ".hwpx", ".pptx", ".xlsx", ".md", ".html"}
KIND_LABEL = {"paper": "논문", "patent": "특허", "presentation": "학술발표"}


# ── 공통 ─────────────────────────────────────────────────────


def frontmatter(source: str) -> str:
    return (
        "---\n"
        "generated: true\n"
        f"built: {date.today().isoformat()}\n"
        f"source: {source}\n"
        "---\n\n"
        f"> [!warning] 자동 생성 문서 — 직접 수정하지 말 것\n"
        f"> `python src/wiki_build.py` 재실행으로 갱신된다. 근거: {source}\n\n"
    )


def write_page(rel: str, text: str) -> None:
    out = WIKI / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"  생성: {out}")


def clean_generated() -> None:
    """이전에 자동 생성한 페이지만 지운다. 사람이 쓴 페이지는 남긴다."""
    for f in WIKI.rglob("*.md"):
        head = f.read_text(encoding="utf-8", errors="ignore")[:200]
        if head.startswith("---") and "generated: true" in head:
            f.unlink()


def link(path: Path) -> str:
    """원본 파일을 여는 링크. 경로가 바뀌면 이 스크립트를 다시 돌리면 된다."""
    return f"[{path.stem}]({path.resolve().as_uri()})"


def scan() -> list[tuple[Path, DocMeta]]:
    cfg = load_config()
    docs = []
    for root in (Path(p) for p in cfg["paths"]["sources"]):
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in EXTS and not f.name.startswith("~$"):
                docs.append((f, extract(f)))
    return docs


# ── 자료 색인 ─────────────────────────────────────────────────


def page_map(docs: list[tuple[Path, DocMeta]]) -> None:
    by_folder = Counter(f.parts[0] for f, _ in docs)
    by_type = Counter(m.doc_type for _, m in docs)
    by_year = Counter(m.proj_year for _, m in docs)

    L = [frontmatter("폴더 구조 + src/metadata.py"), "# 문서 지도\n"]
    L.append(f"원본 문서 **{len(docs)}개**의 위치와 분포. 상세 목록은 각 색인 페이지에 있다.\n")
    L.append("- [[회의록 색인]] — 날짜·발표자별")
    L.append("- [[성과확인자료 색인]] — 연도·성과번호별 증빙")
    L.append("- [[보고서·분석자료 색인]] — 연차보고서·단계보고서·분석결과")
    L.append("- [[핵심 자료 바로가기]] — 자주 찾는 자료 (사람이 관리)\n")

    L.append("## 폴더별\n\n| 폴더 | 개수 | 내용 |\n|---|---|---|")
    desc = {
        "회의록": f"월간 프로젝트 미팅 발표자료 ({_P.period})",
        "성과확인자료": "논문·특허 등 성과 증빙 (연도별 하위폴더)",
        "과년도 보고서": "연차보고서·단계보고서·컨설팅 발표자료",
        "분석결과": "마지막 연차 작성 재료 (분석 리포트)",
        "docs_raw": "새로 변환한 HWPX 연차보고서를 넣는 곳",
    }
    for folder, n in by_folder.most_common():
        L.append(f"| `{folder}` | {n} | {desc.get(folder, '')} |")

    L.append("\n## 문서 종류별\n\n| 종류 | 개수 |\n|---|---|")
    for t, n in by_type.most_common():
        L.append(f"| {t} | {n} |")

    L.append("\n## 과제 연차별\n\n| 연차 | 연도 | 개수 |\n|---|---|---|")
    for y in sorted(by_year, key=lambda x: (x is None, x)):
        label = f"{y}차년도" if y else "연차 미상"
        cal = f"{_P.calendar_year(y)}" if y else "-"
        L.append(f"| {label} | {cal} | {by_year[y]} |")

    L.append(
        "\n> [!note] 연차 ↔ 연도 매핑\n"
        f"> 1차년도={_P.first_year} … {_P.n_years}차년도={_P.last_year}. "
        "단, 연초(1~3월)의 연차컨설팅·단계평가는\n"
        "> **전년도 실적 보고**다 (예: 연초 컨설팅 자료는 직전 연차 실적)."
    )
    write_page("자료 색인/문서 지도.md", "\n".join(L))


def page_meetings(docs: list[tuple[Path, DocMeta]]) -> None:
    rows = [(f, m) for f, m in docs if f.parts[0] == "회의록"]
    by_year: dict[int, list] = defaultdict(list)
    for f, m in rows:
        by_year[m.calendar_year or 0].append((f, m))

    L = [frontmatter("회의록/ 폴더 파일명·경로"), "# 회의록 색인\n"]
    L.append(f"총 {len(rows)}건. \"언제 무슨 얘기가 나왔나\"는 위키에 다 정리할 수 없으므로,")
    L.append("내용 검색은 RAG를 쓴다: `python src/generate.py` (인수인계 → [[시스템 사용법]]).\n")
    for year in sorted(by_year, reverse=True):
        items = sorted(by_year[year], key=lambda t: t[1].date, reverse=True)
        L.append(f"\n## {year or '연도 미상'}년 ({len(items)}건)\n")
        L.append("| 날짜 | 발표자 | 문서 | 경로 |\n|---|---|---|---|")
        for f, m in items:
            L.append(f"| {m.date or '-'} | {m.presenter or '-'} | {link(f)} | `{f}` |")
    write_page("자료 색인/회의록 색인.md", "\n".join(L))


def page_achievements(docs: list[tuple[Path, DocMeta]]) -> None:
    rows = [(f, m) for f, m in docs if f.parts[0] == "성과확인자료"]
    by_year: dict[int, list] = defaultdict(list)
    for f, m in rows:
        by_year[m.calendar_year or 0].append((f, m))

    L = [frontmatter("성과확인자료/ 폴더 파일명·경로"), "# 성과확인자료 색인\n"]
    L.append(f"총 {len(rows)}건. 논문·특허 실적의 증빙 문서. 건수 검증은 [[정량 성과 총괄]]과 대조할 것.\n")
    for year in sorted(by_year, reverse=True):

        def seq(t):
            tags = dict(x.split(":", 1) for x in t[1].tags if ":" in x)
            return tags.get("성과번호", "99")

        items = sorted(by_year[year], key=seq)
        L.append(f"\n## {year or '연도 미상'}년 ({len(items)}건)\n")
        L.append("| 번호 | 기관 | 제목 | 경로 |\n|---|---|---|---|")
        for f, m in items:
            tags = dict(x.split(":", 1) for x in m.tags if ":" in x)
            L.append(
                f"| {tags.get('성과번호', '-')} | {m.org or '-'} | {link(f)} | `{f}` |"
            )
    write_page("자료 색인/성과확인자료 색인.md", "\n".join(L))


def page_reports(docs: list[tuple[Path, DocMeta]]) -> None:
    rows = [(f, m) for f, m in docs if f.parts[0] not in ("회의록", "성과확인자료")]
    L = [frontmatter("보고서·분석결과·docs_raw 폴더의 파일명·경로"), "# 보고서·분석자료 색인\n"]
    L.append("연차보고서가 **성능지표·성과 수치의 원본**이다. 수치 확인은 항상 여기서 시작할 것.\n")
    L.append("| 연차 | 종류 | 문서 | 경로 |\n|---|---|---|---|")
    for f, m in sorted(rows, key=lambda t: (t[1].proj_year or 9, str(t[0]))):
        year = f"{m.proj_year}차" if m.proj_year else ("1~3차" if "1-3차년도" in m.tags else "-")
        note = " ※전년도 실적 보고" if "전년도 실적 보고" in m.tags else ""
        L.append(f"| {year}{note} | {m.doc_type} | {link(f)} | `{f}` |")
    L.append(
        "\n> [!note] 아직 작성되지 않은 빈 양식 보고서는 색인에서 제외되어 있다\n"
        "> (config의 project.exclude_docs). 최종 작성 시 해당 연차 수행내용 초안을 쓸 것."
    )
    write_page("자료 색인/보고서·분석자료 색인.md", "\n".join(L))


# ── 성과 ─────────────────────────────────────────────────────


def _year_sources(con: sqlite3.Connection, year: int) -> list[str]:
    sql = " UNION ".join(
        f"SELECT DISTINCT doc FROM {t} WHERE proj_year = ?"
        for t in ("year_target", "year_actual", "quant_outcome", "achievement")
    )
    return sorted(r[0] for r in con.execute(sql, [year] * 4) if r[0])


def pages_year_summary(con: sqlite3.Connection) -> None:
    for y in _P.years:
        body = pq.format_summary(pq.year_summary(con, y))
        L = [frontmatter("storage/perf.sqlite (src/perf_table.py가 성과 표에서 추출)")]
        L.append(body)
        if srcs := _year_sources(con, y):
            L.append("\n### 근거 문서\n")
            L += [f"- {s}" for s in srcs]
        L.append(f"\n관련: [[성능지표 이력]] · [[정량 성과 총괄]] · [[{y}차년도 수행내용]]")
        write_page(f"성과/{y}차년도 성과 요약.md", "\n".join(L))


def page_indicators(con: sqlite3.Connection) -> None:
    L = [frontmatter("storage/perf.sqlite year_target·year_actual"), "# 성능지표 이력\n"]
    L.append("지표별 연차 목표·실적. `달성률`은 지표 값이 아니라 **목표 대비 달성률**이다.\n")

    # 같은 지표가 문서마다 띄어쓰기만 다르게 적혀 있다("플랫폼 (…" vs "플랫폼(…").
    # 공백을 뺀 이름이 같으면 하나로 합치고, 연차별로 실적이 있는 행을 우선한다.
    groups: dict[str, list[str]] = defaultdict(list)
    for name in pq.indicator_names(con):
        groups[re.sub(r"\s+", "", name)].append(name)  # NBSP 등 모든 공백 제거

    for variants in groups.values():
        by_year: dict[int, dict] = {}
        for name in variants:
            for r in pq.indicator_history(con, name):
                cur = by_year.get(r["proj_year"])
                if cur is None or (r["actual"] and not cur["actual"]):
                    merged = dict(r)
                    if cur:
                        merged["sources"] = sorted(set(cur["sources"]) | set(r["sources"]))
                        merged["target"] = merged["target"] or cur["target"]
                    by_year[r["proj_year"]] = merged
                else:
                    cur["sources"] = sorted(set(cur["sources"]) | set(r["sources"]))
        hist = [by_year[y] for y in sorted(by_year)]
        display = re.sub(r"\s+", " ", max(variants, key=len))
        L.append("\n" + pq.format_indicator(display, hist))
        srcs = sorted({s for r in hist for s in r["sources"]})
        if srcs:
            L.append(f"  - 출처: {', '.join(srcs)}")
    write_page("성과/성능지표 이력.md", "\n".join(L))


def page_outcomes(con: sqlite3.Connection) -> None:
    L = [frontmatter("storage/perf.sqlite quant_outcome"), "# 정량 성과 총괄\n"]
    outs = pq.outcomes(con)
    countable = [o for o in outs if any(k in o["category"] for k in ("논문", "특허"))]
    cats = sorted({(o["category"], o["subcategory"] or "") for o in countable})
    L.append("성과표에 기재된 **과제 기여율 인정 건수**다. 게재·발표 전체 목록([[논문·특허·학술발표 목록]])과\n건수가 다를 수 있다 — 최종보고서 작성 시 확인이 필요하다.\n")
    L.append("| 구분 | 세부 | " + " | ".join(f"{y}차" for y in _P.years) + " | 계 |")
    L.append("|---|---|" + "---|" * (len(_P.years) + 1))
    for cat, sub in cats:
        row, total = [], 0
        for y in _P.years:
            o = next(
                (o for o in countable
                 if o["category"] == cat and (o["subcategory"] or "") == sub and o["proj_year"] == y),
                None,
            )
            if o:
                total += o["count"]
                mark = " ※" if o["conflict"] else ""
                row.append(f"{o['count']}{mark}")
            else:
                row.append("")
        L.append(f"| {cat} | {sub} | " + " | ".join(row) + f" | **{total}** |")
    L.append("\n※ 표시는 문서 간 값이 달라 우선순위 문서 값을 쓴 칸이다(perf_query.DOC_RANK).")
    L.append(
        "\n> [!warning] 제품개발·품목허가·기타 항목은 칸에 산출물 이름이 적혀 있어 자동 집계가\n"
        "> 불가능하다. `성과정리.xlsx`의 `정량성과표(원본)` 시트를 사람이 직접 확인할 것."
    )
    write_page("성과/정량 성과 총괄.md", "\n".join(L))


def page_items(con: sqlite3.Connection) -> None:
    L = [frontmatter("storage/perf.sqlite item (보고서 성과 목록에서 추출)"),
         "# 논문·특허·학술발표 목록\n"]
    L.append("보고서에 실린 **전체 목록**이다. 성과표의 기여율 인정 건수([[정량 성과 총괄]])와 다를 수 있다.\n")
    rows = con.execute("SELECT * FROM item ORDER BY kind, proj_year, date").fetchall()
    by_kind: dict[str, list] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)
    for kind in ("paper", "patent", "presentation"):
        items = by_kind.pop(kind, [])
        if not items:
            continue
        L.append(f"\n## {KIND_LABEL.get(kind, kind)} ({len(items)}건)\n")
        L.append("| 연차 | 제목 | 게재지/번호/학회 | 저자·발표자 | 날짜 |\n|---|---|---|---|---|")
        for r in items:
            L.append(
                f"| {r['proj_year'] or '-'}차 | {r['title']} | {r['detail'] or '-'} "
                f"| {r['person'] or '-'} | {r['date'] or '-'} |"
            )
    for kind, items in by_kind.items():  # 예상 못 한 종류가 생겨도 누락시키지 않는다
        L.append(f"\n## {kind} ({len(items)}건) — 분류 확인 필요")
    write_page("성과/논문·특허·학술발표 목록.md", "\n".join(L))


if __name__ == "__main__":
    WIKI.mkdir(exist_ok=True)
    clean_generated()

    docs = scan()
    print(f"원본 문서 {len(docs)}개 스캔")
    page_map(docs)
    page_meetings(docs)
    page_achievements(docs)
    page_reports(docs)

    con = pq.connect(load_config())
    pages_year_summary(con)
    page_indicators(con)
    page_outcomes(con)
    page_items(con)
    con.close()

    print("\n완료. Obsidian에서 wiki/ 폴더를 볼트로 열면 된다.")
