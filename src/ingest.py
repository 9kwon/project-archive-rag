"""인제스트: 원본 문서 → 청크 목록.

파싱 → 노이즈 제거 → 청킹 → 메타데이터 부여까지를 한 번에 수행한다.
임베딩·색인은 다음 단계(index.py)에서 이 결과를 받아 처리한다.
"""

import sys
from dataclasses import asdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from chunker import Chunk, chunk_blocks
from metadata import extract
from parsers import PARSERS, parse
from project import get_profile
from text_norm import normalize_document

# 색인에서 뺄 문서 (config의 project.exclude_docs, 파일명 부분일치).
# 예: 아직 내용이 없는 빈 양식 보고서 — 이 시스템이 작성을 지원할 산출물이므로
# 소스로 색인하면 안 된다.
EXCLUDE = get_profile().exclude_docs

# 같은 자료가 여러 형식으로 있으면(발표자료 pptx + 그 PDF 변환본) 하나만 색인한다.
# 원본 형식이 위에 오도록 정렬한다 — 변환본은 띄어쓰기·표 구조가 깨져 있다.
FORMAT_PRIORITY = {".hwpx": 0, ".pptx": 1, ".xlsx": 2, ".md": 3, ".html": 4, ".pdf": 5}


def _drop_duplicates(files: list[Path], verbose: bool = True) -> list[Path]:
    by_stem: dict[str, list[Path]] = {}
    for f in files:
        by_stem.setdefault(f.stem, []).append(f)

    kept, dropped = [], []
    for stem, group in by_stem.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        group.sort(key=lambda p: FORMAT_PRIORITY.get(p.suffix.lower(), 9))
        kept.append(group[0])
        dropped.extend(group[1:])

    if verbose and dropped:
        print(f"중복 제외 {len(dropped)}건 (같은 이름의 다른 형식):")
        for f in dropped[:5]:
            print(f"  {f.name}")
    return sorted(kept)


def load_config(path: str = "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def collect_files(cfg: dict) -> list[Path]:
    roots = [Path(p) for p in cfg["paths"]["sources"]]
    files = []
    for root in roots:
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.name.startswith("~$"):
                continue
            if f.suffix.lower() not in PARSERS:
                continue
            if EXCLUDE and any(x in f.stem for x in EXCLUDE):
                continue
            files.append(f)
    return _drop_duplicates(files)


def ingest_file(path: Path) -> list[Chunk]:
    """문서 하나를 청크 목록으로 만든다."""
    blocks = parse(path)
    if not blocks:
        return []

    # 띄어쓰기가 소실된 문서(PPT→PDF 변환본 등)는 복원한다.
    texts, restored = normalize_document([b.text for b in blocks])
    if restored:
        for b, t in zip(blocks, texts):
            b.text = t

    meta = extract(path).as_dict()
    meta["spacing_restored"] = restored
    return chunk_blocks(blocks, meta)


def ingest_all(cfg: dict, verbose: bool = True) -> list[Chunk]:
    files = collect_files(cfg)
    all_chunks: list[Chunk] = []
    failed: list[tuple[Path, str]] = []

    for i, f in enumerate(files, 1):
        try:
            chunks = ingest_file(f)
        except Exception as e:  # 파일 하나가 전체를 막지 않게 한다
            failed.append((f, f"{type(e).__name__}: {e}"))
            continue
        all_chunks.extend(chunks)
        if verbose and (i % 25 == 0 or i == len(files)):
            print(f"  {i:3d}/{len(files)}  누적 청크 {len(all_chunks):,}")

    # 완전히 같은 텍스트의 청크는 하나만 남긴다.
    # 성과확인서처럼 성과 1건마다 같은 양식(과제정보 표 등)이 반복되는 문서가 있다.
    seen: set[str] = set()
    deduped = []
    for c in all_chunks:
        if c.text in seen:
            continue
        seen.add(c.text)
        deduped.append(c)
    if verbose and len(deduped) < len(all_chunks):
        print(f"  동일 텍스트 중복 {len(all_chunks) - len(deduped)}개 제거")
    all_chunks = deduped

    if failed:
        print(f"\n실패 {len(failed)}건:")
        for f, err in failed[:10]:
            print(f"  {f.name[:60]} — {err[:80]}")

    return all_chunks


if __name__ == "__main__":
    from collections import Counter

    cfg = load_config()
    files = collect_files(cfg)
    print(f"대상 파일 {len(files)}개\n")

    chunks = ingest_all(cfg)

    print(f"\n{'=' * 70}")
    print(f"총 청크 {len(chunks):,}개, {sum(len(c.body) for c in chunks):,}자")

    lens = sorted(len(c.body) for c in chunks)
    if lens:
        print(
            f"길이   중앙값 {lens[len(lens) // 2]}자 / "
            f"p90 {lens[int(len(lens) * 0.9)]}자 / 최대 {lens[-1]}자"
        )
    print(f"종류   {dict(Counter(c.meta.get('kind') for c in chunks))}")
    print(f"문서종류 {dict(Counter(c.meta.get('doc_type') for c in chunks))}")
    print(f"연차   {dict(sorted(Counter(str(c.meta.get('proj_year')) for c in chunks).items()))}")
    print(f"기관   {dict(Counter(c.meta.get('org', '-') for c in chunks))}")

    if "-s" in sys.argv:
        import json

        out = Path("storage/chunks.jsonl")
        out.parent.mkdir(exist_ok=True)
        with out.open("w", encoding="utf-8") as fp:
            for c in chunks:
                fp.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
        print(f"\n저장: {out} ({out.stat().st_size / 1e6:.1f} MB)")
