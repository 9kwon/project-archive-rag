"""PDF 파서: 페이지 단위로 텍스트와 표를 추출한다.

출처 표시를 위해 페이지 번호를 locator("p.12")로 남긴다.

발표자료를 PDF로 변환한 문서에는 성과 실적 표가 들어 있다.
표를 격자로 뽑아두면 정형 데이터로 옮길 수 있으므로(perf_table.py),
표 영역과 본문 텍스트를 나눠서 담는다.
"""

from pathlib import Path

import fitz  # PyMuPDF

try:
    from .base import Block
except ImportError:  # 스크립트로 직접 실행할 때
    from base import Block

# 논문 PDF는 임베디드 폰트 경고를 쏟아내지만 텍스트 추출에는 지장이 없다.
fitz.TOOLS.mupdf_display_errors(False)

MIN_TABLE_ROWS = 2
MIN_TABLE_COLS = 2


def _table_text(rows: list[list]) -> str:
    out = []
    for row in rows:
        cells = ["" if c is None else " ".join(str(c).split()) for c in row]
        if any(cells):
            out.append(" | ".join(cells).rstrip(" |"))
    return "\n".join(out)


def parse_pdf(path: str | Path) -> list[Block]:
    blocks: list[Block] = []

    with fitz.open(path) as doc:
        for page_no, page in enumerate(doc, start=1):
            loc = f"p.{page_no}"

            try:
                found = page.find_tables()
                tables = list(found.tables)
            except Exception:  # 표 탐색은 실패해도 본문 추출은 계속한다
                tables = []

            rects = []
            for t in tables:
                rows = t.extract()
                if len(rows) < MIN_TABLE_ROWS or max(map(len, rows)) < MIN_TABLE_COLS:
                    continue
                text = _table_text(rows)
                if not text:
                    continue
                rects.append(fitz.Rect(t.bbox))
                blocks.append(Block(len(blocks) + 1, "table", text, locator=loc))

            # 표 영역과 겹치지 않는 텍스트만 본문으로 담는다(중복 방지)
            parts = []
            for b in page.get_text("blocks"):
                rect, text = fitz.Rect(b[:4]), b[4].strip()
                if not text or any(rect.intersects(r) for r in rects):
                    continue
                parts.append(text)

            if body := "\n".join(parts).strip():
                blocks.append(Block(len(blocks) + 1, "body", body, locator=loc))

    return blocks


if __name__ == "__main__":
    import sys
    from collections import Counter

    target = Path(sys.argv[1])
    blocks = parse_pdf(target)

    total_chars = sum(len(b.text) for b in blocks)
    with fitz.open(target) as doc:
        n_total = doc.page_count

    print(f"파일        : {target.name}")
    print(f"전체 페이지 : {n_total}")
    print(f"블록        : {len(blocks)}개 {dict(Counter(b.kind for b in blocks))}, {total_chars:,}자")
    print("-" * 70)

    for b in blocks[:4]:
        print(f"\n[{b.kind}/{b.locator}] {b.text[:350]}")
