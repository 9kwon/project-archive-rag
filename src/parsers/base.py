"""모든 파서가 공유하는 출력 형식.

형식마다 위치를 가리키는 방식이 다르다(PDF는 페이지, PPTX는 슬라이드,
XLSX는 시트, HWPX는 문단 순번). 이를 locator 문자열 하나로 통일해
답변에 출처를 표시할 때 그대로 쓴다.
"""

from dataclasses import dataclass, field


@dataclass
class Block:
    """문서를 이루는 한 덩어리."""

    index: int  # 문서 내 순번
    kind: str  # heading | body | table
    text: str
    locator: str = ""  # "p.12", "슬라이드 3", "Sheet1" 등 사람이 찾아갈 위치
    level: int = 0  # heading일 때 깊이
    heading_path: list[str] = field(default_factory=list)  # 소속 제목 경로
    org: str = ""  # 소관 기관 코드 (config project.organizations의 키)


def heading_stack_push(
    stack: list[tuple[int, str]], level: int, title: str
) -> list[str]:
    """제목 스택을 갱신하고, 갱신 직전의 상위 경로를 돌려준다."""
    while stack and stack[-1][0] >= level:
        stack.pop()
    path = [t for _, t in stack]
    stack.append((level, title))
    return path
