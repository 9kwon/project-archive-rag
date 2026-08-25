"""과제 프로필 — 과제 고유 정보의 단일 진실원천.

이 저장소의 코드는 특정 과제에 종속되지 않는다. 과제번호·참여기관·연차 구조·
지표 소관처럼 과제마다 다른 값은 전부 config.yaml의 `project` 섹션에서 읽는다.
새 과제에 적용할 때 고치는 것은 config뿐이며, 코드에는 어떤 과제의 이름도
하드코딩되지 않는다.

config.yaml이 없으면 config.example.yaml의 placeholder 프로필로 동작한다
(데이터 없이도 코드를 읽고 실행해볼 수 있게).
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent

# config에 project 섹션이 없을 때 쓰는 placeholder 프로필.
# config.example.yaml의 project 섹션과 같은 내용이다.
_DEFAULTS: dict = {
    "id": "RS-0000-XX000000",
    "title": "OO 기술개발 연구과제",
    "first_year": 2022,
    "n_years": 5,
    "phases": [
        {"name": "1단계", "years": [1, 2, 3]},
        {"name": "2단계", "years": [4, 5]},
    ],
    "organizations": {
        "ORG_MAIN": {"role": "주관연구개발기관", "name": "주관기업(주)", "aliases": ["주관기업"]},
        "ORG_HOME": {"role": "공동연구개발기관", "name": "OO연구원", "aliases": ["OO연구원"]},
        "ORG_SUB": {"role": "위탁연구개발기관", "name": "OO병원", "aliases": ["OO병원"]},
    },
    "home_org": "ORG_HOME",
    "indicator_owner": {"ORG_HOME": ["지표 키워드 A", "지표 키워드 B"]},
    "indicator_default": "ORG_MAIN",
    "core_indicators": [],
    "exclude_docs": [],
    "doc_weights": {
        "연차보고서": 1.35,
        "단계보고서": 1.30,
        "성과확인서": 1.20,
        "성과내역서": 1.20,
        "계획서": 1.10,
        "발표자료": 1.00,
        "분석결과": 1.00,
        "성과확인자료": 0.95,
        "회의록": 0.85,
    },
    "draft_queries": {},
}


@dataclass(frozen=True)
class Org:
    code: str  # 짧은 코드 (예: ORG_HOME) — 메타데이터·필터·정렬에 쓴다
    role: str  # 주관 / 공동 / 위탁 연구개발기관
    name: str  # 표시용 이름
    aliases: tuple[str, ...]  # 문서 본문에서 이 기관을 알아볼 문자열들


@dataclass(frozen=True)
class Profile:
    task_id: str
    title: str
    first_year: int
    n_years: int
    phases: tuple[dict, ...]
    orgs: tuple[Org, ...]
    home_org: str  # "우리 기관" 코드 — 성과 정리·기본 필터의 대상
    indicator_owner: dict  # {기관코드: (지표명 부분문자열, …)}
    indicator_default: str  # 어느 목록에도 안 걸린 지표의 담당 기관
    core_indicators: tuple[str, ...]  # 성과 문서에서 앞세울 지표 순서 (선택)
    exclude_docs: tuple[str, ...]  # 색인에서 뺄 문서명 부분문자열
    doc_weights: dict  # 문서 종류별 검색 권위도
    draft_queries: dict  # {목차 id: [검색어]} — 보고서 초안 검색어 교체용

    # ── 연차 ↔ 연도 ────────────────────────────────────────
    @property
    def years(self) -> range:
        return range(1, self.n_years + 1)

    @property
    def last_year(self) -> int:
        return self.first_year + self.n_years - 1

    @property
    def period(self) -> str:
        return f"{self.first_year}~{self.last_year}"

    def calendar_year(self, proj_year: int) -> int:
        return self.first_year + proj_year - 1

    def project_year(self, calendar_year: int | None) -> int | None:
        if calendar_year is None:
            return None
        y = calendar_year - self.first_year + 1
        return y if 1 <= y <= self.n_years else None

    def year_label(self, proj_year: int) -> str:
        return f"{proj_year}차년도 ({self.calendar_year(proj_year)})"

    # ── 기관 ───────────────────────────────────────────────
    @property
    def org_codes(self) -> list[str]:
        return [o.code for o in self.orgs]

    def org(self, code: str) -> Org | None:
        return next((o for o in self.orgs if o.code == code), None)

    @property
    def home(self) -> Org | None:
        return self.org(self.home_org)

    def org_of_text(self, text: str) -> str:
        """본문·파일명에서 기관을 알아본다. 못 찾으면 빈 문자열."""
        for o in self.orgs:
            if any(a and a in text for a in (*o.aliases, o.code)):
                return o.code
        return ""

    # ── 성능지표 소관 ──────────────────────────────────────
    def indicator_org(self, indicator: str) -> str:
        for code, keys in self.indicator_owner.items():
            if any(k in indicator for k in keys):
                return code
        return self.indicator_default

    # ── LLM 프롬프트용 과제 구조 블록 ──────────────────────
    def prompt_context(self) -> str:
        """생성 프롬프트에 주입하는 과제 구조 설명.

        표의 열 이름("2단계 1차연도(2025)")과 질문의 표현("4차년도")이 어긋나면
        LLM이 엉뚱한 열(누적 "계")을 집는다. 연차↔연도↔단계 매핑을 명시해 막는다.
        """
        lines = ["과제 구조 (자료를 읽을 때 반드시 참고할 것):"]
        years = ", ".join(f"{y}차년도={self.calendar_year(y)}" for y in self.years)
        lines.append(f"- 연차와 연도: {years}")

        for ph in self.phases:
            ys = sorted(ph["years"])
            cal = f"{self.calendar_year(ys[0])}~{self.calendar_year(ys[-1])}"
            span = f"{ys[0]}~{ys[-1]}차년도" if len(ys) > 1 else f"{ys[0]}차년도"
            lines.append(f"- {ph['name']} = {cal} ({span})")
        if self.phases:
            lines.append(
                '- 표의 "N단계 M차연도(YYYY)" 열은 괄호 안 연도로 과제 연차를 정한다.'
            )
        lines.append(
            '- 표의 "계" 열은 전체 기간 누적값이다. 특정 연차를 물으면 누적값이 아니라 '
            "그 연차 열의 값을 읽어야 한다."
        )
        if self.orgs:
            orgs = ", ".join(f"{o.role.replace('연구개발기관', '')} {o.name}" for o in self.orgs)
            lines.append(f"- 참여기관: {orgs}. 질문이 특정 기관 소관 사항이면 그 점을 밝힌다.")
        return "\n".join(lines)


def _find_config() -> Path | None:
    for base in (Path.cwd(), _ROOT):
        for name in ("config.yaml", "config.example.yaml"):
            p = base / name
            if p.exists():
                return p
    return None


def profile_from(cfg: dict | None) -> Profile:
    raw = {**_DEFAULTS, **((cfg or {}).get("project") or {})}
    orgs = tuple(
        Org(code=c, role=v.get("role", ""), name=v.get("name", c),
            aliases=tuple(v.get("aliases", ())))
        for c, v in (raw["organizations"] or {}).items()
    )
    return Profile(
        task_id=raw["id"],
        title=raw["title"],
        first_year=int(raw["first_year"]),
        n_years=int(raw["n_years"]),
        phases=tuple(raw["phases"] or ()),
        orgs=orgs,
        home_org=raw["home_org"],
        indicator_owner={c: tuple(v) for c, v in (raw["indicator_owner"] or {}).items()},
        indicator_default=raw["indicator_default"],
        core_indicators=tuple(raw["core_indicators"] or ()),
        exclude_docs=tuple(raw["exclude_docs"] or ()),
        doc_weights=dict(raw["doc_weights"] or {}),
        draft_queries=dict(raw["draft_queries"] or {}),
    )


@lru_cache(maxsize=1)
def get_profile() -> Profile:
    path = _find_config()
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    return profile_from(cfg)


if __name__ == "__main__":
    p = get_profile()
    print(f"과제   : {p.title} ({p.task_id})")
    print(f"기간   : {p.period} · {p.n_years}개년")
    print(f"기관   : " + ", ".join(f"{o.code}={o.name}[{o.role}]" for o in p.orgs))
    print(f"홈 기관: {p.home_org}")
    print()
    print(p.prompt_context())
