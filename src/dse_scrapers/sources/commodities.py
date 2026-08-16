"""Commodity quotes from tradingeconomics.com/commodities.

One request serves the lot. Trading Economics renders every commodity it
tracks into server-side HTML on a single page — grouped into Energy, Metals,
Agricultural, Industrial, Livestock, Index and Electricity tables, but all
sharing one row shape:

    <tr data-symbol="CO1:COM" data-decimals="3">
      <td class="datatable-item-first">
        <a href="/commodity/brent-crude-oil"><b>Brent</b></a>
        <div style='font-size: 10px;'>USD/Bbl</div>
      </td>
      <td id="p">88.338</td>
      <td id="nch" data-value="1.2683">1.268</td>
      <td id="pch" data-value="1.46">1.46%</td>
      ...four heatmap cells: weekly, monthly, YTD, YoY...
      <td id="date">Aug/14</td>
    </tr>

so there is no need to visit the 13 individual commodity pages.

Two things to watch:

**The date cell carries no year** — it is always `Mon/DD`. The year is
inferred, rolling back one when the result would land in the future, which is
what a stale December quote read in January needs.

**Rows are dated per commodity, not per page.** Trading Economics stamps each
row with its own last observation, so the actively traded futures show today
while the assessed physical benchmarks — Coal, LNG JKM, Iron Ore — sit a day
behind. `Change` is the move into that row's own date, not a common session,
which is why every row carries its date.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import requests

from ..http import BROWSER_UA, get_text
from ._html import clean

log = logging.getLogger(__name__)

COMMODITIES_URL = "https://tradingeconomics.com/commodities"

# A quote dated further ahead than this belongs to an earlier year.
FUTURE_TOLERANCE_DAYS = 2

# How far back to look for a year that makes the day a real past date.
MAX_YEARS_BACK = 5

_ROW = re.compile(r'<tr data-symbol="([^"]*)"(.*?)</tr>', re.DOTALL)
_NAME = re.compile(r'<a href="/commodity/([^"]+)">\s*<b>(.*?)</b>', re.DOTALL)
_UNIT = re.compile(r"<div style='font-size: 10px;'>(.*?)</div>", re.DOTALL)
_PRICE = re.compile(r'<td id="p"[^>]*>(.*?)</td>', re.DOTALL)
_CHANGE = re.compile(r'<td id="nch"[^>]*data-value="([^"]*)"', re.DOTALL)
_PERCENT = re.compile(r'<td id="pch"[^>]*data-value="([^"]*)"', re.DOTALL)
_DATE = re.compile(r'<td id="date"[^>]*>(.*?)</td>', re.DOTALL)


@dataclass(frozen=True)
class CommodityQuote:
    name: str
    slug: str
    unit: str
    as_of: dt.date | None
    price: float | None
    change: float | None
    change_pct: float | None

    @property
    def previous(self) -> float | None:
        """The prior observation, backed out of the published change."""
        if self.price is None or self.change is None:
            return None
        return round(self.price - self.change, 6)


def _number(text: str | None) -> float | None:
    """A price or change cell as a float, tolerating thousands separators."""
    if not text:
        return None
    try:
        return float(text.replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _as_of(cell: str, today: dt.date) -> dt.date | None:
    """`Aug/14` against the most recent year that makes it a real past date.

    Walk back from the current year until the day is both a valid date and not
    in the future. One step covers the ordinary case — a stale December quote
    read in January. The further steps matter only for a 29 February, which is
    a real date one year in four.
    """
    match = re.match(r"([A-Za-z]{3})/(\d{1,2})$", cell.strip())
    if not match:
        return None
    try:
        month = dt.datetime.strptime(match.group(1), "%b").month
    except ValueError:
        return None
    day = int(match.group(2))

    for year in range(today.year, today.year - MAX_YEARS_BACK, -1):
        try:
            parsed = dt.date(year, month, day)
        except ValueError:  # 29 February outside a leap year
            continue
        if (parsed - today).days <= FUTURE_TOLERANCE_DAYS:
            return parsed
    return None


def fetch_quotes(session: requests.Session) -> dict[str, CommodityQuote]:
    """Every commodity on the page, keyed by its Trading Economics name."""
    page = get_text(
        session,
        COMMODITIES_URL,
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    today = dt.date.today()
    quotes: dict[str, CommodityQuote] = {}

    for symbol, body in _ROW.findall(page):
        named = _NAME.search(body)
        if named is None:  # header and spacer rows carry no commodity link
            continue

        name = clean(named.group(2))
        unit = _UNIT.search(body)
        price = _PRICE.search(body)
        change = _CHANGE.search(body)
        percent = _PERCENT.search(body)
        date_cell = _DATE.search(body)

        quote = CommodityQuote(
            name=name,
            slug=named.group(1),
            unit=clean(unit.group(1)) if unit else "",
            as_of=_as_of(clean(date_cell.group(1)), today) if date_cell else None,
            price=_number(clean(price.group(1))) if price else None,
            change=_number(change.group(1)) if change else None,
            change_pct=_number(percent.group(1)) if percent else None,
        )

        if name in quotes:
            log.warning(
                "Duplicate commodity name on the page",
                extra={"name": name, "symbol": symbol, "kept": quotes[name].slug},
            )
            continue
        quotes[name] = quote

    log.debug("Parsed the commodities page", extra={"rows": len(quotes)})
    return quotes
