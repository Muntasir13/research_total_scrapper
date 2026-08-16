"""Shared parsing helpers for dsebd.org pages.

Every page ships the same ~274 KB site shell — nav, sidebar, and a select
holding all instrument codes. The content is a small slice in the middle.
"""

from __future__ import annotations

import html
import re

_CONTENT_START = 'id="RightBody"'
_CONTENT_END = 'class="containerFot"'

# Stops at the quote or tag boundary, not at "&" — KAY&QUE is a real trading
# code, and a stricter class silently truncates it to KAY.
_CODE = re.compile(r"displayCompany\.php\?name=([^'\"<>]+)")

# Table structure. Shared because every dsebd.org and cdbl.com.bd page that
# carries data carries it in a plain <table>, and hand-parsing rows is only
# safe where colspan and rowspan are absent — where they are not, use pandas.
TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.DOTALL | re.IGNORECASE)
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)


def main_content(page: str) -> str:
    """The page's own content, with the shared shell trimmed away."""
    start = page.find(_CONTENT_START)
    if start == -1:
        return page
    end = page.find(_CONTENT_END, start)
    return page[start:end] if end != -1 else page[start:]


def clean(fragment: str) -> str:
    """Tag-stripped, entity-decoded, whitespace-collapsed text."""
    return re.sub(
        r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    ).strip()


def trading_codes(page: str) -> list[str]:
    """Every trading code linked from a page, in document order."""
    return [html.unescape(code).strip() for code in _CODE.findall(main_content(page))]


def number(text: str) -> float:
    """A figure DSE printed with thousands separators. Raises on anything else.

    For cells that are known to hold a number. Where a blank or a placeholder
    is expected instead, the caller wants its own None-returning parse.
    """
    return float(text.replace(",", ""))
