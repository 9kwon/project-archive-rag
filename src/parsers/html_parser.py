"""HTML 파서: 리포트 HTML에서 제목·본문·표를 추출한다.

분석 결과를 HTML로 내보낸 리포트(sleep_analysis_report.html 등)가 대상이다.
script/style은 버리고, 표는 행 단위로 남긴다.
"""

from pathlib import Path

from lxml import html as lhtml

try:
    from .base import Block, heading_stack_push
except ImportError:
    from base import Block, heading_stack_push

_SKIP = {"script", "style", "noscript"}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_TEXT_TAGS = {"p", "li", "pre", "blockquote", "figcaption", "td", "th", "div", "span"}


def _clean(s: str) -> str:
    return " ".join(s.split())


def _table_text(tbl) -> str:
    rows = []
    for tr in tbl.iter("tr"):
        cells = [_clean(td.text_content()) for td in tr.iter("td", "th")]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def parse_html(path: str | Path) -> list[Block]:
    tree = lhtml.fromstring(Path(path).read_bytes())
    for el in tree.iter(*_SKIP):
        el.getparent().remove(el)

    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    seen: set = set()  # 표 안의 요소를 두 번 담지 않기 위한 표시
    idx = 0

    for el in tree.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if el in seen:
            continue

        if tag == "table":
            text = _table_text(el)
            seen.update(el.iter())
            if text:
                idx += 1
                blocks.append(
                    Block(idx, "table", text, locator=f"#{idx}", heading_path=[t for _, t in stack])
                )
            continue

        if tag in _HEADINGS:
            title = _clean(el.text_content())
            if title:
                level = _HEADINGS[tag]
                path_before = heading_stack_push(stack, level, title)
                idx += 1
                blocks.append(
                    Block(idx, "heading", title, locator=f"#{idx}", level=level,
                          heading_path=path_before)
                )
            seen.update(el.iter())
            continue

        if tag in _TEXT_TAGS:
            # 자식에 블록 요소가 있으면 그쪽에서 다룬다
            if any(c.tag in _TEXT_TAGS | set(_HEADINGS) | {"table"} for c in el if isinstance(c.tag, str)):
                continue
            text = _clean(el.text_content())
            if len(text) > 1:
                idx += 1
                blocks.append(
                    Block(idx, "body", text, locator=f"#{idx}", heading_path=[t for _, t in stack])
                )
                seen.update(el.iter())

    return blocks


if __name__ == "__main__":
    import sys
    from collections import Counter

    target = Path(sys.argv[1])
    blocks = parse_html(target)
    print(f"파일 : {target.name}")
    print(f"블록 : {len(blocks)}개 {dict(Counter(b.kind for b in blocks))}, "
          f"총 {sum(len(b.text) for b in blocks):,}자")
    print("-" * 70)
    for b in blocks[:14]:
        prefix = f"[{b.kind[:4]}]"
        print(f"  {prefix} {b.text[:110]}")
