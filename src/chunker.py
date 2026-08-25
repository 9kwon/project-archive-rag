"""블록을 검색 단위(청크)로 묶는다.

두 가지 원칙을 지킨다.

1. 표는 쪼개지 않는다.
   성과지표 표는 행 하나만 떼면 "4차년도 목표 ≥15"가 무엇의 목표인지 알 수 없다.

2. 청크 앞에 제목 경로를 붙인다.
   "≥ 87%"라는 문장만으로는 검색되지 않지만,
   "3. 수행 결과 > 정량적 성과 / ≥ 87%"는 검색된다.
"""

import re
from dataclasses import dataclass, field

from parsers.base import Block

# 국가연구개발혁신법 별지 서식의 작성 안내문. 연구 내용이 아니므로 제외한다.
_BOILERPLATE = re.compile(
    r"기재합니다|기재하지|작성 요령|작성요령|제출하지 않습니다|작성합니다|"
    r"해당 시 작성|별지 제\d+호서식|직접 기재 불필요|표시합니다|"
    r"작성해야 합니다|기재해야|해당하는 경우에 한하여|안내글씨|"
    r"210mm|백상지|중질지|"  # 쪽 하단 용지 규격
    r"「[^」]+법」\s*제\d+조|"  # 기관유형 설명의 법령 나열
    r"상기와 같음을 확인|\(기관\s*인\)|청장\s*귀하|"  # 확인서의 서명·수신 문구
    r"날인합니다|전자서명"  # "20. 기관장 서명: …의 장의 전자서명을 날인합니다"
)
# 의미 없는 자리표시자·양식 잔여물
_PLACEHOLDER = re.compile(r"^[\s.·․\-|%()\[\]]*$|^\(.*입력\)$|^\(기관명\)$")

TARGET_CHARS = 600  # 청크 목표 길이. 한국어 기준 이 정도가 검색·생성 모두 무난하다.
MAX_CHARS = 1400  # 표가 이보다 크면 행 단위로 나눈다(헤더는 반복).
MIN_CHARS = 30  # 이보다 짧은 조각은 앞 청크에 붙인다.


@dataclass
class Chunk:
    text: str  # 임베딩·검색 대상 (제목 경로 포함)
    body: str  # 제목 경로를 뺀 본문
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.text)


def is_noise(text: str) -> bool:
    """양식 안내문·자리표시자인지 판정한다."""
    return bool(_BOILERPLATE.search(text) or _PLACEHOLDER.match(text.strip()))


def _split_text(text: str, limit: int) -> list[str]:
    """긴 본문을 문장 경계에서 나눈다."""
    if len(text) <= limit:
        return [text]
    # look-behind는 고정 폭이어야 한다. 한국어 종결어미도 마침표로 끝나므로
    # 문장부호 한 글자만 보면 충분하다.
    sentences = re.split(r"(?<=[.。!?])\s+|\n", text)
    out, cur, size = [], [], 0
    for s in sentences:
        if not s:
            continue
        if cur and size + len(s) > limit:
            out.append(" ".join(cur))
            cur, size = [], 0
        # 문장 하나가 상한을 넘으면 통째로 자른다
        while len(s) > limit:
            out.append(s[:limit])
            s = s[limit:]
        cur.append(s)
        size += len(s) + 1
    if cur:
        out.append(" ".join(cur))
    return out


def _split_table(text: str, limit: int) -> list[str]:
    """큰 표를 행 단위로 나누되 첫 행(헤더)을 각 조각에 반복한다."""
    lines = text.split("\n")
    if len(lines) < 3:  # 행이 거의 없는데 긴 표 — 문자 단위로 자를 수밖에 없다
        return [text[i : i + limit] for i in range(0, len(text), limit)]
    header, rows = lines[0], lines[1:]
    out, cur = [], []
    size = len(header)
    for row in rows:
        if cur and size + len(row) > limit:
            out.append("\n".join([header, *cur]))
            cur, size = [], len(header)
        cur.append(row)
        size += len(row) + 1
    if cur:
        out.append("\n".join([header, *cur]))
    return out


def _prefix(heading_path: list[str], doc_name: str) -> str:
    """검색 문맥으로 붙일 제목 경로."""
    parts = [doc_name, *heading_path[-3:]]  # 너무 깊으면 하위 3단계만
    return " > ".join(p for p in parts if p)


def chunk_blocks(blocks: list[Block], doc_meta: dict, drop_noise: bool = True) -> list[Chunk]:
    """블록 목록을 청크로 묶는다."""
    doc_name = doc_meta.get("doc_name", "")
    chunks: list[Chunk] = []
    buf: list[Block] = []
    pending: list[str] = []  # 아직 본문을 만나지 못한 제목들

    def flush():
        nonlocal pending
        if not buf:
            return
        head = buf[0]
        body = "\n".join([*pending, *(b.text for b in buf)])
        pending = []
        prefix = _prefix(head.heading_path, doc_name)
        for piece in _split_text(body, MAX_CHARS):
            chunks.append(
                Chunk(
                    text=f"[{prefix}]\n{piece}" if prefix else piece,
                    body=piece,
                    meta={
                        **doc_meta,
                        "locator": head.locator,
                        "kind": head.kind,
                        "section": " > ".join(head.heading_path[-2:]),
                        # 블록에 기관 표시가 있으면 문서 기본값보다 우선한다
                        **({"org": head.org} if head.org else {}),
                    },
                )
            )
        buf.clear()

    for b in blocks:
        if drop_noise and is_noise(b.text):
            continue

        # 제목은 단독 청크로 만들지 않고 뒤따르는 본문·표 앞에 붙인다.
        # (제목 자체는 heading_path를 통해 이미 문맥으로 전달된다)
        if b.kind == "heading":
            flush()
            pending.append(b.text)
            continue

        if b.kind == "table":
            flush()
            lead = "\n".join(pending)
            pending.clear()
            pieces = _split_table(b.text, MAX_CHARS) if len(b.text) > MAX_CHARS else [b.text]
            for i, piece in enumerate(pieces):
                prefix = _prefix(b.heading_path, doc_name)
                suffix = f" ({i + 1}/{len(pieces)})" if len(pieces) > 1 else ""
                piece = f"{lead}\n{piece}" if (lead and i == 0) else piece
                chunks.append(
                    Chunk(
                        text=f"[{prefix}{suffix}]\n{piece}" if prefix else piece,
                        body=piece,
                        meta={
                            **doc_meta,
                            "locator": b.locator,
                            "kind": "table",
                            "section": " > ".join(b.heading_path[-2:]),
                            **({"org": b.org} if b.org else {}),
                        },
                    )
                )
            continue

        # 제목이 바뀌면 새 청크를 시작한다(문맥이 섞이지 않게)
        if buf and buf[0].heading_path != b.heading_path:
            flush()

        buf.append(b)
        if sum(len(x.text) for x in buf) >= TARGET_CHARS:
            flush()

    flush()

    # 너무 짧은 청크는 앞에 붙이고, 붙일 곳이 없으면 버린다.
    # ("▶ 유의사항", "00 | 년 | 00 | 월" 같은 조각은 단독으로 남으면
    #  제목 접두어의 질의어 밀도 때문에 검색 상위를 오염시킨다)
    merged: list[Chunk] = []
    for c in chunks:
        if len(c.body) >= MIN_CHARS:
            merged.append(c)
        elif merged and merged[-1].meta.get("kind") != "table":
            prev = merged[-1]
            prev.body += "\n" + c.body
            prev.text += "\n" + c.body
        # else: 표 뒤의 고아 조각 — 버린다
    return merged


if __name__ == "__main__":
    import sys
    from collections import Counter
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from metadata import extract
    from parsers import parse

    target = Path(sys.argv[1])
    blocks = parse(target)
    meta = extract(target)
    chunks = chunk_blocks(blocks, meta.as_dict())

    raw = sum(len(b.text) for b in blocks)
    kept = sum(len(c.body) for c in chunks)
    print(f"파일   : {target.name}")
    print(f"블록   : {len(blocks)}개 {raw:,}자")
    print(f"청크   : {len(chunks)}개 {kept:,}자 (노이즈 {raw - kept:,}자 제거)")
    print(f"종류   : {dict(Counter(c.meta.get('kind') for c in chunks))}")
    lens = [len(c.body) for c in chunks]
    if lens:
        print(f"길이   : 중앙값 {sorted(lens)[len(lens) // 2]}자, 최대 {max(lens)}자")
    print("-" * 78)
    for c in chunks[:4]:
        print(f"\n--- {c.meta.get('kind')} / {c.meta.get('locator')} / org={c.meta.get('org', '-')}")
        print(c.text[:420])
