"""한국어 텍스트 정규화 (Kiwi 형태소 분석기 기반).

두 가지 문제를 해결한다.

1. 띄어쓰기 복원
   PPT를 PDF로 변환한 문서는 공백 문자가 실제로 없어
   "효율적인건강관리를위한" 처럼 붙어서 추출된다.

2. BM25 토크나이저
   한국어는 조사가 붙어 있어("과제의", "과제를") 공백 기준 토큰화로는
   키워드 검색이 제대로 동작하지 않는다. 형태소 단위로 쪼갠다.
"""

from functools import lru_cache

from kiwipiepy import Kiwi

# 검색에 의미 있는 품사만 남긴다.
# 체언(NN*), 용언 어간(VV/VA), 외국어(SL), 숫자(SN), 한자(SH)
_SEARCH_TAGS = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN", "SH"}


@lru_cache(maxsize=1)
def _kiwi() -> Kiwi:
    """Kiwi 인스턴스는 초기화가 무거우므로 한 번만 만든다."""
    return Kiwi()


def restore_spacing(text: str) -> str:
    """붙어 쓴 한국어 문장에 띄어쓰기를 복원한다."""
    return _kiwi().space(text)


def tokenize(text: str) -> list[str]:
    """BM25 색인·질의에 쓸 형태소 토큰 목록."""
    return [
        tok.form.lower()
        for tok in _kiwi().tokenize(text)
        if tok.tag in _SEARCH_TAGS and len(tok.form) > 1
    ]


def spacing_ratio(text: str) -> float:
    """공백 비율. 이 값이 낮으면 띄어쓰기가 소실된 문서다.

    실측: 논문 PDF 0.171, 회의록 PPTX 0.103, PPT→PDF 변환본 0.048
    """
    if not text:
        return 0.0
    return text.count(" ") / len(text)


# 실측값 사이를 가르는 임계값. 이 아래면 띄어쓰기가 소실된 것으로 본다.
SPACING_THRESHOLD = 0.08


def normalize_document(texts: list[str]) -> tuple[list[str], bool]:
    """문서 단위로 띄어쓰기 소실을 판단하고 필요할 때만 복원한다.

    페이지마다 공백 비율 편차가 크므로(영문 비중 등) 판단은 문서 전체로 한다.
    반환값은 (정규화된 텍스트 목록, 복원 수행 여부).
    """
    joined = " ".join(texts)
    if spacing_ratio(joined) >= SPACING_THRESHOLD:
        return texts, False
    return [restore_spacing(t) for t in texts], True


if __name__ == "__main__":
    samples = [
        "2022년도 효율적인건강관리를위한현장지원기술개발",
        "IoT, AI 기반생체신호분석플랫폼개발사업",
        "인공지능기반빅데이터분석을활용한맞춤형건강기록획득시스템",
    ]
    for s in samples:
        restored = restore_spacing(s)
        print(f"원본  : {s}")
        print(f"복원  : {restored}")
        print(f"토큰  : {tokenize(s)}")
        print("-" * 70)
