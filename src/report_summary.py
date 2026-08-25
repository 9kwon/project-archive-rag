"""연차별 성과 정리 문서를 만든다.

우리 기관(config의 project.home_org) 기준으로 연차별 성능지표 목표·실적·달성도와
정량 실적(논문·특허·학술발표)을 한 문서에 모은다. 인수인계와 최종보고서 작성에
바로 쓰는 것을 목표로 한다.

    python src/report_summary.py            # 연차별_성과정리.md 생성
    python src/report_summary.py --all      # 타 기관 지표까지 포함
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from perf_query import connect, outcomes, year_targets
from perf_table import indicator_org
from project import get_profile

OUT = Path("연차별_성과정리.md")
_P = get_profile()
YEARS = _P.years

# 핵심 지표 표시 순서 (config의 project.core_indicators, 비우면 이름순)
CORE = list(_P.core_indicators)


def _short(indicator: str) -> str:
    """긴 지표명에서 괄호 안 핵심만 남긴다.

    문서마다 공백이 달라("감소 효과" / "감소  효과") 같은 지표가 갈리므로
    공백을 하나로 압축한다.
    """
    name = " ".join(indicator.split())
    if "(" in name and ")" in name:
        name = name[name.rfind("(") + 1 : name.rfind(")")]
    return " ".join(name.split())


def indicator_table(con, home_only: bool = True) -> list[dict]:
    """지표별 연차 목표/실적/달성도를 한 행으로 모은다."""
    rows = con.execute(
        "SELECT indicator, unit, proj_year, target, NULL actual, NULL rate, doc "
        "FROM year_target "
        "UNION ALL "
        "SELECT indicator, unit, proj_year, year_target, year_actual, rate, doc "
        "FROM year_actual"
    ).fetchall()

    table: dict[str, dict] = {}
    for r in rows:
        name = _short(r["indicator"])
        if home_only and indicator_org(r["indicator"]) != _P.home_org:
            continue
        e = table.setdefault(name, {"unit": r["unit"] or "", "years": {}})
        y = e["years"].setdefault(r["proj_year"], {"target": "", "actual": "", "rate": None})
        if r["target"] and not y["target"]:
            y["target"] = r["target"]
        if r["actual"]:
            y["actual"] = r["actual"]
        if r["rate"] is not None:
            y["rate"] = r["rate"]
        if r["unit"] and not e["unit"]:
            e["unit"] = r["unit"]

    order = {n: i for i, n in enumerate(CORE)}
    return [
        {"name": n, **v}
        for n, v in sorted(table.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))
    ]


def items_by_year(con) -> dict[int, dict[str, list[sqlite3.Row]]]:
    out: dict[int, dict[str, list]] = {y: {"paper": [], "patent": [], "presentation": []} for y in YEARS}
    for r in con.execute("SELECT * FROM item ORDER BY proj_year, kind, date"):
        y, k = r["proj_year"], r["kind"]
        if y in out and k in out[y]:
            out[y][k].append(r)
    return out


def build(home_only: bool = True) -> str:
    from ingest import load_config

    con = connect(load_config())
    inds = indicator_table(con, home_only)
    items = items_by_year(con)

    L: list[str] = []
    home = _P.home
    scope = f"{home.name}({home.role})" if (home_only and home) else "전체 기관"
    L.append("# 연차별 성과 정리")
    L.append(f"\n대상: **{scope}** / 사업기간 {_P.period}({_P.n_years}개년) / 과제번호 {_P.task_id}")
    L.append("\n원본 보고서의 성과 표에서 자동 추출한 것이다. "
             "빈 칸은 해당 자료에 값이 기재되지 않은 것이다.")

    # ── 1. 성능지표 한눈에 보기 ────────────────────────────
    L.append("\n## 1. 성능지표 목표 및 달성 현황\n")
    head = "| 지표 | 단위 | " + " | ".join(f"{y}차({_P.calendar_year(y)})" for y in YEARS) + " |"
    L.append(head)
    L.append("|" + "---|" * (len(YEARS) + 2))
    for ind in inds:
        cells = []
        for y in YEARS:
            d = ind["years"].get(y, {})
            t, a = d.get("target", ""), d.get("actual", "")
            if t and a:
                cells.append(f"{t} → **{a}**")
            elif t:
                cells.append(f"{t} → –")
            elif a:
                cells.append(f"**{a}**")
            else:
                cells.append("")
        L.append(f"| {ind['name']} | {ind['unit']} | " + " | ".join(cells) + " |")
    L.append("\n표기: `목표 → 실적`. 실적이 굵게 표시된 칸은 해당 연차에 확인된 값이다.")
    L.append("\n원본 표에는 `달성도(%)` 열이 따로 있는데, 이는 **지표 값이 아니라 "
             "목표 대비 달성률**이다(예: 목표 ≥75%에 실적 92.5%이면 달성률 100%). "
             "지표 값과 혼동하지 않도록 이 문서에서는 `[목표 대비 달성률]`로 표기했다.")

    # ── 2. 정량 실적 요약 ─────────────────────────────────
    L.append("\n## 2. 정량 실적 요약\n")
    L.append("| 연차 | 논문 | 특허 | 학술발표 |")
    L.append("|---|---|---|---|")
    for y in YEARS:
        it = items[y]
        L.append(
            f"| {y}차년도({_P.calendar_year(y)}) | {len(it['paper'])}편 | "
            f"{len(it['patent'])}건 | {len(it['presentation'])}건 |"
        )
    tot = {k: sum(len(items[y][k]) for y in YEARS) for k in ("paper", "patent", "presentation")}
    L.append(f"| **합계** | **{tot['paper']}편** | **{tot['patent']}건** | **{tot['presentation']}건** |")

    # 정량적 성과표(보고서 기재 건수)와 비교 — 논문·특허만 신뢰할 수 있다.
    # 그 밖의 항목("기타" 등)은 칸에 산출물 이름이 적혀 있어 건수로 셀 수 없다.
    oc = [o for o in outcomes(con) if any(k in o["category"] for k in ("논문", "특허"))]
    if oc:
        L.append("\n### 보고서 정량적 성과표 기재 건수 (논문·특허)\n")
        L.append("| 연차 | 구분 | 세부 | 건수 | 기관 |")
        L.append("|---|---|---|---|---|")
        for o in oc:
            L.append(
                f"| {o['proj_year']}차 | {o['category']} | {o['subcategory']} | "
                f"{o['count']} | {o['orgs'] or '-'} |"
            )
        L.append("\n위 목록 건수와 이 표의 건수가 다를 수 있다. "
                 "목록은 보고서에 실린 전체 성과이고, 성과표는 과제 기여율이 인정된 건수다. "
                 "**최종보고서 작성 시 확인이 필요하다.** "
                 "성과표의 나머지 항목(제품개발·품목허가·기타 등)은 칸에 산출물 이름이 "
                 "적혀 있어 자동 집계가 불가능하므로 엑셀의 `정량성과표(원본)` 시트를 참고할 것.")

    # ── 3. 연차별 상세 ────────────────────────────────────
    L.append("\n## 3. 연차별 상세\n")
    for y in YEARS:
        L.append(f"### {y}차년도 ({_P.calendar_year(y)}년)\n")

        rows = [(i["name"], i["unit"], i["years"].get(y, {})) for i in inds]
        rows = [(n, u, d) for n, u, d in rows if d.get("target") or d.get("actual")]
        if rows:
            L.append("**성능지표**\n")
            for n, u, d in rows:
                unit = f" {u}" if u and u != "-" else ""
                line = f"- {n}: 목표 {d.get('target') or '미기재'}{unit}"
                line += f" → 실적 {d['actual']}{unit}" if d.get("actual") else " → 실적 자료 없음"
                if d.get("rate") is not None:
                    # 이 값은 지표 자체가 아니라 목표 대비 달성률이다.
                    # "달성도 100%"로만 적으면 정확도가 100%인 것처럼 읽힌다.
                    line += f" [목표 대비 달성률 {d['rate']:.0f}%]"
                L.append(line)
            L.append("")

        it = items[y]
        for kind, label in (("paper", "논문"), ("patent", "특허"), ("presentation", "학술발표")):
            if not it[kind]:
                continue
            L.append(f"**{label} {len(it[kind])}건**\n")
            for r in it[kind]:
                org = f" [{r['org']}]" if r["org"] else ""
                detail = f" — {r['detail']}" if r["detail"] else ""
                who = f" / {r['person']}" if r["person"] else ""
                when = f" / {r['date']}" if r["date"] else ""
                L.append(f"- {r['title']}{detail}{who}{when}{org}")
            L.append("")

        if not rows and not any(it.values()):
            L.append("_해당 연차의 자료가 아직 정리되지 않았다._\n")

    con.close()
    return "\n".join(L)


if __name__ == "__main__":
    text = build(home_only="--all" not in sys.argv)
    OUT.write_text(text, encoding="utf-8")
    print(f"생성: {OUT.resolve()}  ({len(text):,}자)")
    print("\n" + text[:1800])
