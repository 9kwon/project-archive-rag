"""HWPX 파서: 한글 문서에서 문단·제목·표를 구조를 살려 추출한다.

HWPX는 ZIP 안에 OWPML(XML)이 들어 있는 형식이다.

PDF와 달리 페이지 번호가 없다(레이아웃 시점에 결정되므로).
대신 문서의 제목 계층을 추출해 "4차년도 연차보고서 > 2. 연구개발 수행내용"
같은 경로를 출처로 사용한다. 원문에서 찾기에는 오히려 이쪽이 낫다.
"""

import re
import zipfile
from pathlib import Path

from lxml import etree

try:
    from .base import Block
except ImportError:  # 스크립트로 직접 실행할 때
    from base import Block

NS = {
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
}

# 한글 보고서의 제목 계층은 스타일이 아니라 글머리 번호로 드러난다.
# 아래 두 단계까지만 제목(문서 구조)으로 보고, 그보다 하위 기호
# (○, -, >)는 본문 내용으로 둔다.
_HEADING_PATTERNS = [
    (1, re.compile(r"^(제?\s?\d+\s?[.장]|[IVX]+\.)\s*\S")),  # 1. / 제1장 / Ⅱ.
    (2, re.compile(r"^([□■◇◆]|\d+\)|[가-하]\.)\s*\S")),  # □ / 1) / 가.
]
# 그림·표 캡션은 제목이 아니다.
_CAPTION = re.compile(r"^(그림|표|Fig|Table)\s")

# 보고서는 기관별로 꼭지가 나뉜다. 아래 마커가 나오면 이후 블록은 그 기관 소관이다.
#   [공동연구개발기관명: OO연구원]  [주관연구개발기관 : (기관명)]  [공동] OO연구원
# 기관명↔코드 매핑은 config의 project.organizations에서 읽는다 (src/project.py).
_ORG_MARKER = re.compile(r"\[(?:주관|공동|위탁)(?:연구개발기관)?\d*\s*(?:명)?\s*[:\]]\s*[(\s]*([^)\]\n]{0,40})")


def _normalize_org(raw: str) -> str | None:
    """기관명을 짧은 코드로 정규화한다.

    빈 양식의 자리표시자("(기관명)", "주관연구개발기관명 입력")는 기관이 아니므로
    None을 돌려 직전 기관을 유지하게 한다.
    """
    try:
        from project import get_profile
    except ImportError:  # src가 경로에 없을 때 (파서 단독 사용)
        get_profile = None

    text = raw.strip()
    # 한 글자짜리는 "[주관연구개발기관의 장]" 같은 표현에서 잘못 잡힌 것이다
    if len(text) < 2 or "입력" in text or text in ("기관명", "(기관명)"):
        return None
    if get_profile and (code := get_profile().org_of_text(text)):
        return code
    return text


def _heading_level(text: str) -> int:
    """글머리 번호로 제목 깊이를 판정한다. 제목이 아니면 0."""
    if len(text) > 80 or _CAPTION.match(text):
        return 0
    for level, pat in _HEADING_PATTERNS:
        if pat.match(text):
            return level
    return 0


def _text_of(el, skip_tables: bool = False) -> str:
    """요소 아래 hp:t 텍스트를 이어 붙인다.

    skip_tables=True면 하위 표 안의 텍스트는 제외한다. 문단이 표를 품고 있을 때
    (한글 문서에서는 흔하다) 표 내용이 문단 텍스트로도 중복 추출되는 것을 막는다.
    """
    t_tag, tbl_tag = f"{{{NS['hp']}}}t", f"{{{NS['hp']}}}tbl"
    if not skip_tables:
        return "".join(t.text or "" for t in el.iter(t_tag))

    parts = []
    for t in el.iter(t_tag):
        cur, in_table = t.getparent(), False
        while cur is not None and cur is not el:
            if cur.tag == tbl_tag:
                in_table = True
                break
            cur = cur.getparent()
        if not in_table:
            parts.append(t.text or "")
    return "".join(parts)


def _table_text(tbl) -> str:
    """표를 격자로 복원해 행마다 ' | ' 로 이은 문자열로 만든다.

    한글 표는 병합 셀이 흔하고(성과지표 표는 헤더가 3행에 걸쳐 있다),
    셀을 순서대로 나열하면 열이 어긋난다. cellAddr(위치)와 cellSpan(병합 범위)로
    실제 격자를 복원해야 "4차년도 목표" 같은 값이 올바른 열에 놓인다.
    """
    hp = NS["hp"]
    n_row, n_col = int(tbl.get("rowCnt", 0)), int(tbl.get("colCnt", 0))
    if not n_row or not n_col:
        return ""

    grid = [[""] * n_col for _ in range(n_row)]
    for tc in tbl.iter(f"{{{hp}}}tc"):
        addr, span = tc.find(f"{{{hp}}}cellAddr"), tc.find(f"{{{hp}}}cellSpan")
        if addr is None:
            continue
        r, c = int(addr.get("rowAddr")), int(addr.get("colAddr"))
        rs = int(span.get("rowSpan", 1)) if span is not None else 1
        cs = int(span.get("colSpan", 1)) if span is not None else 1
        text = _text_of(tc).strip().replace("\n", " ")
        # 세로 병합은 값을 아래로 이어 붙인다. 행 단위로 읽어도 뜻이 통해야 한다.
        for i in range(r, min(r + rs, n_row)):
            if 0 <= c < n_col:
                grid[i][c] = text
        # 가로 병합은 반복하지 않는다(헤더가 불필요하게 늘어난다).

    rows = [" | ".join(row).rstrip(" |") for row in grid]
    return "\n".join(r for r in rows if r.strip())


def parse_hwpx(path: str | Path) -> list[Block]:
    """문서를 DOM 순서대로 훑어 제목·본문·표 블록으로 나눈다.

    정부 양식 한글 문서는 바깥 표가 레이아웃 컨테이너 역할을 한다
    (표 하나가 본문 전체를 감싸는 경우도 있다). 그래서 표를 만나면
    무조건 표로 처리하지 않고, 중첩 표가 없는 말단 표만 데이터 표로 본다.
    레이아웃 표는 그냥 통과해서 내부 문단을 개별 블록으로 만든다.
    """
    p_tag, tbl_tag = f"{{{NS['hp']}}}p", f"{{{NS['hp']}}}tbl"

    with zipfile.ZipFile(path) as z:
        sections = sorted(n for n in z.namelist() if n.startswith("Contents/section"))
        xmls = [z.read(n) for n in sections]

    blocks: list[Block] = []
    path_stack: list[tuple[int, str]] = []  # (level, title)
    org = ""  # 직전 기관 마커 이후의 내용은 그 기관 소관으로 본다
    idx = 0

    for xml in xmls:
        root = etree.fromstring(xml)

        # 말단 표: 내부에 다른 표가 없는 표. 이것만 데이터 표로 취급한다.
        leaf_tables = {t for t in root.iter(tbl_tag) if len(list(t.iter(tbl_tag))) == 1}
        # 말단 표 안의 문단은 표에서 함께 다루므로 따로 블록을 만들지 않는다.
        inside_leaf = {p for t in leaf_tables for p in t.iter(p_tag)}

        for el in root.iter(p_tag, tbl_tag):  # DOM 순서 유지
            if el.tag == tbl_tag:
                if el not in leaf_tables:
                    continue  # 레이아웃 표 — 통과하고 내부 문단을 개별 처리
                text = _table_text(el)
                if text:
                    idx += 1
                    blocks.append(
                        Block(idx, "table", text, locator=f"문단 {idx}",
                              heading_path=[t for _, t in path_stack], org=org)
                    )
                continue

            if el in inside_leaf:
                continue

            # 표를 품은 문단은 표 텍스트를 빼고 읽는다(표는 별도 블록으로 나온다)
            text = _text_of(el, skip_tables=True).strip()
            if not text:
                continue

            if m := _ORG_MARKER.search(text):
                if (found := _normalize_org(m.group(1))) is not None:
                    org = found

            idx += 1
            level = _heading_level(text)
            if level:
                while path_stack and path_stack[-1][0] >= level:
                    path_stack.pop()
                blocks.append(
                    Block(idx, "heading", text, locator=f"문단 {idx}", level=level,
                          heading_path=[t for _, t in path_stack], org=org)
                )
                path_stack.append((level, text))
            else:
                blocks.append(
                    Block(idx, "body", text, locator=f"문단 {idx}",
                          heading_path=[t for _, t in path_stack], org=org)
                )

    return blocks


if __name__ == "__main__":
    import sys
    from collections import Counter

    target = Path(sys.argv[1])
    blocks = parse_hwpx(target)
    kinds = Counter(b.kind for b in blocks)
    chars = sum(len(b.text) for b in blocks)

    print(f"파일   : {target.name}")
    print(f"블록   : {len(blocks)}개 ({dict(kinds)}), 총 {chars:,}자")
    print("-" * 70)

    print("\n=== 제목 계층 (앞 25개) ===")
    for b in [b for b in blocks if b.kind == "heading"][:25]:
        print(f"  {'  ' * (b.level - 1)}[{b.level}] {b.text[:60]}")

    print("\n=== 표 예시 ===")
    for b in [b for b in blocks if b.kind == "table"][:2]:
        print(f"  경로: {' > '.join(b.heading_path[-2:])}")
        for line in b.text.split("\n")[:4]:
            print(f"    {line[:100]}")
        print()
