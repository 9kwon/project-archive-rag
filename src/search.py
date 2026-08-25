"""하이브리드 검색: 벡터 + BM25를 RRF로 합친다.

한국어 문서에서 둘은 서로의 약점을 메운다.
- BM25   : "RS-0000-XX000000", "≥ 87%" 같은 고유명사·수치에 강하다
- 벡터   : "정확도를 어떻게 검증했나" 같은 의미 검색에 강하다

RRF(Reciprocal Rank Fusion)는 점수 체계가 다른 두 결과를 순위만으로 합치므로
정규화가 필요 없고 한쪽이 이상값을 내도 잘 견딘다.
"""

import pickle
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chromadb

from index import COLLECTION, load_model
from project import get_profile
from text_norm import tokenize

RRF_K = 60  # 순위 융합 상수. 클수록 상위권 가중치가 완만해진다.

# 두 검색의 융합 비중. 평가(eval/run_eval.py) 결과로 정한 값이다.
# 정부 R&D 문서는 표·수치·고유명사가 많아 키워드 검색이 더 정확했다.
# (STS 계열 임베딩 기준 BM25 단독 MRR 0.618 > 동등 가중 하이브리드 0.520)
RRF_WEIGHTS = {"bm25": 1.0, "vector": 0.5}

# 문서 권위도. 같은 사안이라도 확정된 공식 기록이 중간 논의보다 신뢰도가 높다.
# 값은 config의 project.doc_weights에서 읽는다 (연차보고서 1.35 … 회의록 0.85).
DOC_WEIGHT = get_profile().doc_weights


@dataclass
class Hit:
    text: str
    meta: dict
    score: float
    via: str  # 어느 검색이 찾았는지 (vector / bm25 / both)

    def source(self) -> str:
        """출처 문자열: 문서명 · 위치 · 절"""
        m = self.meta
        parts = [m.get("doc_name", "?")]
        if loc := m.get("locator"):
            parts.append(loc)
        if sec := m.get("section"):
            parts.append(sec)
        return " · ".join(parts)


class Searcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        storage = Path(cfg["paths"]["storage"])

        client = chromadb.PersistentClient(path=str(storage / "chroma"))
        self.col = client.get_collection(COLLECTION)

        with (storage / "bm25.pkl").open("rb") as f:
            data = pickle.load(f)
        self.bm25 = data["bm25"]
        self.bm_metas = data["metas"]
        self.bm_texts = data["texts"]

        self._model = None  # 임베딩 모델은 처음 검색할 때 올린다

    @property
    def model(self):
        if self._model is None:
            self._model = load_model(self.cfg)
        return self._model

    def _vector(self, query: str, k: int, where: dict | None) -> list[tuple[str, dict, float]]:
        vec = self.model.encode([query], normalize_embeddings=True)[0].tolist()
        res = self.col.query(
            query_embeddings=[vec],
            n_results=k,
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        return list(zip(res["documents"][0], res["metadatas"][0], res["distances"][0]))

    def _keyword(self, query: str, k: int, where: dict | None) -> list[tuple[str, dict, float]]:
        scores = self.bm25.get_scores(tokenize(query))
        order = scores.argsort()[::-1]
        out = []
        for i in order:
            if scores[i] <= 0:
                break
            meta = self.bm_metas[i]
            if where and not _matches(meta, where):
                continue
            out.append((self.bm_texts[i], meta, float(scores[i])))
            if len(out) >= k:
                break
        return out

    def search(
        self,
        query: str,
        k: int = 8,
        pool: int = 30,
        filters: dict | None = None,
        mode: str = "hybrid",
        weight: bool = True,
        rrf_weights: dict | None = None,
    ) -> list[Hit]:
        """filters 예: {"proj_year": 4} 또는 {"doc_type": "연차보고서"}"""
        where = _to_chroma_where(filters)

        vec_hits = self._vector(query, pool, where) if mode in ("hybrid", "vector") else []
        kw_hits = self._keyword(query, pool, filters) if mode in ("hybrid", "bm25") else []

        # 같은 청크를 두 검색이 모두 찾을 수 있으므로 본문으로 식별한다.
        # 단, 한 목록 안에서 같은 텍스트가 여러 번 나와도 최고 순위 한 번만 반영한다.
        # (점수를 누적하면 반복 양식이 많은 문서가 부당하게 밀어올려진다)
        fused: dict[str, dict] = {}
        for via, hits_list in (("vector", vec_hits), ("bm25", kw_hits)):
            w = (rrf_weights or RRF_WEIGHTS).get(via, 1.0)
            seen_in_list: set[str] = set()
            for rank, (text, meta, _) in enumerate(hits_list):
                if text in seen_in_list:
                    continue
                seen_in_list.add(text)
                fused.setdefault(text, {"meta": meta, "score": 0.0, "via": set()})
                fused[text]["score"] += w / (RRF_K + rank + 1)
                fused[text]["via"].add(via)

        hits = []
        for text, v in fused.items():
            score = v["score"]
            if weight:
                score *= DOC_WEIGHT.get(v["meta"].get("doc_type", ""), 1.0)
            via = "both" if len(v["via"]) > 1 else next(iter(v["via"]))
            hits.append(Hit(text, v["meta"], score, via))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


def _to_chroma_where(filters: dict | None) -> dict | None:
    """{"a": 1, "b": 2} → Chroma의 $and 형식"""
    if not filters:
        return None
    terms = [{k: {"$eq": v}} for k, v in filters.items()]
    return terms[0] if len(terms) == 1 else {"$and": terms}


def _matches(meta: dict, filters: dict) -> bool:
    return all(meta.get(k) == v for k, v in filters.items())


@lru_cache(maxsize=1)
def get_searcher() -> Searcher:
    from ingest import load_config

    return Searcher(load_config())


def _print_hits(q: str, hits: list[Hit], mode: str, filters: dict, full: bool = False):
    print(f"\n질의: {q}   (모드 {mode}, 필터 {filters or '없음'})")
    print("=" * 100)
    for i, h in enumerate(hits, 1):
        m = h.meta
        tag = f"{m.get('doc_type', '?')} {m.get('proj_year', '?')}차년도"
        print(f"\n[{i}] {h.score:.4f} ({h.via})  {tag}  {m.get('org', '-')}")
        print(f"    출처: {h.source()}")
        body = h.text if full else h.text[:300].replace("\n", " ")
        print(f"    {body}")


def repl():
    """대화형 검색. 모델을 한 번만 올리고 계속 질문을 받는다.

    질문 앞뒤에 옵션을 붙일 수 있다:
      연차보고서에서: 측정 정확도 목표     → doc_type 필터
      4차: 실증 실험 결과                  → proj_year 필터
    빈 줄 또는 q 입력 시 종료.
    """
    s = get_searcher()
    print("대화형 검색 (종료: q 또는 빈 줄)")
    print("필터 예시:  4차: 질문…   /  연차보고서: 질문…\n")

    doc_types = set(DOC_WEIGHT)
    while True:
        try:
            q = input("질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() == "q":
            break

        filters = {}
        if ":" in q:
            head, _, rest = q.partition(":")
            head = head.strip()
            if head.rstrip("차년도").isdigit():
                filters["proj_year"] = int(head.rstrip("차년도"))
                q = rest.strip()
            elif head in doc_types:
                filters["doc_type"] = head
                q = rest.strip()

        hits = s.search(q, k=5, filters=filters)
        _print_hits(q, hits, "hybrid", filters)
        print()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*")
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--year", type=int, help="과제 연차")
    ap.add_argument("--type", help="문서 종류 (연차보고서/회의록 …)")
    ap.add_argument("--org", help="기관 코드 (config project.organizations의 키)")
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "vector", "bm25"])
    ap.add_argument("--full", action="store_true", help="본문 전체 표시")
    args = ap.parse_args()

    if not args.query:  # 질의 없이 실행하면 대화형 모드
        repl()
        sys.exit(0)

    filters = {}
    if args.year:
        filters["proj_year"] = args.year
    if args.type:
        filters["doc_type"] = args.type
    if args.org:
        filters["org"] = args.org

    q = " ".join(args.query)
    hits = get_searcher().search(q, k=args.k, filters=filters, mode=args.mode)
    _print_hits(q, hits, args.mode, filters, args.full)
