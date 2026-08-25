"""문서 형식별 파서. 모두 list[Block]을 돌려준다."""

from pathlib import Path

from .base import Block
from .hwpx_parser import parse_hwpx
from .html_parser import parse_html
from .md_parser import parse_md
from .pdf_parser import parse_pdf
from .pptx_parser import parse_pptx
from .xlsx_parser import parse_xlsx

PARSERS = {
    ".pdf": parse_pdf,
    ".hwpx": parse_hwpx,
    ".pptx": parse_pptx,
    ".xlsx": parse_xlsx,
    ".md": parse_md,
    ".html": parse_html,
    ".htm": parse_html,
}


def parse(path: str | Path) -> list[Block]:
    """확장자를 보고 알맞은 파서로 넘긴다."""
    path = Path(path)
    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        raise ValueError(f"지원하지 않는 형식: {path.suffix} ({path.name})")
    return parser(path)


__all__ = ["Block", "parse", "PARSERS"]
