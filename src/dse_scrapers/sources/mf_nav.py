"""Mutual fund NAV, parsed out of DSE's news archive.

There is no NAV page on dsebd.org. Funds disclose it as news, and the
archive search behind `news_archive.php` is `old_news.php`:

    old_news.php?startDate=&endDate=&criteria=4&archive=news   all instruments
    old_news.php?inst=<CODE>&criteria=3&archive=news           one instrument

The date-range form returns every instrument at once, so a whole window costs
one request. Each disclosure reads:

    On the close of operation on 12-Aug-2026, the Fund has reported Net Asset
    Value (NAV) of Tk. 6.56 per unit on the basis of current market price and
    Tk. 11.51 per unit on the basis of cost price against face value of Tk.
    10.00 whereas total Net Assets of the Fund stood at Tk. 1,901,929,999.00
    on the basis of current market price and Tk. 3,336,204,828.00 on the
    basis of cost price ...

Note the two bases appear **twice** — once per unit, once as total net assets.
The per-unit figures are matched on that qualifier so the fund-level totals
can never be picked up instead. The two are matched independently rather than
in one pattern, so the sentence order does not matter.

Titles vary ("Daily NAV", "Mutual Fund Daily NAV") and cannot be used to
identify these items anyway — NAVANAPHAR's ticker contains "NAV". A body that
parses is the test.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import requests

from ..http import get_text
from ._html import clean, main_content, number
from ._news import ITEM, OLD_NEWS_URL

log = logging.getLogger(__name__)

# Funds disclose the day after the close being reported, so the window has to
# run past the requested day to catch it.
POSTING_LAG_DAYS = 7
# How far back to look for a fund that has not reported recently.
DEFAULT_LOOKBACK_DAYS = 30

_AS_ON = re.compile(r"close of operation on\s*(\d{1,2}-[A-Za-z]{3}-\d{4})", re.IGNORECASE)
_MARKET_NAV = re.compile(
    r"Tk\.?\s*([\d,]+\.?\d*)\s*per unit on the basis of current market price", re.IGNORECASE
)
_COST_NAV = re.compile(
    r"Tk\.?\s*([\d,]+\.?\d*)\s*per unit on the basis of cost price", re.IGNORECASE
)


@dataclass(frozen=True)
class NavDisclosure:
    ticker: str
    as_on: dt.date
    cost_nav: float | None
    market_nav: float | None
    posted: dt.date


def parse_disclosures(page: str) -> list[NavDisclosure]:
    """Every NAV disclosure on an old_news.php page."""
    text = clean(main_content(page))
    disclosures = []

    for item in ITEM.finditer(text):
        body = item["body"]
        as_on = _AS_ON.search(body)
        market = _MARKET_NAV.search(body)
        cost = _COST_NAV.search(body)
        if not as_on or not (market or cost):
            continue  # not a NAV disclosure
        try:
            as_on_date = dt.datetime.strptime(as_on.group(1), "%d-%b-%Y").date()
        except ValueError:
            log.debug("Unparsable NAV date", extra={"value": as_on.group(1)})
            continue

        disclosures.append(
            NavDisclosure(
                ticker=item["code"].strip(),
                as_on=as_on_date,
                cost_nav=number(cost.group(1)) if cost else None,
                market_nav=number(market.group(1)) if market else None,
                posted=dt.date.fromisoformat(item["posted"]),
            )
        )

    return disclosures


def fetch_disclosures(
    session: requests.Session,
    trading_day: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[NavDisclosure]:
    """NAV disclosures posted around `trading_day`, in one request."""
    start = trading_day - dt.timedelta(days=lookback_days)
    end = trading_day + dt.timedelta(days=POSTING_LAG_DAYS)
    page = get_text(
        session,
        OLD_NEWS_URL,
        params={
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "criteria": 4,
            "archive": "news",
        },
    )
    disclosures = parse_disclosures(page)
    log.debug(
        "Fetched NAV disclosures",
        extra={"count": len(disclosures), "from": start.isoformat(), "to": end.isoformat()},
    )
    return disclosures


def latest_on_or_before(
    disclosures: list[NavDisclosure], trading_day: dt.date
) -> dict[str, NavDisclosure]:
    """Each fund's most recent disclosure as at `trading_day`.

    Funds that skip a day — or report weekly — carry their last published NAV
    forward, which is why the sheet states the date each figure is as of.
    """
    latest: dict[str, NavDisclosure] = {}
    for disclosure in disclosures:
        if disclosure.as_on > trading_day:
            continue
        current = latest.get(disclosure.ticker)
        if current is None or disclosure.as_on > current.as_on:
            latest[disclosure.ticker] = disclosure
    return latest
