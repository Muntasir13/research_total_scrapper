"""Market-wide P/E ratios, from latest_PE.php — one request for every ticker.

DSE publishes several P/E variants and defines them on the page itself:

    P/E 1  latest interim financials, basic EPS, continuing operations
    P/E 2  ... diluted
    P/E 3  latest interim, basic EPS, including extraordinary income
    P/E 4  ... diluted
    P/E 5  last audited financials approved by AGM, basic EPS
    P/E 6  ... including extraordinary income
    Trailing P/E  last twelve months

Only Trailing P/E is used here, and only to recover an LTM EPS that cannot be
computed from a company page: the page carries the current year's interims and
the last audited year, but not the prior year's interims, so the trailing
figure cannot be reconstructed. Inverting DSE's own ratio is the one route to
it — at the cost of a blank wherever DSE prints n/a, which includes every
loss-making company.

The page has no date parameter: it is always the live session. Its Close Price
is therefore captured alongside, so an EPS can be recovered at the price DSE
actually divided by, rather than the price of the day being scraped.
"""

from __future__ import annotations

import io
import logging

import pandas as pd
import requests

from ..http import get_text
from ._html import main_content

log = logging.getLogger(__name__)

LATEST_PE_URL = "https://www.dsebd.org/latest_PE.php"

TICKER_COLUMN = "Trade Code"
CLOSE_COLUMN = "Close Price"
TRAILING_COLUMN = "Trailing P/E"
# Column labels carry doubled spaces on the page, so match loosely.
INTERIM_PE_PREFIX = "P/E 1*"


def fetch_pe_table(session: requests.Session) -> pd.DataFrame:
    """Ticker-indexed close price, interim P/E and trailing P/E."""
    page = main_content(get_text(session, LATEST_PE_URL))
    tables = pd.read_html(io.StringIO(page), flavor="lxml")

    candidates = [t for t in tables if TICKER_COLUMN in t.columns]
    if not candidates:
        log.warning("latest_PE.php returned no recognisable table")
        return pd.DataFrame(columns=[TICKER_COLUMN, CLOSE_COLUMN, TRAILING_COLUMN])

    frame = max(candidates, key=len)
    interim = next(
        (c for c in frame.columns if str(c).startswith(INTERIM_PE_PREFIX)), None
    )

    wanted = {TICKER_COLUMN: "Ticker", CLOSE_COLUMN: "pe_close", TRAILING_COLUMN: "trailing_pe"}
    if interim:
        wanted[interim] = "interim_pe"

    frame = frame[list(wanted)].rename(columns=wanted)
    frame["Ticker"] = frame["Ticker"].astype(str).str.strip()
    for column in ("pe_close", "trailing_pe", "interim_pe"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    log.debug("Fetched P/E table", extra={"rows": len(frame)})
    return frame.set_index("Ticker")
