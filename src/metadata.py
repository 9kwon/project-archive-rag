"""파일 경로와 이름에서 메타데이터를 뽑는다.

자료가 `회의록\\2023\\20231129\\프로젝트 미팅_20231129_홍길동.pptx` 처럼
폴더 구조와 파일명에 이미 연도·종류·날짜·발표자를 담고 있다.
이를 검색 필터로 쓸 수 있게 구조화한다.

과제 기간·참여기관은 config의 project 섹션에서 읽는다 (src/project.py).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from project import get_profile


def project_year(calendar_year: int | None) -> int | None:
    return get_profile().project_year(calendar_year)


# 파일명·경로에 나타나는 문서 종류. 위에서부터 먼저 맞는 것을 쓴다.
_DOC_TYPES = [
    ("연차보고서", ("연차보고서",)),
    ("단계보고서", ("단계보고서",)),
    ("성과확인서", ("성과확인서",)),
    ("성과내역서", ("성과내역서",)),
    ("발표자료", ("발표자료", "컨설팅")),
    ("계획서", ("연구개발계획서", "계획서")),
    ("회의록", ("회의록",)),
    ("분석결과", ("ANALYSIS_REPORT", "analysis_report", "_결과")),
]

_YYYYMMDD = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")
_YYMMDD = re.compile(r"(?<!\d)(2[0-9])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_YYYYMM = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)")  # "202601"
_NTH_YEAR = re.compile(r"([1-9])차\s?년도")
# "2026", "2026_결과" 처럼 연도로 시작하는 폴더
_YEAR_FOLDER = re.compile(r"^(20\d{2})(?:\D|$)")
_MMDD_FOLDER = re.compile(r"^(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")  # 회의록의 "0107"
# 회의록 파일명 끝의 발표자 이름: "프로젝트 미팅_20231129_홍길동.pptx"
# 끝에 붙는 한글이 모두 이름은 아니어서("_수정", "_반영") 성씨로 거른다.
_PRESENTER = re.compile(r"[_\s]([가-힣]{2,4})$")
_SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민진지엄채원천방공현함변염여추도소석선설마길연표명기왕육인맹제모탁국탁어은편용"
)
# 성과확인자료 파일명: "2025_RS-0000-XX000000_09_기관코드_제목"
_ACHIEVEMENT = re.compile(r"^(20\d{2})_([A-Z]{1,4}-[\d-]+\w*)_(\d+)_(\w+)_(.+)$")


@dataclass
class DocMeta:
    """문서 하나에 대한 메타데이터."""

    path: str
    name: str  # 확장자 없는 파일명
    doc_type: str = "기타"
    calendar_year: int | None = None
    proj_year: int | None = None  # 과제 연차 (1~n_years)
    date: str = ""  # YYYY-MM-DD (회의록 등 날짜가 특정되는 문서)
    org: str = ""
    presenter: str = ""  # 회의록 발표자
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """벡터 DB에 넣을 수 있는 평평한 딕셔너리."""
        d = {
            "path": self.path,
            "doc_name": self.name,
            "doc_type": self.doc_type,
            "date": self.date,
            "org": self.org,
            "presenter": self.presenter,
        }
        if self.calendar_year:
            d["calendar_year"] = self.calendar_year
        if self.proj_year:
            d["proj_year"] = self.proj_year
        return {k: v for k, v in d.items() if v not in ("", None)}


def _find_org(text: str) -> str:
    return get_profile().org_of_text(text)


def _find_date(text: str) -> tuple[str, int | None]:
    """YYYYMMDD 또는 YYMMDD 표기를 찾아 (YYYY-MM-DD, 연도)로 돌려준다."""
    if m := _YYYYMMDD.search(text):
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}", int(y)
    if m := _YYMMDD.search(text):
        y, mo, d = m.groups()
        return f"20{y}-{mo}-{d}", 2000 + int(y)
    return "", None


def extract(path: str | Path, root: Path | None = None) -> DocMeta:
    p = Path(path)
    rel = p.relative_to(root) if root else p
    parts = rel.parts
    stem, joined = p.stem, str(rel)

    meta = DocMeta(path=str(rel), name=stem)

    # 문서 종류: 파일명을 먼저 보고, 없으면 상위 폴더명으로 판단
    for label, keys in _DOC_TYPES:
        if any(k in stem for k in keys) or any(k in parts[0] for k in keys):
            meta.doc_type = label
            break
    else:
        if "성과확인자료" in parts[0]:
            meta.doc_type = "성과확인자료"

    # 날짜: 파일명 우선, 없으면 폴더명(회의록의 20231129 폴더)
    meta.date, year = _find_date(stem)
    if not meta.date:
        for part in parts:
            meta.date, year = _find_date(part)
            if meta.date:
                break

    # 연도: 날짜에서 못 얻으면 "2025", "2026_결과" 같은 연도 폴더에서
    if year is None:
        for part in parts:
            if m := _YEAR_FOLDER.match(part):
                year = int(m.group(1))
                break
    meta.calendar_year = year

    # 날짜가 아직 없으면 두 가지를 더 본다.
    #   · 회의록의 "2026\0107" 폴더 조합
    #   · 파일명의 "202601"(YYYYMM) 표기 — 이 경우 일자는 1일로 둔다
    if not meta.date and year:
        for part in parts:
            if m := _MMDD_FOLDER.match(part):
                meta.date = f"{year}-{m.group(1)}-{m.group(2)}"
                break
    if not meta.date:
        if m := _YYYYMM.search(stem):
            meta.date = f"{m.group(1)}-{m.group(2)}-01"
            meta.calendar_year = year = int(m.group(1))

    # 과제 연차: "4차년도" 표기가 있으면 그것을 우선한다
    if m := _NTH_YEAR.search(joined):
        meta.proj_year = int(m.group(1))
    elif stem.startswith("123_"):  # 1~3차년도를 묶은 단계보고서
        meta.proj_year = None
        meta.tags.append("1-3차년도")
    else:
        meta.proj_year = project_year(year)

    # 성과확인자료: "2025_RS-0000-XX000000_09_기관코드_제목"
    if m := _ACHIEVEMENT.match(stem):
        y, task_no, seq, org, title = m.groups()
        meta.calendar_year = int(y)
        meta.proj_year = project_year(int(y))
        meta.org = _find_org(org) or org
        meta.name = title
        meta.tags += [f"과제번호:{task_no}", f"성과번호:{seq}"]

    if not meta.org:
        meta.org = _find_org(joined)

    # 연차컨설팅·단계평가는 연초에 '전년도' 실적을 보고하는 자리다.
    # 발표 시점으로 연차를 매기면 전년도 내용이 당해 실적으로 둔갑한다.
    # (예: 2026-01 연차컨설팅은 4차년도(2025) 실적 보고다)
    if (
        meta.proj_year
        and meta.date[5:7] in ("01", "02", "03")
        and any(k in stem for k in ("연차컨설팅", "단계평가", "컨설팅", "성과발표"))
        and not _NTH_YEAR.search(stem)  # 파일명에 연차가 명시돼 있으면 그것을 믿는다
    ):
        meta.proj_year -= 1
        meta.tags.append("전년도 실적 보고")

    # 회의록 발표자
    if meta.doc_type == "회의록":
        if (m := _PRESENTER.search(stem)) and m.group(1)[0] in _SURNAMES:
            meta.presenter = m.group(1)

    return meta


if __name__ == "__main__":
    import sys

    import yaml

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    roots = [Path(p) for p in cfg["paths"]["sources"]]
    exts = {".pdf", ".hwpx", ".pptx", ".xlsx", ".md", ".html"}

    files = [f for r in roots if r.exists() for f in r.rglob("*") if f.suffix.lower() in exts]
    print(f"{len(files)}개 파일\n")

    show = "-v" in sys.argv
    header = f"{'종류':10s} {'연차':4s} {'연도':5s} {'날짜':11s} {'기관':6s} {'발표자':5s} 파일"
    print(header)
    print("-" * 110)
    for f in files[:40] if not show else files:
        m = extract(f)
        print(
            f"{m.doc_type:10s} {str(m.proj_year or '-'):4s} {str(m.calendar_year or '-'):5s} "
            f"{m.date or '-':11s} {m.org or '-':6s} {m.presenter or '-':5s} {m.name[:45]}"
        )

    from collections import Counter

    print("\n=== 종류별 ===", dict(Counter(extract(f).doc_type for f in files)))
    print("=== 연차별 ===", dict(Counter(str(extract(f).proj_year) for f in files)))
