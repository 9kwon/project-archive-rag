"""생성 품질 자동 채점 — LLM-as-judge + 규칙 기반 기권 판정.

검색 평가(run_eval.py)는 정답 근거가 상위 k에 들어오는지만 잰다.
이 스크립트는 그 다음 단계를 잰다:

  1. 정답률      LLM이 근거로 맞는 답을 만들었는가 (judge LLM이 기준 정답과 대조)
  2. 기권 행동   없는 것을 없다고 했는가 — **기권 정밀도/재현율**로 보고한다
                 · 재현율: absent 질문 중 실제로 기권한 비율 (환각 억제)
                 · 정밀도: 기권한 것 중 기권해야 했던 것의 비율 (과잉 기권 감지)
  3. 출처 표기율 답변 문장에 근거 번호 [n]이 달렸는가 (규칙 검사)

채점 설계 원칙:
  - 기권 판정은 **규칙을 먼저** 쓴다. 기권 문구는 시스템 프롬프트가 강제하는
    정형 표현이라 규칙이 정확하고, LLM 판정보다 재현 가능하다.
    규칙이 못 가른 경우에만 judge에게 묻는다.
  - 정답 판정은 judge LLM을 쓰되, **답변 생성 모델과 분리할 수 있게** 한다
    (config의 eval.judge). 같은 모델이 자기 답을 채점하면 관대해지는
    편향(self-preference)이 알려져 있다.
  - judge의 신뢰도 자체가 검증 대상이다. `--save`로 판정 근거를 함께 남겨
    사람이 표본 대조할 수 있게 한다. 사람 채점과의 일치도를 확인하기 전에는
    이 수치를 절대 지표가 아니라 회귀 감지용으로 쓸 것.

사용:
    python eval/grade_generation.py                 # 전체 질문 채점
    python eval/grade_generation.py --limit 5       # 앞 5문항만 (judge 동작 확인)
    python eval/grade_generation.py --save          # eval/generation_results.md 저장
    python eval/grade_generation.py --selftest      # LLM 없이 규칙·파서만 검사
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

QUESTIONS = Path(__file__).parent / "questions.yaml"
OUT = Path(__file__).parent / "generation_results.md"

# ── 기권 판정 (규칙) ─────────────────────────────────────────
# 시스템 프롬프트(generate.SYSTEM_PROMPT 규칙 1)가 강제하는 정형 기권 표현.
# 프롬프트를 바꿔 기권 문구가 달라지면 여기도 같이 바꿔야 한다.
ABSTAIN_PATTERNS = (
    "확인되지 않습니다",
    "확인되지 않는다",
    "확인할 수 없습니다",
    "자료에 없",
    "자료에서 찾을 수 없",
    "확인 불가",
)


def rule_abstained(answer: str) -> bool:
    """답변이 정형 기권 표현으로 기권했는가."""
    head = answer.strip()
    return any(p in head for p in ABSTAIN_PATTERNS)


def has_citation(answer: str) -> bool:
    """근거 번호 [n] 표기가 있는가."""
    return bool(re.search(r"\[\d+\]", answer))


# ── judge LLM ────────────────────────────────────────────────
JUDGE_SYSTEM = """당신은 검색 기반 질의응답 시스템의 채점자다.
질문, 사람이 확인한 기준 정답, 시스템 답변을 보고 판정한다.

판정 기준:
- correct   : 핵심 사실·수치가 기준 정답과 일치한다
- partial   : 일부만 일치한다 (예: 두 수치 중 하나만 정답)
- incorrect : 핵심이 틀렸거나 기준 정답과 모순된다
- abstain   : 답을 제시하지 않고 자료에 없다/확인 불가라고 했다

표기 차이(단위 표현, 조사, 어순)는 무시한다. 기준 정답에 없는 부가 설명은
감점하지 않는다. 수치가 있는 질문은 수치 일치가 판정의 중심이다.

반드시 아래 JSON 한 줄만 출력한다. 다른 텍스트를 붙이지 않는다.
{"verdict": "correct|partial|incorrect|abstain", "reason": "판정 근거 한 문장"}"""

JUDGE_USER = """질문: {question}

기준 정답: {gold}

시스템 답변:
{answer}"""


def parse_verdict(text: str) -> dict:
    """judge 출력에서 JSON 판정을 뽑는다. 못 뽑으면 verdict='parse_error'."""
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        return {"verdict": "parse_error", "reason": text.strip()[:120]}
    try:
        d = json.loads(m.group())
    except json.JSONDecodeError:
        return {"verdict": "parse_error", "reason": m.group()[:120]}
    v = str(d.get("verdict", "")).strip().lower()
    if v not in ("correct", "partial", "incorrect", "abstain"):
        return {"verdict": "parse_error", "reason": str(d)[:120]}
    return {"verdict": v, "reason": str(d.get("reason", ""))[:200]}


def judge_cfg(cfg: dict) -> dict:
    """judge용 LLM 설정. eval.judge에 지정된 값이 llm 섹션을 덮어쓴다.

    답변 생성 모델과 judge를 분리할 수 있게 하는 장치다. 같은 모델로
    자기 답을 채점하면 관대해지는 편향이 있으므로, 가능하면 eval.judge에
    더 강한 모델을 지정할 것.
    """
    judge = (cfg.get("eval") or {}).get("judge") or {}
    merged = {**cfg["llm"], **{k: v for k, v in judge.items() if v}}
    return {**cfg, "llm": merged}


# ── 채점 실행 ────────────────────────────────────────────────
@dataclass
class Row:
    id: str
    type: str
    question: str
    gold: str
    answer: str = ""
    abstained: bool = False
    verdict: str = ""
    reason: str = ""
    cited: bool = False
    n_hits: int = 0


def grade_one(q: dict, llm, k: int) -> Row:
    from generate import ask

    row = Row(id=q["id"], type=q["type"], question=q["question"], gold=str(q.get("answer", "")))
    answer, hits = ask(q["question"], q.get("filters"), k=k)
    row.answer, row.n_hits = answer, len(hits)
    row.cited = has_citation(answer)
    row.abstained = rule_abstained(answer)

    if row.abstained:
        # 기권은 규칙으로 확정 — judge를 부를 필요가 없다
        row.verdict, row.reason = "abstain", "정형 기권 표현 (규칙 판정)"
        return row

    v = parse_verdict(llm.chat(JUDGE_SYSTEM, JUDGE_USER.format(
        question=q["question"], gold=row.gold, answer=answer)))
    row.verdict, row.reason = v["verdict"], v["reason"]
    # judge가 기권으로 읽었으면 (규칙이 놓친 우회 표현) 기권으로 집계한다
    if row.verdict == "abstain":
        row.abstained = True
    return row


def summarize(rows: list[Row]) -> dict:
    answerable = [r for r in rows if r.type != "absent"]
    absent = [r for r in rows if r.type == "absent"]
    abstained = [r for r in rows if r.abstained]
    judged = [r for r in answerable if not r.abstained and r.verdict != "parse_error"]

    tp = sum(1 for r in abstained if r.type == "absent")  # 기권해야 했고 기권함
    s = {
        "n": len(rows),
        "correct": sum(1 for r in judged if r.verdict == "correct"),
        "partial": sum(1 for r in judged if r.verdict == "partial"),
        "incorrect": sum(1 for r in judged if r.verdict == "incorrect"),
        "judged": len(judged),
        "parse_error": sum(1 for r in rows if r.verdict == "parse_error"),
        # 기권 행동 — flagging 정밀도/재현율
        "abstain_precision": tp / len(abstained) if abstained else None,
        "abstain_recall": tp / len(absent) if absent else None,
        "n_absent": len(absent),
        "n_abstained": len(abstained),
        # 출처 표기 (기권 답변은 출처가 없는 게 정상이라 제외)
        "citation_rate": (
            sum(1 for r in rows if not r.abstained and r.cited)
            / max(1, sum(1 for r in rows if not r.abstained))
        ),
    }
    return s


def render(rows: list[Row], s: dict, judge_model: str) -> str:
    L = ["# 생성 품질 채점 결과\n"]
    L.append(f"질문 {s['n']}개 · judge: `{judge_model}` · `python eval/grade_generation.py`\n")
    L.append("> judge 판정은 사람 채점과의 일치도가 검증되기 전까지 **회귀 감지용**이다.")
    L.append("> 절대 수치로 인용하지 말 것. 판정 근거(reason)를 표본 대조할 것.\n")

    L.append("## 요약\n")
    L.append("| 지표 | 값 |")
    L.append("|---|---|")
    L.append(f"| 정답 / 부분 / 오답 (answerable {s['judged']}건 판정) | {s['correct']} / {s['partial']} / {s['incorrect']} |")
    ap = f"{s['abstain_precision']:.0%}" if s["abstain_precision"] is not None else "-"
    ar = f"{s['abstain_recall']:.0%}" if s["abstain_recall"] is not None else "-"
    L.append(f"| 기권 정밀도 (기권 {s['n_abstained']}건 중 기권해야 했던 것) | {ap} |")
    L.append(f"| 기권 재현율 (absent {s['n_absent']}건 중 기권한 것) | {ar} |")
    L.append(f"| 출처 표기율 (비기권 답변 중 [n] 표기) | {s['citation_rate']:.0%} |")
    if s["parse_error"]:
        L.append(f"| judge 파싱 실패 | {s['parse_error']}건 |")

    L.append("\n## 문항별\n")
    L.append("| id | 유형 | 판정 | 출처 | 근거 |")
    L.append("|---|---|---|---|---|")
    for r in rows:
        mark = "✓" if r.cited else ("-" if r.abstained else "✗")
        L.append(f"| {r.id} | {r.type} | {r.verdict} | {mark} | {r.reason[:60]} |")

    L.append("\n<details><summary>답변 전문</summary>\n")
    for r in rows:
        L.append(f"### {r.id} — {r.question}\n")
        L.append(f"기준 정답: {r.gold}\n")
        L.append(f"```\n{r.answer.strip()[:1200]}\n```\n")
    L.append("</details>")
    return "\n".join(L)


# ── 셀프테스트 (LLM 없이 규칙·파서 검사) ─────────────────────
def selftest() -> int:
    cases_abstain = [
        ("해당 내용은 제공된 자료에서 확인되지 않습니다.", True),
        ("확인 불가 — 자료에 없는 정보다.", True),
        ("4차년도 논문은 4편이다 [1].", False),
    ]
    cases_parse = [
        ('{"verdict": "correct", "reason": "수치 일치"}', "correct"),
        ('판정합니다.\n{"verdict": "INCORRECT", "reason": "모순"}', "incorrect"),
        ("JSON 없이 서술만", "parse_error"),
        ('{"verdict": "maybe"}', "parse_error"),
    ]
    fails = 0
    for text, want in cases_abstain:
        got = rule_abstained(text)
        ok = got is want
        fails += not ok
        print(f"  {'OK' if ok else 'FAIL'} rule_abstained({text[:30]!r}) = {got}")
    for text, want in cases_parse:
        got = parse_verdict(text)["verdict"]
        ok = got == want
        fails += not ok
        print(f"  {'OK' if ok else 'FAIL'} parse_verdict → {got} (기대 {want})")
    print("셀프테스트", "통과" if fails == 0 else f"실패 {fails}건")
    return fails


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="앞 N문항만 채점")
    ap.add_argument("--save", action="store_true", help=f"{OUT.name}로 저장")
    ap.add_argument("--selftest", action="store_true", help="LLM 없이 규칙·파서만 검사")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    from generate import load_llm
    from ingest import load_config

    cfg = load_config()
    k = (cfg.get("eval") or {}).get("k", 8)
    jcfg = judge_cfg(cfg)
    llm = load_llm(jcfg)
    judge_model = jcfg["llm"].get("model", "?")

    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    if args.limit:
        questions = questions[: args.limit]

    rows = []
    for q in questions:
        print(f"  채점 중: {q['id']} ({q['type']})", file=sys.stderr)
        rows.append(grade_one(q, llm, k))

    s = summarize(rows)
    text = render(rows, s, judge_model)
    print("\n" + text)
    if args.save:
        OUT.write_text(text, encoding="utf-8")
        print(f"\n저장: {OUT}", file=sys.stderr)
