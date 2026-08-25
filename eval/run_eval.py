"""검색 품질 평가.

측정 지표
  Hit@k  정답 근거가 상위 k개 안에 들어온 질문의 비율
  MRR    정답이 처음 나온 순위의 역수 평균 (1위면 1.0, 5위면 0.2)

검색 설정을 바꿀 때마다 같은 질문셋으로 돌려 비교한다.
개선했다고 생각한 변경이 실제로는 다른 질문을 망치는 일이 흔하다.
"""

import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from search import Searcher  # noqa: E402

QUESTIONS = Path(__file__).parent / "questions.yaml"


def is_gold(hit, q: dict) -> bool:
    """이 검색 결과가 정답 근거인가."""
    doc = hit.meta.get("doc_name", "")
    if not any(g in doc for g in q["gold_docs"]):
        return False
    if not q.get("must_contain"):
        return True
    return any(s in hit.text for s in q["must_contain"])


def evaluate(searcher: Searcher, questions: list[dict], k: int = 8, **search_kw) -> dict:
    rows = []
    for q in questions:
        if q["type"] == "absent":  # 검색 평가 대상이 아니다(생성 단계에서 평가)
            continue
        hits = searcher.search(q["question"], k=k, filters=q.get("filters"), **search_kw)
        rank = next((i for i, h in enumerate(hits, 1) if is_gold(h, q)), None)
        rows.append(
            {
                "id": q["id"],
                "type": q["type"],
                "question": q["question"],
                "rank": rank,
                "top1": hits[0].source() if hits else "-",
            }
        )

    found = [r for r in rows if r["rank"]]
    return {
        "rows": rows,
        "hit_rate": len(found) / len(rows) if rows else 0.0,
        "mrr": sum(1 / r["rank"] for r in found) / len(rows) if rows else 0.0,
        "n": len(rows),
    }


def report(res: dict, label: str = ""):
    print(f"\n{'=' * 88}")
    print(f"{label}   질문 {res['n']}개   Hit@k {res['hit_rate']:.1%}   MRR {res['mrr']:.3f}")
    print("=" * 88)

    by_type = defaultdict(list)
    for r in res["rows"]:
        by_type[r["type"]].append(r)
    for t, rows in by_type.items():
        hit = sum(1 for r in rows if r["rank"])
        print(f"  {t:8s} {hit}/{len(rows)}")

    print("\n실패한 질문:")
    fails = [r for r in res["rows"] if not r["rank"]]
    if not fails:
        print("  없음")
    for r in fails:
        print(f"  [{r['id']}] {r['question']}")
        print(f"        1위: {r['top1']}")

    print("\n순위 분포:")
    for r in res["rows"]:
        mark = str(r["rank"]) if r["rank"] else "✗"
        print(f"  {r['id']}  {mark:>2s}  {r['question'][:58]}")


if __name__ == "__main__":
    from ingest import load_config  # noqa: E402

    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    cfg = load_config()
    searcher = Searcher(cfg)

    k = 8
    if "--compare" in sys.argv:
        print(f"모델: {cfg['embedding']['model']}\n")
        for mode in ("hybrid", "vector", "bm25"):
            res = evaluate(searcher, questions, k=k, mode=mode)
            print(f"  {mode:8s}      Hit@{k} {res['hit_rate']:5.1%}  MRR {res['mrr']:.3f}")

        res = evaluate(searcher, questions, k=k, weight=False)
        print(f"  {'권위도 없음':8s}  Hit@{k} {res['hit_rate']:5.1%}  MRR {res['mrr']:.3f}")

        # RRF 융합 비중 탐색 — 벡터 검색이 실제로 기여하는 지점을 찾는다
        print("\n  RRF 벡터 비중별:")
        for wv in (0.0, 0.25, 0.5, 0.75, 1.0):
            res = evaluate(
                searcher, questions, k=k, rrf_weights={"bm25": 1.0, "vector": wv}
            )
            print(f"    vector={wv:<5} Hit@{k} {res['hit_rate']:5.1%}  MRR {res['mrr']:.3f}")
        sys.exit(0)

    res = evaluate(searcher, questions, k=k)
    report(res, f"하이브리드 검색 (k={k})")
