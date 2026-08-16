"""Index levels — today and the previous close — from three sources.

**Bangladesh** comes off the DSE homepage. Its index graphs are driven by
intraday series embedded inline as `index_value_*` JavaScript variables, one
per index. The first point of a series is the *previous* close, seeded at
09:59 before trading opens, and the last is the current level. Verified
against `recent_market_information.php`: for DSEX, DSES and DS30 the first
point equals the prior day's published close exactly, which is what licenses
using the same trick for CDSET and DSMEX — neither appears in any daily table.

Note DSEX's variable is `index_value_dsbi`, after the old "DSE Broad Index".

**International** comes from investing.com, whose pages embed a `__NEXT_DATA__`
JSON payload holding the instrument's price object. The previous close is
derived as `last - change` rather than read from `lastClose`: once a market
shuts for the day investing.com rolls `lastClose` forward to *that* close, so
it starts reporting the same number as `last`. Checked across 13 indices —
the two agree wherever `lastClose` has not rolled, and the subtraction stays
correct where it has.

**Sri Lanka** comes from the Colombo exchange itself. investing.com resolves
`cse-all-share` but serves nothing but zeros for it, whereas cse.lk publishes
a small JSON API.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass

import requests

from ..http import BROWSER_UA, get_text

log = logging.getLogger(__name__)

DSE_HOME_URL = "https://www.dsebd.org/index.php"
INVESTING_URL = "https://www.investing.com/indices/{slug}"
CSE_ASPI_URL = "https://www.cse.lk/api/aspiData"

# Courtesy pause between investing.com requests; its pages are ~1.4 MB each.
REQUEST_SPACING_SECONDS = 1.0

# JS variable per index on the DSE homepage.
DSE_SERIES_VARIABLES = {
    "DSEX": "index_value_dsbi",
    "DS30": "index_value_ds30",
    "DSES": "index_value_dses",
    "CDSET": "index_value_cdset",
    "DSMEX": "index_value_dsmex",
}

_SERIES_POINT = re.compile(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2},([\d.]+)")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


@dataclass(frozen=True)
class IndexQuote:
    name: str
    country: str
    as_of: dt.date | None
    today: float | None
    yesterday: float | None


def _series_variable(page: str, variable: str) -> str | None:
    """The concatenated string literals assigned to a JS variable."""
    match = re.search(re.escape(variable) + r"\s*=\s*((?:\s*\+?\s*\"[^\"]*\")+)", page, re.DOTALL)
    return match.group(1) if match else None


def fetch_dse_indices(session: requests.Session) -> list[IndexQuote]:
    """DSEX, DS30, DSES, CDSET and DSMEX for the latest DSE session."""
    page = get_text(session, DSE_HOME_URL)
    quotes = []

    for name, variable in DSE_SERIES_VARIABLES.items():
        assigned = _series_variable(page, variable)
        points = _SERIES_POINT.findall(assigned) if assigned else []
        if not points:
            log.warning("No intraday series on the DSE homepage", extra={"index": name})
            quotes.append(IndexQuote(name, "Bangladesh", None, None, None))
            continue

        session_date = dt.date.fromisoformat(points[-1][0])
        quotes.append(
            IndexQuote(
                name=name,
                country="Bangladesh",
                as_of=session_date,
                today=float(points[-1][1]),
                yesterday=float(points[0][1]),
            )
        )

    return quotes


def _as_of_from_epoch_ms(value) -> dt.date | None:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return dt.datetime.fromtimestamp(milliseconds / 1000, dt.timezone.utc).date()


def fetch_investing_index(
    session: requests.Session, name: str, country: str, slug: str
) -> IndexQuote:
    """One index from investing.com."""
    try:
        page = get_text(
            session, INVESTING_URL.format(slug=slug), headers={"User-Agent": BROWSER_UA}
        )
        payload = _NEXT_DATA.search(page)
        if payload is None:
            raise ValueError("no __NEXT_DATA__ payload")
        price = json.loads(payload.group(1))["props"]["pageProps"]["state"][
            "indexStore"
        ]["instrument"]["price"]
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as error:
        log.warning(
            "Index unavailable from investing.com",
            extra={"index": name, "slug": slug, "error": str(error)[:120]},
        )
        return IndexQuote(name, country, None, None, None)

    last = price.get("last")
    change = price.get("change")
    if not last:  # zeros mean the feed carries no data, as with Sri Lanka
        log.warning("investing.com published no level", extra={"index": name, "slug": slug})
        return IndexQuote(name, country, None, None, None)

    previous = round(last - change, 4) if change is not None else price.get("lastClose")
    return IndexQuote(
        name=name,
        country=country,
        as_of=_as_of_from_epoch_ms(price.get("lastUpdateTime")),
        today=last,
        yesterday=previous,
    )


def fetch_cse_sri_lanka(session: requests.Session) -> IndexQuote:
    """Colombo All-Share, from the exchange's own JSON API."""
    name, country = "CSE All-Share", "Sri Lanka"
    try:
        response = session.post(CSE_ASPI_URL, timeout=30, headers={"User-Agent": BROWSER_UA})
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        log.warning("CSE All-Share unavailable", extra={"error": str(error)[:120]})
        return IndexQuote(name, country, None, None, None)

    last, change = data.get("value"), data.get("change")
    if not last:
        log.warning("cse.lk published no level")
        return IndexQuote(name, country, None, None, None)

    return IndexQuote(
        name=name,
        country=country,
        as_of=_as_of_from_epoch_ms(data.get("timestamp")),
        today=last,
        yesterday=round(last - change, 4) if change is not None else None,
    )
