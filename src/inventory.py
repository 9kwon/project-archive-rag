"""인제스트 대상 자료의 형식·개수를 집계한다.

인덱싱 전에 어떤 파일이 얼마나 있는지, 파서가 없는 형식은 무엇인지 확인한다.
"""

from collections import Counter
from pathlib import Path

SUPPORTED = {".pdf", ".hwpx", ".pptx", ".xlsx"}
IGNORED = {".db", ".ini", ".tmp"}  # Thumbs.db 등


def scan(roots: list[Path]) -> tuple[Counter, Counter, list[Path]]:
    """(폴더별 개수, 확장자별 개수, 미지원 파일 목록)"""
    by_folder, by_ext, unsupported = Counter(), Counter(), []
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.name.startswith("~$"):
                continue
            ext = f.suffix.lower()
            if ext in IGNORED:
                continue
            by_ext[ext] += 1
            if ext in SUPPORTED:
                by_folder[root.name] += 1
            else:
                unsupported.append(f)
    return by_folder, by_ext, unsupported


if __name__ == "__main__":
    import sys

    import yaml

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    roots = [Path(p) for p in cfg["paths"]["sources"]]

    by_folder, by_ext, unsupported = scan(roots)

    print("=== 소스 폴더 ===")
    for p in roots:
        mark = "" if p.exists() else "  (없음)"
        print(f"  {p}{mark}")

    print("\n=== 확장자별 ===")
    for ext, n in by_ext.most_common():
        mark = "OK " if ext in SUPPORTED else "-- "
        print(f"  {mark}{ext or '(없음)':8s} {n:4d}")

    print("\n=== 폴더별 (지원 형식만) ===")
    for folder, n in by_folder.most_common():
        print(f"  {folder:20s} {n:4d}")
    print(f"\n  합계 {sum(by_folder.values())}개 파일이 인제스트 대상")

    if unsupported and "-v" in sys.argv:
        print("\n=== 미지원 파일 ===")
        for f in unsupported:
            print(f"  {f}")
