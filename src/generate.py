"""생성: 검색된 근거로 출처가 달린 답변을 만든다.

LLM 호출부는 provider로 추상화되어 있다.
- ollama    : 로컬/내부망 (기본). 원문이 외부로 나가지 않는다.
- anthropic : 상용 API. 보안 승인이 있을 때만 config에서 전환한다.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from project import get_profile
from search import Hit, get_searcher

# 과제 구조(연차↔연도↔단계 매핑, 참여기관)는 config의 project 섹션에서 만든다.
# 이 블록이 없으면 LLM이 표의 열 이름("2단계 1차연도(2025)")과 질문의 표현
# ("4차년도")을 잇지 못해 누적값("계") 열을 집는 오독이 생긴다.
_P = get_profile()

SYSTEM_PROMPT = f"""당신은 {_P.title}({_P.period}, {_P.n_years}개년)의 문서를 근거로 답하는 조수다.

{_P.prompt_context()}

규칙:
0. "성과 표에서 추출한 정형 데이터"가 함께 제공되면 수치는 그 값을 우선 사용한다.
   원문 표와 정형 데이터가 다르면 정형 데이터를 따르되, 차이가 있다는 점을 언급한다.
1. 반드시 아래 제공된 근거 자료만으로 답한다. 근거에 없는 내용은 "제공된 자료에서 확인되지 않습니다"라고 답한다.
   추세·경향을 근거로 한 예상, 추정, 외삽은 절대 하지 않는다. 자료에 없으면 없다고만 한다.
2. 답변의 각 핵심 사실 뒤에 근거 번호를 [1], [2] 형식으로 단다.
3. 계획(예정, 목표)과 실제 수행 결과를 구분해서 서술한다.
4. 근거들 사이에 수치나 내용이 충돌하면 하나를 고르지 말고 충돌 사실을 명시한다.
5. 회의록은 중간 논의라 확정 사실이 아닐 수 있다 — 회의록만 근거일 때는 그 점을 밝힌다.
6. 간결하게, 한국어로 답한다."""


def _build_context(hits: list[Hit]) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        m = h.meta
        tag = f"{m.get('doc_type', '')} · {h.source()}"
        if y := m.get("proj_year"):
            tag = f"{y}차년도 {tag}"
        parts.append(f"[{i}] ({tag})\n{h.text}")
    return "\n\n".join(parts)


class OllamaLLM:
    def __init__(self, cfg: dict):
        c = cfg["llm"]
        self.url = c.get("base_url", "http://localhost:11434").rstrip("/")
        self.model = c["model"]
        self.options = {
            "num_ctx": c.get("num_ctx", 8192),
            "temperature": c.get("temperature", 0.2),
        }

    def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": self.options,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                return json.loads(res.read())["message"]["content"]
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Ollama에 연결할 수 없습니다({self.url}). "
                "Ollama가 실행 중인지, 모델을 pull했는지 확인하세요."
            ) from e


class AnthropicLLM:
    """보안 승인 후 config의 provider를 anthropic으로 바꾸면 사용된다."""

    def __init__(self, cfg: dict):
        import anthropic  # 필요할 때만 임포트 (환경에 없어도 ollama는 동작)

        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
        self.model = cfg["llm"].get("model", "claude-sonnet-5")
        self.temperature = cfg["llm"].get("temperature", 0.2)

    def chat(self, system: str, user: str) -> str:
        res = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return res.content[0].text


def load_llm(cfg: dict):
    provider = cfg["llm"].get("provider", "ollama")
    return {"ollama": OllamaLLM, "anthropic": AnthropicLLM}[provider](cfg)


# 수치를 묻는 질문의 신호. 이때는 표에서 뽑아둔 정형 데이터를 함께 넣는다.
_METRIC_Q = re.compile(
    r"목표|실적|달성|성과|몇\s*건|몇\s*편|정확도|지표|건수|논문|특허|비교|추이|변화"
)


def _perf_context(query: str, cfg: dict) -> str:
    """질문에 걸린 연차의 정형 성과 요약을 만든다.

    표를 LLM이 직접 읽으면 열을 잘못 짚는 일이 잦다("계" 누적값을 특정 연차로 오독).
    정형 데이터를 함께 주면 그 오류가 사라진다.
    """
    if not _METRIC_Q.search(query):
        return ""
    try:
        import perf_query as pq

        con = pq.connect(cfg)
    except (FileNotFoundError, ImportError):
        return ""

    # 질문이 특정 지표를 가리키면 그 지표의 연차별 이력만 넣는다.
    # 연차 전체 요약을 통째로 주면 LLM이 비슷한 이름의 다른 지표를 집어온다.
    if names := pq.match_indicators(con, query):
        parts = [pq.format_indicator(n, pq.indicator_history(con, n)) for n in names]
        con.close()
        return "\n\n".join(parts)

    years = sorted({int(m) for m in re.findall(r"([1-9])차\s?년도", query)})
    years = [y for y in years if y <= _P.n_years]
    if not years:  # 연차를 안 밝히고 비교를 물으면 전체를 넣는다
        years = list(_P.years) if re.search(r"비교|추이|변화|연차별", query) else []
    if not years:
        con.close()
        return ""

    parts = [pq.format_summary(pq.year_summary(con, y)) for y in years]
    con.close()
    return "\n\n".join(parts)


def ask(query: str, filters: dict | None = None, k: int = 8) -> tuple[str, list[Hit]]:
    """질문 → (답변, 사용한 근거 목록)"""
    searcher = get_searcher()
    hits = searcher.search(query, k=k, filters=filters)
    if not hits:
        return "검색 결과가 없습니다.", []

    blocks = []
    if perf := _perf_context(query, searcher.cfg):
        blocks.append(
            "성과 표에서 추출한 정형 데이터 (수치는 이 값을 우선 사용할 것):\n\n" + perf
        )
    blocks.append("근거 자료:\n\n" + _build_context(hits))
    blocks.append(f"질문: {query}")

    llm = load_llm(searcher.cfg)
    answer = llm.chat(SYSTEM_PROMPT, "\n\n".join(blocks))
    return answer, hits


def _print_answer(answer: str, hits: list[Hit]):
    print("\n" + "=" * 78)
    print(answer)
    print("\n" + "-" * 78)
    print("근거:")
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] {h.source()}")


if __name__ == "__main__":
    from search import DOC_WEIGHT

    if len(sys.argv) > 1:  # 단발 질문
        answer, hits = ask(" ".join(sys.argv[1:]))
        _print_answer(answer, hits)
        sys.exit(0)

    # 대화형. search.py의 repl과 같은 필터 문법을 쓴다.
    print("근거 기반 질의응답 (종료: q 또는 빈 줄)")
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

        try:
            answer, hits = ask(q, filters)
        except RuntimeError as e:
            print(f"\n오류: {e}\n")
            continue
        _print_answer(answer, hits)
        print()
