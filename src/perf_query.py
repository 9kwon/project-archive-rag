"""성과 DB 조회.

같은 표가 여러 보고서에 실려 있어(단계보고서·연차보고서) 그대로 합치면
건수가 두 배가 된다. 문서 우선순위로 하나를 고르고, 다른 문서의 값이
다르면 충돌로 표시한다(SKILL.md: "문서마다 수치가 다르면 임의로 하나를
선택하지 않고 사용자에게 표시한다").
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from project import get_profile

_P = get_profile()

# 뒤에 올수록 우선한다. 확정 보고서 > 단계 중간 보고서.
# 문서명 부분일치로 순위를 매기므로, 특정 문서를 콕 집어 우선하려면
# "4차년도 연차보고서"처럼 더 구체적인 이름을 뒤쪽에 추가하면 된다.
DOC_RANK = ["단계보고서", "성과확인서", "연차보고서"]


def _rank(doc: str) -> int:
    for i, name in enumerate(DOC_RANK):
        if name in doc:
            return i
    return -1


def connect(cfg: dict) -> sqlite3.Connection:
    path = Path(cfg["paths"]["storage"]) / "perf.sqlite"
    if not path.exists():
        raise FileNotFoundError(f"성과 DB가 없습니다: {path} (python src/perf_table.py 실행)")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _pick(rows: list[sqlite3.Row], key_fields: tuple, value_field: str) -> list[dict]:
    """같은 항목이 여러 문서에 있으면 우선순위가 높은 문서 값을 쓴다."""
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(tuple(r[f] for f in key_fields), []).append(r)

    out = []
    for key, rs in groups.items():
        rs.sort(key=lambda r: _rank(r["doc"]))
        best = rs[-1]
        others = {r[value_field] for r in rs if r[value_field] != best[value_field]}
        item = dict(best)
        item["sources"] = sorted({r["doc"] for r in rs})
        item["conflict"] = sorted(others) if others else None
        out.append(item)
    return out


def year_targets(con, indicator: str | None = None, proj_year: int | None = None) -> list[dict]:
    """연차별 목표치."""
    sql = "SELECT * FROM year_target WHERE 1=1"
    args: list = []
    if indicator:
        sql += " AND indicator LIKE ?"
        args.append(f"%{indicator}%")
    if proj_year:
        sql += " AND proj_year = ?"
        args.append(proj_year)
    rows = con.execute(sql, args).fetchall()
    picked = _pick(rows, ("indicator", "proj_year"), "target")
    return sorted(picked, key=lambda d: (d["indicator"], d["proj_year"]))


def year_actuals(con, proj_year: int | None = None) -> list[dict]:
    """당해연도 목표 대비 실적."""
    sql = "SELECT * FROM year_actual WHERE 1=1"
    args: list = []
    if proj_year:
        sql += " AND proj_year = ?"
        args.append(proj_year)
    rows = con.execute(sql, args).fetchall()
    return _pick(rows, ("indicator", "proj_year"), "year_actual")


def outcomes(con, proj_year: int | None = None) -> list[dict]:
    """논문·특허 등 정량 성과 건수."""
    sql = "SELECT * FROM quant_outcome WHERE 1=1"
    args: list = []
    if proj_year:
        sql += " AND proj_year = ?"
        args.append(proj_year)
    rows = con.execute(sql, args).fetchall()
    picked = _pick(rows, ("category", "subcategory", "proj_year"), "count")
    return sorted(picked, key=lambda d: (d["proj_year"], d["category"], d["subcategory"]))


def achievements(con, proj_year: int | None = None, org: str | None = None) -> list[dict]:
    """세부목표별 달성률."""
    sql = "SELECT * FROM achievement WHERE rate IS NOT NULL"
    args: list = []
    if proj_year:
        sql += " AND proj_year = ?"
        args.append(proj_year)
    if org:
        sql += " AND org LIKE ?"
        args.append(f"%{org}%")
    rows = con.execute(sql, args).fetchall()
    return _pick(rows, ("org", "goal", "proj_year"), "rate")


def indicator_names(con) -> list[str]:
    """DB에 있는 모든 지표명."""
    rows = con.execute(
        "SELECT DISTINCT indicator FROM year_target "
        "UNION SELECT DISTINCT indicator FROM year_actual"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def match_indicators(con, query: str) -> list[str]:
    """질문에 언급된 지표를 찾는다.

    지표명은 "OO 기반 서버-앱 플랫폼(핵심 지표 A 정확도)"처럼
    길기 때문에 괄호 안이나 뒷부분의 핵심어로 맞춰본다.
    """
    q = query.replace(" ", "")
    found = []
    for name in indicator_names(con):
        cores = re.findall(r"[(（]([^)）]+)[)）]", name) or [name]
        for core in cores:
            if len(core) >= 4 and core.replace(" ", "") in q:
                found.append(name)
                break
    return sorted(set(found))


def indicator_history(con, indicator: str) -> list[dict]:
    """한 지표의 연차별 목표·실적을 모은다."""
    targets = {t["proj_year"]: t for t in year_targets(con, indicator=indicator)}
    actuals: dict[int, dict] = {}
    for a in _pick(
        con.execute(
            "SELECT * FROM year_actual WHERE indicator LIKE ?", (f"%{indicator}%",)
        ).fetchall(),
        ("indicator", "proj_year"),
        "year_actual",
    ):
        actuals[a["proj_year"]] = a

    out = []
    for y in sorted(set(targets) | set(actuals)):
        t, a = targets.get(y, {}), actuals.get(y, {})
        out.append(
            {
                "proj_year": y,
                "calendar_year": _P.calendar_year(y),
                "unit": t.get("unit") or a.get("unit") or "",
                "target": t.get("target") or a.get("year_target") or "",
                "actual": a.get("year_actual") or "",
                "rate": a.get("rate"),
                "sources": sorted(set(t.get("sources", []) + a.get("sources", []))),
            }
        )
    return out


def format_indicator(indicator: str, rows: list[dict]) -> str:
    L = [f"### 지표: {indicator}"]
    for r in rows:
        unit = f" {r['unit']}" if r["unit"] and r["unit"] != "-" else ""
        line = f"- {r['proj_year']}차년도({r['calendar_year']}) 목표 {r['target'] or '미기재'}{unit}"
        line += f" / 실적 {r['actual']}{unit}" if r["actual"] else " / 실적 자료 없음"
        if r["rate"] is not None:
            line += f" [목표 대비 달성률 {r['rate']:.0f}%]"
        L.append(line)
    return "\n".join(L)


def year_summary(con, proj_year: int) -> dict:
    """한 연차의 목표·실적·달성률·성과를 한데 모은다.

    요구사항: "연차별 목표(지표)와 성과, 달성률, 수행내용을 간결하게 파악"
    """
    return {
        "proj_year": proj_year,
        "calendar_year": _P.calendar_year(proj_year),
        "targets": year_targets(con, proj_year=proj_year),
        "actuals": year_actuals(con, proj_year=proj_year),
        "outcomes": outcomes(con, proj_year=proj_year),
        "achievements": achievements(con, proj_year=proj_year),
    }


def format_summary(s: dict) -> str:
    """사람이 읽을 요약. LLM 프롬프트에도 그대로 넣는다."""
    L = [f"## {s['proj_year']}차년도 ({s['calendar_year']}년) 성과 요약"]

    if s["actuals"]:
        L.append("\n### 성능지표 목표 대비 실적")
        for a in s["actuals"]:
            unit = f" {a['unit']}" if a["unit"] and a["unit"] != "-" else ""
            line = (
                f"- {a['indicator']}: 목표 {a['year_target']}{unit} → "
                f"실적 {a['year_actual']}{unit}"
            )
            if a.get("rate") is not None:
                # 지표 값이 아니라 목표 대비 달성률이다(혼동 방지)
                line += f" [목표 대비 달성률 {a['rate']:.0f}%]"
            if a["final_target"]:
                line += f" [최종목표 {a['final_target']}{unit}]"
            L.append(line)

    if s["targets"]:
        L.append(f"\n### {s['proj_year']}차년도 목표치 ({len(s['targets'])}개 지표)")
        for t in s["targets"][:20]:
            unit = f" {t['unit']}" if t["unit"] and t["unit"] != "-" else ""
            mark = f"  ※ 다른 문서 값: {t['conflict']}" if t["conflict"] else ""
            L.append(f"- {t['indicator']}: {t['target']}{unit}{mark}")

    # 정량 성과는 논문·특허만 건수로 신뢰할 수 있다. 나머지 항목("기타" 등)은
    # 칸에 "자문 보고서(1)아키텍쳐 설계서(1)"처럼 산출물 이름이 적혀 있어 셀 수 없다.
    counted = [o for o in s["outcomes"] if any(k in o["category"] for k in ("논문", "특허"))]
    others = [o for o in s["outcomes"] if o not in counted]
    if counted:
        L.append("\n### 정량 성과 (논문·특허)")
        total: dict[str, int] = {}
        for o in counted:
            total[o["category"]] = total.get(o["category"], 0) + o["count"]
            sub = f" {o['subcategory']}" if o["subcategory"] else ""
            orgs = f" [{o['orgs']}]" if o["orgs"] else ""
            L.append(f"- {o['category']}{sub}: {o['count']}건{orgs}")
        L.append("  합계: " + ", ".join(f"{k} {v}건" for k, v in total.items()))
    if others:
        names = ", ".join(sorted({o["category"] for o in others}))
        L.append(f"\n  ※ 그 밖의 항목({names})은 원본 표에 산출물 이름이 적혀 있어 "
                 "자동 집계하지 않는다. 원본 표를 확인할 것.")

    if s["achievements"]:
        L.append("\n### 세부목표 달성률")
        for a in s["achievements"]:
            L.append(f"- [{a['org']}] {a['goal']}: {a['rate']:.0f}%")

    return "\n".join(L)


if __name__ == "__main__":
    from ingest import load_config

    cfg = load_config()
    con = connect(cfg)

    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        print(format_summary(year_summary(con, int(sys.argv[1]))))
    else:
        for y in _P.years:
            print(format_summary(year_summary(con, y)))
            print()
    con.close()
