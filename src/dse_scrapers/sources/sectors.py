"""Sector membership, from the sector directory and its per-sector boards.

`by_industrylisting.php` lists every sector with the area id its board hangs
off, and `ltp_industry.php?area=<id>` lists that sector's instruments —
all of them, traded that day or not. Two requests, versus a company page
per instrument.
"""

from __future__ import annotations

import logging
import re

import requests

from ..http import get_text
from ._html import CELL, ROW, clean, main_content, trading_codes

log = logging.getLogger(__name__)

INDUSTRY_LIST_URL = "https://www.dsebd.org/by_industrylisting.php"
SECTOR_BOARD_URL = "https://www.dsebd.org/ltp_industry.php"

_AREA = re.compile(r"ltp_industry\.php\?area=(\d+)")


def fetch_sector_areas(session: requests.Session) -> dict[str, str]:
    """Sector name to the area id of its board."""
    section = main_content(get_text(session, INDUSTRY_LIST_URL))
    areas: dict[str, str] = {}
    for row in ROW.findall(section):
        area = _AREA.search(row)
        if not area:
            continue
        cells = [clean(cell) for cell in CELL.findall(row)]
        # The sector name is the first cell that is not a number or a link label.
        name = next(
            (c for c in cells if c and not c.isdigit() and c.lower() != "more info"),
            None,
        )
        if name:
            areas.setdefault(name, area.group(1))
    return areas


def fetch_sector_constituents(session: requests.Session, sector: str) -> list[str]:
    """Every trading code in a named sector, e.g. "Mutual Funds"."""
    areas = fetch_sector_areas(session)
    area = areas.get(sector)
    if area is None:
        log.warning(
            "Sector not on the industry list",
            extra={"sector": sector, "available": sorted(areas)},
        )
        return []

    codes = sorted(set(trading_codes(get_text(session, SECTOR_BOARD_URL, params={"area": area}))))
    log.debug("Sector constituents", extra={"sector": sector, "count": len(codes)})
    return codes
