"""Day-end trade data for every instrument, from dsebd.org.

`day_end_archive.php` renders the whole site shell around the table we want,
so we cut the page down to its content div before handing it to pandas —
otherwise read_html parses ~390 layout tables on every call.

DSE keeps roughly two years of history here; older dates come back empty.
"""

from __future__ import annotations

import datetime as dt
import io
import re

import pandas as pd
import requests

from ..errors import NoTradingDataError
from ..http import get_text
from ._html import main_content

DAY_END_URL = "https://www.dsebd.org/day_end_archive.php"

# The site's own header names, mapped to the Uptick column names.
COLUMN_MAP = {
    "DATE": "TradingDate",
    "TRADING CODE": "InstrumentName",
    "LTP*": "LTP",
    "HIGH": "HIGH",
    "LOW": "LOW",
    "OPENP*": "OPENP",
    "CLOSEP*": "CLOSEP",
    "YCP": "YCP",
    "TRADE": "Trade",
    "VALUE (mn)": "Value",
    "VOLUME": "Volume",
}

# Everything the table carries except the date and the code itself.
NUMERIC_COLUMNS = [
    column
    for column in COLUMN_MAP.values()
    if column not in ("TradingDate", "InstrumentName")
]


def _pick_table(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """The data table is the one carrying a TRADING CODE column."""
    candidates = [t for t in tables if "TRADING CODE" in t.columns]
    if not candidates:
        raise NoTradingDataError(
            "day_end_archive.php returned no table with a TRADING CODE column. "
            "The page layout may have changed."
        )
    return max(candidates, key=len)


def fetch_trade_data(session: requests.Session, trading_day: dt.date) -> pd.DataFrame:
    """One row per instrument for `trading_day`, with Uptick column names."""
    day = trading_day.isoformat()
    html = get_text(
        session,
        DAY_END_URL,
        params={
            "startDate": day,
            "endDate": day,
            "inst": "All Instrument",
            "archive": "data",
        },
    )

    # lxml pinned: the default flavour falls back to html5lib, an optional
    # dependency, on malformed markup — which DSE serves on some pages.
    tables = pd.read_html(io.StringIO(main_content(html)), flavor="lxml")
    frame = _pick_table(tables)

    frame = frame.rename(columns=COLUMN_MAP)
    missing = [c for c in COLUMN_MAP.values() if c not in frame.columns]
    if missing:
        raise NoTradingDataError(
            f"day_end_archive.php is missing expected columns: {', '.join(missing)}"
        )
    frame = frame[list(COLUMN_MAP.values())]

    frame["TradingDate"] = pd.to_datetime(frame["TradingDate"], errors="coerce").dt.date
    frame = frame[frame["TradingDate"] == trading_day]

    if frame.empty:
        raise NoTradingDataError(
            f"DSE published no trades for {day}. It was a weekend, a holiday, "
            "or the date is older than the two years day_end_archive keeps."
        )

    frame["InstrumentName"] = frame["InstrumentName"].astype(str).str.strip()
    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(
            frame[column].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )

    return frame.reset_index(drop=True)


def fetch_instrument_names(session: requests.Session) -> dict[str, str]:
    """Trading code to full company name, read off the company list page.

    Needed because CDBL publishes ISINs against company names only.
    """
    html = get_text(session, "https://www.dsebd.org/company_listing.php")
    pattern = re.compile(
        r"displayCompany\.php\?name=([^']+)'[^>]*>[^<]*</a>\s*<span[^>]*>\((.*?)\)",
        re.DOTALL,
    )
    return {
        code.strip(): re.sub(r"\s+", " ", name).strip()
        for code, name in pattern.findall(main_content(html))
    }
