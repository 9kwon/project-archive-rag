"""마크다운 파서: 헤딩 계층과 표를 살려 추출한다.

5차년도 보고서 작성 재료인 분석 리포트(ANALYSIS_REPORT.md 등)가 대상이다.
"""

import re
from pathlib import Path

try:
    from .base import Block, heading_stack_push
except ImportError:
    from base import Block, heading_stack_push

_H = re.compile(r"^(#{1,6})\s+(.*)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_FENCE = re.compile(r"^\s*```")


def parse_md(path: str | Path) -> list[Block]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    table: list[str] = []
    in_fence = False
    idx = 0

    def flush(kind: str, chunk: list[str]):
        nonlocal idx
        text = "\n".join(chunk).strip()
        if text:
            idx += 1
            blocks.append(
                Block(idx, kind, text, locator=f"L{idx}", heading_path=[t for _, t in stack])
            )
        chunk.clear()

    for line in lines:
        if _FENCE.match(line):  # 코드 블록은 통째로 본문에 둔다
            in_fence = not in_fence
            buf.append(line)
            continue
        if in_fence:
            buf.append(line)
            continue

        if _TABLE_ROW.match(line):
            if buf:
                flush("body", buf)
            if not _TABLE_SEP.match(line):  # 구분선(|---|)은 버린다
                table.append(line.strip().strip("|").strip())
            continue
        if table:
            flush("table", table)

        if m := _H.match(line):
            if buf:
                flush("body", buf)
            level, title = len(m.group(1)), m.group(2).strip()
            path_before = heading_stack_push(stack, level, title)
            idx += 1
            blocks.append(
                Block(idx, "heading", title, locator=f"L{idx}", level=level, heading_path=path_before)
            )
            continue

        if not line.strip():
            if buf:
                flush("body", buf)
            continue
        buf.append(line)

    flush("table", table)
    flush("body", buf)
    return blocks


if __name__ == "__main__":
    import sys
    from collections import Counter

    target = Path(sys.argv[1])
    blocks = parse_md(target)
    print(f"파일 : {target.name}")
    print(f"블록 : {len(blocks)}개 {dict(Counter(b.kind for b in blocks))}, "
          f"총 {sum(len(b.text) for b in blocks):,}자")
    print("-" * 70)
    print("\n=== 제목 계층 ===")
    for b in [b for b in blocks if b.kind == "heading"][:20]:
        print(f"  {'  ' * (b.level - 1)}[{b.level}] {b.text[:70]}")
    print("\n=== 표 예시 ===")
    for b in [b for b in blocks if b.kind == "table"][:2]:
        print(f"  경로: {' > '.join(b.heading_path[-2:])}")
        for line in b.text.split("\n")[:5]:
            print(f"    {line[:120]}")
        print()
