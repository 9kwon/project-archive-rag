"""성과 표를 정형 데이터로 추출한다.

LLM 요약으로는 표의 열을 잘못 읽는 일이 잦다(예: "계" 누적값을 특정 연차 값으로 오독).
연차별 목표·실적·달성률은 조회로 답해야 정확하다.

다루는 표 네 가지:
  A. 연구개발성과 성능지표    연차별 목표치 (1~5차년도 열)
  B. 주요성능Spec            당해연도 목표/실적
  C. 정량적 연구개발성과표    논문·특허 건수 (연도별, 기관 표기 포함)
  D. 목표대비 달성도          세부목표별 달성률 (기관 구분)
"""

import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from metadata import extract, project_year
from parsers import parse
from parsers.base import Block
from project import get_profile

# 빈 양식의 자리표시자 행
_PLACEHOLDER = re.compile(r"\(세부목표\)|기관명 입력|개조식으로 명시|^[\s.·․\-|%]*$")
# 항목명 앞 번호: "1. 항목명…", "8.항목명…"
_LEADING_NO = re.compile(r"^\d+\s*[.)]\s*")
# 정량성과표 셀: "1(기관A)", "1(기관A)1(기관B)", "1(기관A+기관B)"
_COUNT_ORG = re.compile(r"(\d+)\s*\(([^)]*)\)")


def _clean(cell: str) -> str:
    return cell.strip().replace("　", " ")


def _rows(block: Block) -> list[list[str]]:
    return [[_clean(c) for c in line.split(" | ")] for line in block.text.split("\n")]


def _num(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


# ──────────────────────────────────────────────────────────────
# A. 연구개발성과 성능지표 — 연차별 목표치
# ──────────────────────────────────────────────────────────────
@dataclass
class TargetRow:
    indicator: str
    unit: str
    weight: float | None
    proj_year: int
    target: str
    basis: str
    world_best: str
    doc: str
    locator: str


def parse_year_targets(block: Block, doc: str) -> list[TargetRow]:
    rows = _rows(block)
    # "1차년도 … 5차년도"가 나열된 헤더 행을 찾는다
    hdr_i, year_cols = None, {}
    for i, r in enumerate(rows[:6]):
        cols = {
            int(m.group(1)): j
            for j, c in enumerate(r)
            if (m := re.fullmatch(r"([1-9])차년?도", c))
        }
        if len(cols) >= 3:
            hdr_i, year_cols = i, cols
            break
    if hdr_i is None:
        return []

    out = []
    for r in rows[hdr_i + 1 :]:
        if len(r) < max(year_cols.values()) + 1:
            continue
        name = _LEADING_NO.sub("", r[0])
        if not name or _PLACEHOLDER.match(name) or "평가 항목" in name:
            continue
        for year, col in sorted(year_cols.items()):
            val = r[col]
            if not val or val == "-":
                continue
            out.append(
                TargetRow(
                    indicator=name,
                    unit=r[1] if len(r) > 1 else "",
                    weight=_num(r[2]) if len(r) > 2 else None,
                    proj_year=year,
                    target=val,
                    basis=r[-1] if len(r) > 10 else "",
                    world_best=r[3] if len(r) > 3 else "",
                    doc=doc,
                    locator=block.locator,
                )
            )
    return out


# ──────────────────────────────────────────────────────────────
# B. 주요성능Spec — 당해연도 목표/실적
# ──────────────────────────────────────────────────────────────
@dataclass
class ActualRow:
    indicator: str
    unit: str
    weight: float | None
    proj_year: int | None
    final_target: str
    year_target: str
    year_actual: str
    basis: str
    doc: str
    locator: str
    rate: float | None = None  # 달성도(%) — 단계평가 자료에만 있다


def parse_year_actuals(block: Block, doc: str, proj_year: int | None) -> list[ActualRow]:
    rows = _rows(block)
    hdr_i = next(
        (i for i, r in enumerate(rows[:4]) if "최종목표" in " ".join(r) and "해당연도" in " ".join(r)),
        None,
    )
    if hdr_i is None:
        return []

    hdr = rows[hdr_i]
    try:
        c_final = hdr.index("최종목표")
    except ValueError:
        return []
    # "해당연도"가 두 번 나온다: 목표, 실적 순서
    c_year = [j for j, c in enumerate(hdr) if c == "해당연도"]

    out = []
    for r in rows[hdr_i + 1 :]:
        name = _LEADING_NO.sub("", r[0]) if r else ""
        if not name or _PLACEHOLDER.match(name) or "평가 항목" in name:
            continue
        out.append(
            ActualRow(
                indicator=name,
                unit=r[1] if len(r) > 1 else "",
                weight=_num(r[2]) if len(r) > 2 else None,
                proj_year=proj_year,
                final_target=r[c_final] if len(r) > c_final else "",
                year_target=r[c_year[0]] if c_year and len(r) > c_year[0] else "",
                year_actual=r[c_year[1]] if len(c_year) > 1 and len(r) > c_year[1] else "",
                basis=r[-2] if len(r) > 8 else "",
                doc=doc,
                locator=block.locator,
            )
        )
    return out


# ──────────────────────────────────────────────────────────────
# C. 정량적 연구개발성과표 — 논문·특허 건수
# ──────────────────────────────────────────────────────────────
def parse_stage_actuals(block: Block, doc: str) -> list[ActualRow]:
    """단계평가 발표자료의 연차별 실적 표.

    구조: 평가항목 | 하위항목 | 단위 | 비중 | N차년도 개발 목표치 | N차년도 개발 실적 | 달성도(%) | 근거
    대분류는 세로 병합되어 빈 셀로 나오므로 직전 값을 이어 쓴다.
    """
    rows = _rows(block)
    if not rows:
        return []

    hdr = rows[0]
    joined = " ".join(hdr)
    m = re.search(r"([1-9])차년도\s*개발\s*목표치", joined)
    if not m:
        return []
    year = int(m.group(1))

    def col_of(pat: str) -> int | None:
        return next((j for j, c in enumerate(hdr) if re.search(pat, c)), None)

    c_target, c_actual = col_of(r"개발\s*목표치"), col_of(r"개발\s*실적")
    c_rate, c_unit, c_weight = col_of(r"달성도"), col_of(r"^단위$"), col_of(r"^비중$")
    if c_target is None or c_actual is None:
        return []

    out, major = [], ""
    for r in rows[1:]:
        if len(r) <= max(c_target, c_actual):
            continue
        head = _LEADING_NO.sub("", r[0])
        if head:
            major = head
        sub = r[1] if len(r) > 1 else ""
        name = f"{major} ({sub})" if sub and sub != major else major
        if not name.strip():
            continue

        target, actual = r[c_target], r[c_actual]
        if not target and not actual:  # 값이 비어 있는 행은 담지 않는다
            continue
        out.append(
            ActualRow(
                indicator=name,
                unit=r[c_unit] if c_unit is not None and len(r) > c_unit else "",
                weight=_num(r[c_weight]) if c_weight is not None and len(r) > c_weight else None,
                proj_year=year,
                final_target="",
                year_target=target,
                year_actual=actual,
                basis=r[-1] if len(r) > 7 else "",
                doc=doc,
                locator=block.locator,
                rate=_num(r[c_rate]) if c_rate is not None and len(r) > c_rate else None,
            )
        )
    return out


@dataclass
class OutcomeRow:
    category: str
    subcategory: str
    proj_year: int
    calendar_year: int
    count: int
    orgs: str
    doc: str
    locator: str


def parse_quant_outcomes(block: Block, doc: str) -> list[OutcomeRow]:
    rows = _rows(block)
    # 헤더의 "1차연도(2022)" 형태에서 연도를 읽는다.
    # 단계 안의 차수가 아니라 연도로 과제 연차를 정해야 한다
    # (2단계 1차연도(2025)는 과제 4차년도다).
    hdr_i, year_cols = None, {}
    for i, r in enumerate(rows[:6]):
        cols = {
            int(m.group(1)): j
            for j, c in enumerate(r)
            if (m := re.search(r"\((20\d{2})\)", c))
        }
        if len(cols) >= 3:
            hdr_i, year_cols = i, cols
            break
    if hdr_i is None:
        return []

    out = []
    for r in rows[hdr_i + 1 :]:
        if len(r) < 3:
            continue
        category, sub = r[0], r[1] if len(r) > 1 else ""
        if not category or "성과지표명" in category:
            continue
        for cal_year, col in sorted(year_cols.items()):
            if len(r) <= col:
                continue
            cell = r[col]
            if not cell or cell == "-":
                continue
            pairs = _COUNT_ORG.findall(cell)  # [("1", "기관A"), ("1", "기관B")]
            if pairs:
                count = sum(int(n) for n, _ in pairs)
                orgs = ",".join(o.strip() for _, o in pairs)
            elif (n := _num(cell)) is not None:
                count, orgs = int(n), ""
            else:
                continue
            py = project_year(cal_year)
            if py is None:
                continue
            out.append(
                OutcomeRow(
                    category=category,
                    subcategory=sub,
                    proj_year=py,
                    calendar_year=cal_year,
                    count=count,
                    orgs=orgs,
                    doc=doc,
                    locator=block.locator,
                )
            )
    return out


# ──────────────────────────────────────────────────────────────
# D. 목표대비 달성도 — 세부목표별
# ──────────────────────────────────────────────────────────────
@dataclass
class AchievementRow:
    org: str
    goal: str
    proj_year: int | None
    target_desc: str
    result_desc: str
    rate: float | None
    doc: str
    locator: str


_ORG_LINE = re.compile(r"^\[(주관|공동|위탁)\]\s*(.+)")


def parse_achievements(block: Block, doc: str) -> list[AchievementRow]:
    rows = _rows(block)
    # 연차보고서는 헤더에 "4차년도 연구개발목표"로 연차가 적혀 있다
    year = None
    for r in rows[:3]:
        if m := re.search(r"([1-9])차년도\s*연구개발목표", " ".join(r)):
            year = int(m.group(1))
            break

    out, org = [], ""
    for r in rows:
        if len(r) == 1:  # 기관 구분 행 또는 연차 구분 행
            # 단계보고서는 "1차년도"가 단독 행으로 나오고 그 아래에 세부목표가 온다
            if m := re.fullmatch(r"([1-9])차년?도", r[0]):
                year = int(m.group(1))
            elif m := _ORG_LINE.match(r[0]):
                name = m.group(2)
                if "입력" not in name:
                    org = name.split(",")[0].strip()
            continue
        if len(r) < 4:
            continue
        goal = _LEADING_NO.sub("", r[0])
        if not goal or _PLACEHOLDER.match(goal) or "세부목표" in goal:
            continue
        out.append(
            AchievementRow(
                org=org,
                goal=goal,
                proj_year=year,
                target_desc=r[1],
                result_desc=r[2],
                rate=_num(r[3]),
                doc=doc,
                locator=block.locator,
            )
        )
    return out


# ──────────────────────────────────────────────────────────────
# E. 성과 목록 표 — 논문 / 학술발표 / 특허 (건별 목록)
# ──────────────────────────────────────────────────────────────
# 성능지표의 담당 기관 판정. 지표명 키워드 → 기관 매핑은 config의
# project.indicator_owner에서 읽는다 (보고서의 기관별 성능지표 표 기준으로 작성).
_YEAR_ROW = re.compile(r"^([1-9])차년?도$")
_DATE = re.compile(r"(20\d{2})\s*[.\-년]\s*(\d{1,2})")


def indicator_org(indicator: str) -> str:
    return get_profile().indicator_org(indicator)


def _year_from_date(text: str) -> int | None:
    if m := _DATE.search(text):
        return project_year(int(m.group(1)))
    return None


@dataclass
class ItemRow:
    kind: str  # paper | presentation | patent
    title: str
    detail: str  # 학술지명 / 회의 장소 / 특허 번호
    person: str  # 주저자 / 발표자 / 출원인
    date: str
    proj_year: int | None
    org: str
    doc: str
    locator: str


def parse_item_list(block: Block, doc: str, doc_year: int | None) -> list[ItemRow]:
    """논문·학술발표·특허 목록 표를 건별로 뽑는다.

    단계보고서는 표 안에 "1차년도" 같은 구분 행이 들어 있고,
    연차보고서는 문서 자체가 한 연차에 해당한다.
    """
    rows = _rows(block)
    head = block.text[:300]

    if "논문명" in head:
        kind, c_title, c_detail, c_person, c_date = "paper", 1, 2, 3, 8
    elif "회의 명칭" in head:
        kind, c_title, c_detail, c_person, c_date = "presentation", 1, 4, 2, 3
    elif "지식재산권 등 명칭" in head:
        kind, c_title, c_detail, c_person, c_date = "patent", 1, 5, 3, 4
    else:
        return []

    out, year = [], doc_year
    for r in rows:
        if len(r) == 1:
            if m := _YEAR_ROW.match(r[0]):
                year = int(m.group(1))
            continue
        if len(r) <= c_title or not r[0].isdigit():  # 헤더·빈 행 제외
            continue

        title = r[c_title]
        if not title or _PLACEHOLDER.match(title):
            continue

        def cell(i: int) -> str:
            return r[i] if len(r) > i else ""

        date = cell(c_date)
        person = cell(c_person)
        # 기관은 행 안에 적힌 기관명으로만 판단한다.
        # 표 앞쪽의 "[위탁] OO병원" 같은 마커를 물려받으면
        # 뒤따르는 성과 목록 전체가 그 기관으로 잘못 분류된다.
        org = get_profile().org_of_text(" ".join(r))
        out.append(
            ItemRow(
                kind=kind,
                title=title,
                detail=cell(c_detail),
                person=person,
                date=date,
                proj_year=year or _year_from_date(date),
                org=org,
                doc=doc,
                locator=block.locator,
            )
        )
    return out


# ──────────────────────────────────────────────────────────────
# 적재
# ──────────────────────────────────────────────────────────────
SCHEMA = """
DROP TABLE IF EXISTS year_target;
CREATE TABLE year_target (
  indicator TEXT, unit TEXT, weight REAL, proj_year INTEGER,
  target TEXT, basis TEXT, world_best TEXT, doc TEXT, locator TEXT
);
DROP TABLE IF EXISTS year_actual;
CREATE TABLE year_actual (
  indicator TEXT, unit TEXT, weight REAL, proj_year INTEGER,
  final_target TEXT, year_target TEXT, year_actual TEXT,
  basis TEXT, doc TEXT, locator TEXT, rate REAL
);
DROP TABLE IF EXISTS quant_outcome;
CREATE TABLE quant_outcome (
  category TEXT, subcategory TEXT, proj_year INTEGER, calendar_year INTEGER,
  count INTEGER, orgs TEXT, doc TEXT, locator TEXT
);
DROP TABLE IF EXISTS achievement;
CREATE TABLE achievement (
  org TEXT, goal TEXT, proj_year INTEGER, target_desc TEXT,
  result_desc TEXT, rate REAL, doc TEXT, locator TEXT
);
DROP TABLE IF EXISTS item;
CREATE TABLE item (
  kind TEXT, title TEXT, detail TEXT, person TEXT, date TEXT,
  proj_year INTEGER, org TEXT, doc TEXT, locator TEXT
);
"""

# 표 종류를 알아보는 표지
MARKERS = {
    "target": ("연구개발성과 성능지표", "연구개발 목표치"),
    "actual": ("주요성능Spec",),
    "outcome": ("정량적 연구개발성과표", "단계성과지표명"),
    "achievement": ("목표대비 달성도",),
    "stage_actual": ("개발 목표치",),  # 단계평가 발표자료의 연차별 실적 표
    "item": ("논문명", "회의 명칭", "지식재산권 등 명칭"),  # 건별 성과 목록
}

_ACTUAL_SQL = (
    "INSERT INTO year_actual VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    lambda r: (
        r.indicator, r.unit, r.weight, r.proj_year, r.final_target,
        r.year_target, r.year_actual, r.basis, r.doc, r.locator, r.rate,
    ),
)


def build(cfg: dict, verbose: bool = True) -> dict:
    from ingest import collect_files

    db_path = Path(cfg["paths"]["storage"]) / "perf.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    counts = dict.fromkeys(MARKERS, 0)
    # 성과 표가 실린 문서. 단계평가 발표자료에 1~3차년도 실적이 있다.
    files = [
        f
        for f in collect_files(cfg)
        if any(
            k in f.stem
            for k in ("연차보고서", "단계보고서", "성과확인서", "단계평가")
        )
    ]

    for f in files:
        meta = extract(f)
        doc = meta.name
        try:
            blocks = [b for b in parse(f) if b.kind == "table"]
        except Exception as e:
            print(f"  건너뜀: {f.name} — {e}")
            continue

        for b in blocks:
            head = b.text[:400]
            if any(m in head for m in MARKERS["target"]):
                rs = parse_year_targets(b, doc)
                con.executemany(
                    "INSERT INTO year_target VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (r.indicator, r.unit, r.weight, r.proj_year, r.target,
                         r.basis, r.world_best, r.doc, r.locator)
                        for r in rs
                    ],
                )
                counts["target"] += len(rs)
            if any(m in head for m in MARKERS["actual"]):
                rs = parse_year_actuals(b, doc, meta.proj_year)
                sql, row = _ACTUAL_SQL
                con.executemany(sql, [row(r) for r in rs])
                counts["actual"] += len(rs)
            if any(m in head for m in MARKERS["stage_actual"]):
                rs = parse_stage_actuals(b, doc)
                sql, row = _ACTUAL_SQL
                con.executemany(sql, [row(r) for r in rs])
                counts["stage_actual"] += len(rs)
            if any(m in head for m in MARKERS["outcome"]):
                rs = parse_quant_outcomes(b, doc)
                con.executemany(
                    "INSERT INTO quant_outcome VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (r.category, r.subcategory, r.proj_year, r.calendar_year,
                         r.count, r.orgs, r.doc, r.locator)
                        for r in rs
                    ],
                )
                counts["outcome"] += len(rs)
            # 성과확인서에는 같은 이름의 "작성 요령" 표가 있어 목록으로 오인된다.
            # 실제 성과 목록은 연차보고서·단계보고서에만 실린다.
            if any(m in head for m in MARKERS["item"]) and "성과확인서" not in doc:
                rs = parse_item_list(b, doc, meta.proj_year)
                con.executemany(
                    "INSERT INTO item VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (r.kind, r.title, r.detail, r.person, r.date,
                         r.proj_year, r.org, r.doc, r.locator)
                        for r in rs
                    ],
                )
                counts["item"] += len(rs)
            if any(m in head for m in MARKERS["achievement"]):
                rs = parse_achievements(b, doc)
                con.executemany(
                    "INSERT INTO achievement VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (r.org, r.goal, r.proj_year, r.target_desc,
                         r.result_desc, r.rate, r.doc, r.locator)
                        for r in rs
                    ],
                )
                counts["achievement"] += len(rs)

    con.commit()
    if verbose:
        print(f"성과 DB: {db_path}")
        for k, v in counts.items():
            print(f"  {k:12s} {v:4d}행")
    con.close()
    return counts


if __name__ == "__main__":
    from ingest import load_config

    cfg = load_config()
    build(cfg)

    con = sqlite3.connect(Path(cfg["paths"]["storage"]) / "perf.sqlite")
    con.row_factory = sqlite3.Row

    # 첫 지표를 골라 연차별 목표를 보여준다 (적재 확인용)
    first = con.execute("SELECT indicator FROM year_target LIMIT 1").fetchone()
    if first:
        print(f"\n=== '{first['indicator'][:30]}' 연차별 목표 ===")
        for r in con.execute(
            "SELECT proj_year, target, doc FROM year_target "
            "WHERE indicator = ? ORDER BY proj_year, doc",
            (first["indicator"],),
        ):
            print(f"  {r['proj_year']}차년도  {r['target']:12s}  ({r['doc']})")

    print("\n=== 연차별 논문·특허 건수 ===")
    for r in con.execute(
        "SELECT proj_year, category, subcategory, SUM(count) n, GROUP_CONCAT(DISTINCT orgs) orgs "
        "FROM quant_outcome WHERE category LIKE '%논문%' OR category LIKE '%특허%' "
        "GROUP BY proj_year, category, subcategory ORDER BY proj_year"
    ):
        print(f"  {r['proj_year']}차년도  {r['category']:12s} {r['subcategory']:18s} {r['n']}건  [{r['orgs'] or '-'}]")

    print("\n=== 세부목표 달성률 ===")
    for r in con.execute(
        "SELECT org, proj_year, goal, rate FROM achievement WHERE rate IS NOT NULL "
        "ORDER BY proj_year, org"
    ):
        print(f"  {r['proj_year']}차 {r['org'][:20]:20s} {r['goal'][:38]:38s} {r['rate']:.0f}%")
    con.close()
