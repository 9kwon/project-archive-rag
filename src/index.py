"""색인: 청크를 벡터 DB와 키워드 인덱스에 적재한다.

- 벡터: 로컬 임베딩 모델 → Chroma (storage/chroma)
- 키워드: Kiwi 형태소 토큰 → BM25 (storage/bm25.pkl)

두 인덱스 모두 원본과 파이프라인으로 언제든 다시 만들 수 있는 산출물이다.
"""

import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chromadb
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from chunker import Chunk
from text_norm import tokenize

COLLECTION = "project_archive"


def load_model(cfg: dict) -> SentenceTransformer:
    ec = cfg["embedding"]
    device = ec.get("device", "auto")
    if device == "auto":
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"

    model = SentenceTransformer(ec["model"], device=device)
    # ko-sroberta의 tokenizer는 model_max_length가 128로 적혀 있지만
    # 실제 모델은 512토큰까지 처리한다(max_position_embeddings=514).
    # 이 값을 그대로 두면 청크의 앞 1/4만 임베딩된다.
    if want := ec.get("max_seq_length"):
        hard_limit = model[0].auto_model.config.max_position_embeddings - 2
        model.max_seq_length = min(want, hard_limit)
    return model


def build(chunks: list[Chunk], cfg: dict, verbose: bool = True) -> None:
    storage = Path(cfg["paths"]["storage"])
    storage.mkdir(parents=True, exist_ok=True)

    model = load_model(cfg)
    if verbose:
        print(f"모델   : {cfg['embedding']['model']}")
        print(f"장치   : {model.device}, 최대 길이 {model.max_seq_length} 토큰")
        print(f"차원   : {model.get_sentence_embedding_dimension()}")

    texts = [c.text for c in chunks]

    # ── 벡터 인덱스 ──
    vectors = model.encode(
        texts,
        batch_size=cfg["embedding"].get("batch_size", 64),
        show_progress_bar=verbose,
        normalize_embeddings=True,  # 코사인 유사도를 내적으로 계산하기 위해
        convert_to_numpy=True,
    )

    client = chromadb.PersistentClient(path=str(storage / "chroma"))
    if COLLECTION in [c.name for c in client.list_collections()]:
        client.delete_collection(COLLECTION)
    col = client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})

    # Chroma는 한 번에 넣을 수 있는 양에 제한이 있다
    STEP = 2000
    for i in range(0, len(chunks), STEP):
        sl = slice(i, i + STEP)
        col.add(
            ids=[str(j) for j in range(i, min(i + STEP, len(chunks)))],
            embeddings=vectors[sl].tolist(),
            documents=texts[sl],
            metadatas=[c.meta for c in chunks[sl]],
        )

    # ── 키워드 인덱스 ──
    if verbose:
        print("BM25 토큰화 중…")
    corpus = [tokenize(t) for t in texts]
    bm25 = BM25Okapi(corpus)
    with (storage / "bm25.pkl").open("wb") as f:
        pickle.dump({"bm25": bm25, "metas": [c.meta for c in chunks], "texts": texts}, f)

    if verbose:
        size = sum(f.stat().st_size for f in storage.rglob("*") if f.is_file())
        print(f"\n색인 완료: {len(chunks):,}개 청크")
        print(f"저장 위치: {storage.resolve()} ({size / 1e6:.0f} MB)")


if __name__ == "__main__":
    import json

    from ingest import ingest_all, load_config

    cfg = load_config()
    cached = Path(cfg["paths"]["storage"]) / "chunks.jsonl"

    if cached.exists() and "--reingest" not in sys.argv:
        print(f"청크 불러오기: {cached}")
        chunks = [
            Chunk(**json.loads(line)) for line in cached.read_text(encoding="utf-8").splitlines()
        ]
    else:
        chunks = ingest_all(cfg)

    print(f"청크 {len(chunks):,}개\n")
    build(chunks, cfg)
