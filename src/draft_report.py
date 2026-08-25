"""연차보고서 초안 생성.

목차 항목마다 근거를 검색해 초안을 만든다. 보고서 전체를 한 번에 쓰지 않고
**목차 단위로 검색 → 근거 확인 → 초안 생성**을 반복한다.

마지막 연차를 기본 대상으로 한다. 목표는 직전 연차 보고서의 "다음 연도
연구개발계획"에 적힌 것을 쓰고, 실적은 그 해의 회의록·분석결과·성과자료에서 찾는다.

    python src/draft_report.py            # 마지막 연차 전체 목차
    python src/draft_report.py -y 5 -s 2  # 특정 연차·목차만
    python src/draft_report.py --dry       # LLM 없이 근거만 수집(빠름)

목차 구성은 국가 R&D 연차보고서 표준 양식을 따른다. 각 목차의 검색어는
일반적인 기본값을 쓰되, config의 project.draft_queries로 과제의 핵심 기술
용어로 교체할 수 있다 (교체하면 근거 회수율이 오른다).
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from generate import _build_context, load_llm
from project import get_profile
from search import Hit, get_searcher

_P = get_profile()
OUT_TEMPLATE = "{year}차년도_보고서_초안.md"


@dataclass
class Section:
    id: str
    title: str
    queries: list[str]  # 근거를 찾을 검색어들 (config draft_queries로 교체 가능)
    guide: str  # LLM에 주는 작성 지침
    prefer_year: bool = True  # 해당 연차 자료를 우선할지
    doc_types: list[str] = field(default_factory=list)  # 특정 문서 종류로 제한
    perf: bool = False  # 성과 DB 요약을 함께 넣을지


SECTIONS = [
    Section(
        id="1",
        title="1. 연구개발과제의 개요",
        queries=["연구개발 목표 개요", "과제 필요성 배경", "최종목표 세부목표"],
        guide=(
            "과제의 배경·필요성과 최종목표, 해당 연차의 목표를 간결히 정리하라. "
            "이전 연차 보고서에 이미 서술된 내용이 있으면 그대로 인용하되 연차만 맞춰라."
        ),
        prefer_year=False,
        doc_types=["연차보고서", "단계보고서"],
    ),
    Section(
        id="2",
        title="2. 연구개발과제의 수행 과정 및 수행 내용",
        queries=[
            # 일반 기본값 — config의 project.draft_queries["2"]로
            # 과제의 핵심 기술 용어("OO 알고리즘 고도화" 등)를 넣으면 회수율이 오른다
            "실증 데이터 수집 참여자 모집",
            "핵심 기술 개발 고도화",
            "알고리즘 성능 개선",
            "효과 검증 실험 결과",
        ],
        guide=(
            "해당 연차에 **실제로 수행한 일**을 개조식(○, -, >)으로 정리하라. "
            "계획이 아니라 수행 결과다. 실험 참여 인원, 분석 방법, 도출된 수치를 "
            "근거에서 찾아 포함하라. 근거에 없는 수치는 쓰지 마라."
        ),
    ),
    Section(
        id="3-1",
        title="3. 수행 결과 및 목표 달성 정도 — 1) 정성적 연구개발성과",
        queries=[
            "연구개발 결과 요약 달성",
            "알고리즘 성능 검증 결과",
            "실증 실험 완료",
        ],
        guide=(
            "목표 대비 무엇을 달성했는지 개조식으로 정리하라. "
            "각 항목은 '목표 → 수행 결과' 형태로 대응시켜라. "
            "달성하지 못한 항목이 근거에 보이면 숨기지 말고 사유와 함께 적어라."
        ),
        perf=True,
    ),
    Section(
        id="3-2",
        title="3. 수행 결과 및 목표 달성 정도 — 2) 정량적 연구개발성과",
        queries=["논문 게재 학술지", "특허 출원 등록", "학술대회 발표"],
        guide=(
            "논문·특허·학술발표 실적을 표로 정리하라. 제공된 정형 데이터의 건수를 "
            "그대로 쓰고, 개별 항목은 근거에 있는 것만 나열하라. 추정하지 마라."
        ),
        perf=True,
    ),
    Section(
        id="4",
        title="4. 연구개발성과의 활용 방안 및 기대효과",
        queries=["활용방안 기대효과", "사업화 계획", "기술 이전 확산"],
        guide=(
            "연구 성과를 어디에 어떻게 쓸 수 있는지 정리하라. "
            "이전 연차 보고서의 활용방안을 참고하되 해당 연차 성과를 반영하라."
        ),
        prefer_year=False,
    ),
]

SYSTEM_PROMPT = """당신은 국가 R&D 연차보고서 초안을 작성하는 조수다.

작성 규칙:
1. 제공된 근거 자료에 있는 내용만 쓴다. 없는 사실·수치는 절대 만들지 않는다.
   근거가 부족한 부분은 "[확인 필요: …]"로 표시해 작성자가 채우게 한다.
2. 국가연구개발 보고서 문체를 쓴다 — 개조식(○, -, >), 명사형 종결("~함", "~수행함").
3. 수치를 쓸 때는 근거 번호 [n]을 붙인다.
4. 계획과 실적을 구분한다. 이 문서는 실적 보고다.
   목표에 적힌 수치(예: "500명 수집")를 실적으로 옮겨 적지 마라.
   실제 수행 결과가 근거에 없으면 "[확인 필요: 실적 미확인]"으로 남긴다.
5. 정형 데이터(성과 표)가 제공되면 수치는 그 값을 우선한다.
6. **근거마다 붙은 연차 표시를 확인하라.** 다른 연차의 수치를 이번 연차 실적으로
   쓰면 안 된다. 근거가 이전 연차 것이면 "(N차년도 자료)"라고 밝히고 인용하라.
7. 초안이므로 사람의 검토가 전제다. 확신할 수 없는 부분은 명시적으로 표시하라."""


def collect_evidence(section: Section, year: int, k: int = 6) -> list[Hit]:
    """목차 항목에 필요한 근거를 모은다. 검색어별로 나눠 찾고 중복을 없앤다."""
    searcher = get_searcher()
    seen: set[str] = set()
    hits: list[Hit] = []

    # config에 이 목차의 검색어가 정의돼 있으면 그것을 쓴다
    queries = _P.draft_queries.get(section.id) or section.queries

    for q in queries:
        filters: dict = {}
        if section.prefer_year:
            filters["proj_year"] = year
        if len(section.doc_types) == 1:
            filters["doc_type"] = section.doc_types[0]

        for h in searcher.search(q, k=k, filters=filters or None):
            key = h.text[:200]
            if key in seen:
                continue
            if section.doc_types and h.meta.get("doc_type") not in section.doc_types:
                continue
            seen.add(key)
            hits.append(h)

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[: k * 2]


def _plan_text(year: int) -> str:
    """이전 연차 보고서에 적힌 '다음 연도 계획' — 이번 연차의 목표다."""
    hits = get_searcher().search(
        "다음 연도 연구개발계획 연구개발 목표 및 내용",
        k=3,
        filters={"proj_year": year - 1, "doc_type": "연차보고서"},
    )
    if not hits:
        return ""
    return "\n\n".join(h.text for h in hits[:2])


def draft_section(section: Section, year: int, dry: bool = False) -> tuple[str, list[Hit]]:
    hits = collect_evidence(section, year)
    if not hits:
        return "_근거를 찾지 못했다. 자료를 추가하거나 검색어를 조정할 것._", []
    if dry:
        return "", hits

    parts = [f"작성 대상: {year}차년도({_P.calendar_year(year)}년) 연차보고서 — {section.title}"]

    if plan := _plan_text(year):
        parts.append(
            f"이번 연차의 목표 (이전 연차 보고서의 '다음 연도 계획'):\n\n{plan}"
        )

    if section.perf:
        import perf_query as pq

        try:
            con = pq.connect(get_searcher().cfg)
            parts.append(
                "성과 표에서 추출한 정형 데이터 (수치는 이 값을 우선할 것):\n\n"
                + pq.format_summary(pq.year_summary(con, year))
            )
            con.close()
        except FileNotFoundError:
            pass

    parts.append("근거 자료:\n\n" + _build_context(hits))
    parts.append(f"작성 지침: {section.guide}")
    parts.append(f"위 내용을 바탕으로 '{section.title}' 항목의 초안을 작성하라.")

    llm = load_llm(get_searcher().cfg)
    return llm.chat(SYSTEM_PROMPT, "\n\n".join(parts)), hits


def build(year: int, only: str | None = None, dry: bool = False) -> str:
    targets = [s for s in SECTIONS if only is None or s.id == only]

    L = [f"# {year}차년도({_P.calendar_year(year)}년) 연차보고서 초안"]
    L.append(
        "\n> 자동 생성된 초안이다. 모든 문장은 사람이 검토해야 한다.\n"
        "> `[확인 필요]` 표시는 근거가 부족해 작성자가 채워야 하는 부분이다.\n"
        "> 각 항목 끝에 사용된 근거의 출처를 붙였다."
    )

    for s in targets:
        print(f"  작성 중: {s.title}", file=sys.stderr)
        text, hits = draft_section(s, year, dry)

        L.append(f"\n---\n\n## {s.title}\n")
        if not dry:
            L.append(text)
        if hits:
            L.append("\n<details><summary>근거 " + str(len(hits)) + "건</summary>\n")
            for i, h in enumerate(hits, 1):
                L.append(f"{i}. {h.source()} — {h.meta.get('doc_type', '')}")
            L.append("\n</details>")

    return "\n".join(L)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("-y", "--year", type=int, default=_P.n_years, help="대상 연차 (기본: 마지막 연차)")
    ap.add_argument("-s", "--section", help="특정 목차만 (예: 2, 3-1)")
    ap.add_argument("--dry", action="store_true", help="LLM 없이 근거만 수집")
    ap.add_argument("--list", action="store_true", help="목차 목록만 보기")
    args = ap.parse_args()

    if args.list:
        for s in SECTIONS:
            print(f"  {s.id:5s} {s.title}")
        sys.exit(0)

    text = build(args.year, args.section, args.dry)
    out = Path(OUT_TEMPLATE.format(year=args.year))
    out.write_text(text, encoding="utf-8")
    print(f"\n생성: {out.resolve()}  ({len(text):,}자)", file=sys.stderr)
    print(text)
