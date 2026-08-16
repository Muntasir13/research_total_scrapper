"""ISIN lookup, from CDBL.

Two things to know about the source:

* The advertised page, `isin_details.php`, only meta-refreshes to a shell
  that never renders the table. The data actually lives at `isin.php`.
* CDBL lists ISINs against **company names only** — there is no trading
  code column — so codes have to be matched through the company name that
  DSE publishes.

Name matching is deliberately exact-after-normalisation. Loosening it to
prefix or fuzzy matching raises coverage from ~91% to ~98% but starts
assigning, for example, AB Bank's equity ISIN to ABBLPBOND, its perpetual
bond. A blank ISIN is recoverable; a plausible wrong one is not.

Anything unmatched can be pinned by hand in data/isin_overrides.csv.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

import requests

from ..http import get_text
from ._html import CELL, ROW, clean

CDBL_ISIN_URL = "https://www.cdbl.com.bd/isin.php"
OVERRIDES_FILENAME = "isin_overrides.csv"

# Dropped before comparing, because DSE and CDBL disagree on them freely.
_SUFFIXES = re.compile(r"\b(plc|ltd|limited|company|co|the|corporation)\b")


def normalise(name: str) -> str:
    """Reduce a company name to a comparable key."""
    name = name.lower().replace("&", "and")
    name = _SUFFIXES.sub(" ", name)
    return re.sub(r"[^a-z0-9]", "", name)


def fetch_isin_directory(session: requests.Session, cache_dir: Path) -> dict[str, str]:
    """Normalised company name to ISIN, for all CDBL-enlisted securities.

    Cached under today's calendar date — the directory changes only when
    something is enlisted, and the dated files are the record of when that
    happened.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"cdbl_isin_{dt.date.today():%Y-%m-%d}.html"

    if cached.exists():
        page = cached.read_text(encoding="utf-8", errors="ignore")
    else:
        page = get_text(session, CDBL_ISIN_URL)
        cached.write_text(page, encoding="utf-8")

    directory: dict[str, str] = {}
    for row in ROW.findall(page):
        cells = [clean(cell) for cell in CELL.findall(row)]
        if len(cells) >= 3 and re.fullmatch(r"BD[0-9A-Z]{10}", cells[2]):
            directory.setdefault(normalise(cells[1]), cells[2])
    return directory


def load_overrides(data_dir: Path) -> dict[str, str]:
    """Hand-maintained trading code to ISIN pairs, which win over CDBL."""
    path = data_dir / OVERRIDES_FILENAME
    if not path.exists():
        return {}

    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["InstrumentName"].strip(): row["ISIN"].strip()
            for row in csv.DictReader(handle)
            if row.get("InstrumentName") and row.get("ISIN")
        }


def resolve(
    codes: list[str],
    company_names: dict[str, str],
    directory: dict[str, str],
    overrides: dict[str, str],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Map trading codes to ISINs.

    Returns the mapping plus the codes that could not be matched, each with
    the company name DSE gave it, so the caller can report them.
    """
    resolved: dict[str, str] = {}
    unmatched: list[tuple[str, str]] = []

    for code in codes:
        if code in overrides:
            resolved[code] = overrides[code]
            continue

        name = company_names.get(code, "")
        isin = directory.get(normalise(name)) if name else None
        if isin:
            resolved[code] = isin
        else:
            unmatched.append((code, name))

    return resolved, unmatched
