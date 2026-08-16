"""Block-market trades, from https://www.dsebd.org/mst.txt

mst.txt is a plain-text daily bulletin. Its second half lists every scrip
that traded in the block market:

    Instr Code    Max Price    Min Price    Trades    Quantity    Value(In Mn)

    AAMRATECH         20.00        20.00         1       47000           0.940

DSE only ever serves the latest session there — no archive, no date
parameter. So every run saves the raw file under the session date it
reports, and older dates are read back out of that archive.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pandas as pd
import requests

from ..errors import BlockDataUnavailableError
from ..http import get_text
from ._html import number

MST_URL = "https://www.dsebd.org/mst.txt"

BLOCK_COLUMNS = ["InstrumentName", "MaxPrice", "MinPrice", "Trades", "Quantity", "ValueInMn"]

_HEADING = re.compile(r"PRICES IN BLOCK TRANSACTIONS\s*:\s*(\d{4}-\d{2}-\d{2})")
_ROW = re.compile(
    r"^\s*(?P<code>[A-Z0-9][A-Z0-9().\-]*)\s+"
    r"(?P<max>[\d,]+\.?\d*)\s+"
    r"(?P<min>[\d,]+\.?\d*)\s+"
    r"(?P<trades>[\d,]+)\s+"
    r"(?P<quantity>[\d,]+)\s+"
    r"(?P<value>[\d,]+\.?\d*)\s*$"
)


def session_date(text: str) -> dt.date | None:
    """The date mst.txt says its block section covers."""
    match = _HEADING.search(text)
    return dt.date.fromisoformat(match.group(1)) if match else None


def parse_block_trades(text: str) -> pd.DataFrame:
    """Rows between the block heading and its totals line."""
    match = _HEADING.search(text)
    if not match:
        return pd.DataFrame(columns=BLOCK_COLUMNS)

    rows = []
    for line in text[match.end() :].splitlines():
        if set(line.strip()) <= {"-", " "} and "-" in line:
            break  # the dashed rule above the totals row
        row = _ROW.match(line)
        if row:
            rows.append(
                {
                    "InstrumentName": row["code"],
                    "MaxPrice": number(row["max"]),
                    "MinPrice": number(row["min"]),
                    "Trades": int(number(row["trades"])),
                    "Quantity": int(number(row["quantity"])),
                    "ValueInMn": number(row["value"]),
                }
            )

    return pd.DataFrame(rows, columns=BLOCK_COLUMNS)


def _archive_path(archive_dir: Path, day: dt.date) -> Path:
    return archive_dir / f"mst_{day:%Y-%m-%d}.txt"


def refresh_archive(session: requests.Session, archive_dir: Path) -> dt.date | None:
    """Fetch the live bulletin and file it under the date it reports.

    Returns that date, or None if the bulletin carried no block section.
    Runs on every invocation so the archive keeps growing even when the
    scraper is being used for an older date.
    """
    try:
        text = get_text(session, MST_URL)
    except requests.RequestException:
        return None

    day = session_date(text)
    if day is None:
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    _archive_path(archive_dir, day).write_text(text, encoding="utf-8")
    return day


def load_block_trades(
    session: requests.Session, trading_day: dt.date, archive_dir: Path
) -> tuple[pd.DataFrame, str]:
    """Block trades for `trading_day`, live if it is the current session.

    Returns the frame and a short note describing where it came from.
    """
    live_day = refresh_archive(session, archive_dir)

    path = _archive_path(archive_dir, trading_day)
    if not path.exists():
        if live_day is None:
            detail = "mst.txt could not be read just now."
        else:
            detail = (
                f"dsebd.org only publishes the latest session, currently "
                f"{live_day:%Y-%m-%d}."
            )
        raise BlockDataUnavailableError(
            f"No block-market data for {trading_day:%Y-%m-%d}. {detail} "
            f"Archived sessions live in {archive_dir}."
        )

    frame = parse_block_trades(path.read_text(encoding="utf-8"))
    source = "live from mst.txt" if live_day == trading_day else f"archived {path.name}"
    return frame, source
