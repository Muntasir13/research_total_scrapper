"""Company reference data, from one displayCompany.php page per instrument.

Everything here comes off `displayCompany.php?name=<CODE>`. There is no
bulk endpoint, so this is one request per instrument — a few hundred — run
across a small thread pool.

Free float is not published as a number. DSE gives a shareholding split
(Sponsor/Director, Govt, Institute, Foreign, Public) and the market's free
float convention is everything outside sponsor and government hands:

    FF% = 100 - Sponsor/Director - Govt

Checked against the hand-maintained `DSE PE File_Root.xlsx` over a 22-ticker
sample: 22/22 agree once the right snapshot is used. Which snapshot matters —
each page carries three, and the *first* is a year-end figure that can be
years stale (YPL's is Jun 2021). The monthly ones that follow are current, so
we always take the block with the latest date.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from ..http import build_session, get_text
from ._html import clean as _clean
from ._html import CELL, ROW, TABLE, main_content, trading_codes

log = logging.getLogger(__name__)

COMPANY_URL = "https://www.dsebd.org/displayCompany.php"
CATEGORY_BOARD_URL = "https://www.dsebd.org/latest_share_price_scroll_group.php"

CATEGORIES = ("A", "B", "G", "N", "Z")

# Shareholding lines run together once tags are stripped, so match the whole
# block at once rather than trying to keep table structure.
_SHAREHOLDING = re.compile(
    r"Share Holding Percentage\s*\[as on ([^\]]+)\]\s*"
    r"Sponsor/Director:\s*([\d.]+)\s*"
    r"Govt:\s*([\d.]+)\s*"
    r"Institute:\s*([\d.]+)\s*"
    r"Foreign:\s*([\d.]+)\s*"
    r"Public:\s*([\d.]+)",
    re.IGNORECASE,
)
# "120% 2025, 110% 2024" — and DSE is inconsistent about the space ("20%2006").
# Bonus entries stated as ratios ("1B:3 1993") carry no % and are skipped.
_DIVIDEND_ENTRY = re.compile(r"(-?[\d.]+)\s*%\s*(\d{4})")

# How much of the year each cumulative interim column covers. Q2 and Q3 are
# discrete quarters, not year-to-date, so they are deliberately absent.
CUMULATIVE_PERIODS = {"Q1": 3, "HALF YEARLY": 6, "9 MONTHS": 9, "ANNUAL": 12}

DEFAULT_FACE_VALUE = 10.0


# DSE writes these where it has nothing; they are not values.
PLACEHOLDERS = {"", "-", "--", "n/a", "N/A", "na", "null"}

# An instrument type that classifies nothing. DSE intermittently prints the
# literal "Unknown" in place of the real value on a page that is otherwise
# complete and parses cleanly. It is never a company's real type — 2,548
# consecutive fetches across 637 instruments produced not one — so it always
# means the response is bad and a refetch is worth making.
INDETERMINATE_TYPES = {None, "Unknown"}

# The last thing a complete company page carries. DSE occasionally cuts a
# response short (one arrived at 83 KB against a ~300 KB floor); the body still
# parses, it just silently has no record in it. `Type of Instrument` sits about
# 87% of the way down, so anything truncated loses that along with free float,
# share count and NAV.
PAGE_END_MARKER = "</body>"

# Total attempts per company page before giving up on it.
PROFILE_ATTEMPTS = 3

# Threads fetching company pages. Six takes the ~637 instruments to about 75
# seconds; DSE starts refusing connections well before this is worth raising.
PROFILE_WORKERS = 6


@dataclass(frozen=True)
class CompanyProfile:
    ticker: str
    category: str | None
    sector: str | None
    year_end: str | None
    outstanding_shares: int | None
    free_float_pct: float | None
    # Equity, Mutual Funds, Corporate Bond, Debenture or Debt. Not part of the
    # company-description output, but it is how debt gets filtered out.
    instrument_type: str | None = None
    # The snapshot free_float_pct was derived from, and its five components.
    # Its date lets the caller flag figures that are years old.
    shareholding: Shareholding | None = None

    # Valuation inputs, used by the market-cap scraper.
    nav_per_share: float | None = None
    face_value: float | None = None
    # Latest year-to-date interim EPS scaled to a full year.
    annualised_eps: float | None = None
    # The interim column it came from, e.g. "9 Months" — kept for logging.
    annualised_from: str | None = None
    # Period end of that column, so stale filings can be flagged.
    annualised_period_end: dt.date | None = None
    cash_dividend_pct: float | None = None
    stock_dividend_pct: float | None = None
    dividend_year: int | None = None

    @property
    def shareholding_as_on(self) -> dt.date | None:
        """Date of the snapshot the free float came from."""
        return self.shareholding.as_on if self.shareholding else None

    @property
    def cash_dividend_per_share(self) -> float | None:
        """Cash dividend in taka, from the percentage of par value."""
        if self.cash_dividend_pct is None:
            return None
        return self.cash_dividend_pct / 100 * (self.face_value or DEFAULT_FACE_VALUE)

    @property
    def total_dividend_pct(self) -> float | None:
        """Cash plus stock dividend for the latest year that declared either."""
        if self.cash_dividend_pct is None and self.stock_dividend_pct is None:
            return None
        return round((self.cash_dividend_pct or 0) + (self.stock_dividend_pct or 0), 2)


def _main_content(page: str) -> str:
    """Page content with scripts stripped, so stray JS never parses as data."""
    return re.sub(
        r"<script.*?</script>", " ", main_content(page), flags=re.DOTALL | re.IGNORECASE
    )


def _label_value_pairs(section: str) -> dict[str, str]:
    """Every `label | value` pair on the page, first occurrence winning."""
    pairs: dict[str, str] = {}
    for table in TABLE.findall(section):
        for row in ROW.findall(table):
            cells = [_clean(cell) for cell in CELL.findall(row)]
            for label, value in zip(cells, cells[1:]):
                if label and value and len(label) < 60:
                    pairs.setdefault(label.rstrip(":"), value)
    return pairs


def _parse_as_on(label: str) -> dt.date | None:
    """'Jul 31, 2026' and 'Dec 31, 2025 (year ended)' both parse."""
    text = re.sub(r"\(.*?\)", "", label).strip()
    try:
        return dt.datetime.strptime(text, "%b %d, %Y").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class Shareholding:
    """One shareholding split, as DSE states it."""

    as_on: dt.date | None
    sponsor_director: float
    govt: float
    institute: float
    foreign: float
    public: float

    @property
    def free_float_pct(self) -> float:
        """Everything outside sponsor and government hands."""
        return round(100.0 - self.sponsor_director - self.govt, 2)


def _shareholding(section: str) -> Shareholding | None:
    """The most recent shareholding snapshot on the page.

    A page carries three, and the first is a year-end figure that can be years
    stale, so they are ranked by date rather than by position. A block that is
    entirely zeros is a placeholder, not a real 0% split.
    """
    snapshots = []
    for match in _SHAREHOLDING.finditer(_clean(section)):
        label, *values = match.groups()
        parts = [float(x) for x in values]
        if sum(parts) == 0:
            continue
        snapshots.append((_parse_as_on(label) or dt.date.min, parts))

    if not snapshots:
        return None

    as_on, parts = max(snapshots, key=lambda item: item[0])
    return Shareholding(
        as_on=as_on if as_on != dt.date.min else None,
        sponsor_director=parts[0],
        govt=parts[1],
        institute=parts[2],
        foreign=parts[3],
        public=parts[4],
    )


def _text(value: str | None) -> str | None:
    """A field's value, or None where DSE printed a placeholder."""
    if value is None or value.strip() in PLACEHOLDERS:
        return None
    return value.strip()


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def _to_float(value) -> float | None:
    text = str(value).strip().replace(",", "")
    if not text or text in PLACEHOLDERS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _dividends_by_year(value: str | None) -> dict[int, float]:
    """Parse a '120% 2025, 110% 2024' history into {year: percent}."""
    if not value:
        return {}
    return {
        int(year): float(percent) for percent, year in _DIVIDEND_ENTRY.findall(value)
    }


def _latest_dividends(pairs: dict[str, str]) -> tuple[float | None, float | None, int | None]:
    """Cash and stock dividend for the most recent year that declared either."""
    cash = _dividends_by_year(pairs.get("Cash Dividend"))
    stock = _dividends_by_year(pairs.get("Bonus Issue (Stock Dividend)"))
    years = set(cash) | set(stock)
    if not years:
        return None, None, None
    latest = max(years)
    return cash.get(latest), stock.get(latest), latest


def _tables(section: str) -> list:
    """Every table on the page as a normalised grid.

    Uses pandas so that colspan and rowspan are expanded — the financial
    tables lean on both, and hand-parsed cells do not line up without it.
    lxml is pinned because the fallback parser is an optional dependency.
    """
    try:
        return pd.read_html(io.StringIO(section), flavor="lxml")
    except ValueError:  # no tables at all
        return []


def _nav_per_share(tables: list) -> float | None:
    """NAV per share for the latest year in the five-year summary.

    That table has three header rows — group, measure, then Original vs
    Restated — so columns are located by their header text rather than by
    position, which shifts between companies.
    """
    for table in tables:
        grid = table.to_numpy()
        if grid.shape[0] < 4 or grid.shape[1] < 4:
            continue
        groups = [str(x).strip() for x in grid[0]]
        if "NAV Per Share" not in groups:
            continue

        variants = [str(x).strip() for x in grid[2]]
        columns = [i for i, g in enumerate(groups) if g == "NAV Per Share"]
        original = [i for i in columns if variants[i] == "Original"] or columns

        for row in reversed(grid[3:]):  # latest year is last
            for index in original:
                value = _to_float(row[index])
                if value is not None:
                    return value
    return None


def _period_end(text: str) -> dt.date | None:
    """Last day of the YYYYMM stamped on an interim column header."""
    match = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])\b", text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    next_month = dt.date(year + month // 12, month % 12 + 1, 1)
    return next_month - dt.timedelta(days=1)


def _annualised_eps(tables: list) -> tuple[float | None, str | None, dt.date | None]:
    """Latest year-to-date interim EPS, scaled to a full year.

    Verified against DSE's own P/E 1 (which it defines as being based on the
    latest interim financials) across a 15-ticker sample: agreement within
    0.05%, the residue being DSE publishing the ratio to two decimals.

    Computed here rather than inverted out of that ratio because DSE prints
    n/a for loss-making companies, where this still returns a real negative.
    """
    for table in tables:
        grid = table.to_numpy()
        if grid.shape[0] < 6:
            continue
        header = [re.sub(r"\s+", " ", str(x).strip().upper()) for x in grid[1]]
        if not any(h.startswith("Q1") for h in header):
            continue

        basic = None
        for index in range(grid.shape[0] - 1):
            label = str(grid[index][0]).strip()
            following = str(grid[index + 1][0]).strip()
            if label == "Earnings Per Share (EPS)" and following == "Basic":
                basic = grid[index + 1]
                break
        if basic is None:
            continue

        # The table covers a single fiscal year, so columns run oldest to
        # newest and the rightmost populated one is the furthest through it.
        period_ends = [str(x) for x in grid[3]] if grid.shape[0] > 3 else []
        latest = None
        for column, label in enumerate(header):
            period = next((p for p in CUMULATIVE_PERIODS if label.startswith(p)), None)
            if period is None:
                continue
            value = _to_float(basic[column])
            if value is not None:
                ends = " ".join(
                    [label] + ([period_ends[column]] if column < len(period_ends) else [])
                )
                latest = (CUMULATIVE_PERIODS[period], value, period, _period_end(ends))

        if latest:
            months, eps, period, ends = latest
            return round(eps * 12 / months, 4), period, ends
    return None, None, None


def parse_profile(ticker: str, page: str) -> CompanyProfile:
    section = _main_content(page)
    pairs = _label_value_pairs(section)

    sector = _text(pairs.get("Sector"))
    category = _text(pairs.get("Market Category"))

    # Government securities carry no Market Category of their own, but DSE
    # files them under the G board — which its own sector label identifies.
    if category is None and sector and sector.upper().startswith("G-SEC"):
        category = "G"

    shareholding = _shareholding(section)
    tables = _tables(section)
    annualised, annualised_from, annualised_end = _annualised_eps(tables)
    cash_pct, stock_pct, dividend_year = _latest_dividends(pairs)

    return CompanyProfile(
        ticker=ticker,
        category=category,
        sector=sector,
        year_end=_text(pairs.get("Year End")),
        outstanding_shares=_to_int(pairs.get("Total No. of Outstanding Securities")),
        free_float_pct=shareholding.free_float_pct if shareholding else None,
        instrument_type=_text(pairs.get("Type of Instrument")),
        shareholding=shareholding,
        nav_per_share=_nav_per_share(tables),
        face_value=_to_float(pairs.get("Face/par Value")),
        annualised_eps=annualised,
        annualised_from=annualised_from,
        annualised_period_end=annualised_end,
        cash_dividend_pct=cash_pct,
        stock_dividend_pct=stock_pct,
        dividend_year=dividend_year,
    )


def fetch_category_board(session: requests.Session) -> dict[str, str]:
    """Trading code to category, from the by-category board.

    Only covers instruments that traded today, so this is a fallback for
    pages whose own Market Category is blank — not the primary source.
    """
    mapping: dict[str, str] = {}
    for category in CATEGORIES:
        try:
            page = get_text(session, CATEGORY_BOARD_URL, params={"group": category})
        except requests.RequestException:
            log.debug("Category board unavailable", extra={"category": category})
            continue
        for code in trading_codes(page):
            mapping.setdefault(code, category)
    return mapping


def fetch_profiles(
    codes: list[str], cache_dir: Path
) -> tuple[list[CompanyProfile], list[str]]:
    """Fetch every company page concurrently.

    Returns the profiles plus the tickers whose page could not be read. Each
    worker gets its own session, since requests.Session is not thread-safe.
    """
    local = threading.local()

    def session_for_thread() -> requests.Session:
        if not hasattr(local, "session"):
            local.session = build_session(cache_dir)
        return local.session

    def fetch_one(ticker: str) -> CompanyProfile | None:
        """One company page, refetched while the response is unusable.

        Two transient faults are retried rather than trusted: a body cut short
        before the record, and a complete page whose instrument type reads
        "Unknown". Both are rare (roughly 1 fetch in 2,000) and both resolve on
        a refetch, but either one silently costs the instrument its row —
        `market-cap` keeps only recognised types, so an unretried "Unknown"
        reclassifies a listed company as debt and the sheet quietly shortens.
        """
        profile = None

        for attempt in range(1, PROFILE_ATTEMPTS + 1):
            try:
                page = get_text(
                    session_for_thread(), COMPANY_URL, params={"name": ticker}
                )
            except requests.RequestException as error:
                # The session already retries transport failures, so this is
                # the end of the line for this ticker.
                log.warning(
                    "Company page unavailable",
                    extra={"ticker": ticker, "attempt": attempt, "error": str(error)},
                )
                return None

            if PAGE_END_MARKER not in page:
                log.warning(
                    "Truncated company page, refetching",
                    extra={"ticker": ticker, "attempt": attempt, "bytes": len(page)},
                )
                continue

            profile = parse_profile(ticker, page)
            if profile.instrument_type not in INDETERMINATE_TYPES:
                return profile

            log.warning(
                "Company page published no instrument type, refetching",
                extra={
                    "ticker": ticker,
                    "attempt": attempt,
                    "type": profile.instrument_type,
                },
            )

        log.error(
            "Company page never returned a usable record",
            extra={"ticker": ticker, "attempts": PROFILE_ATTEMPTS},
        )
        # None only if every attempt was truncated; otherwise the profile is
        # sound apart from its type, which the caller decides what to do with.
        return profile

    profiles: list[CompanyProfile] = []
    failed: list[str] = []

    with ThreadPoolExecutor(max_workers=PROFILE_WORKERS) as pool:
        for index, (ticker, profile) in enumerate(
            zip(codes, pool.map(fetch_one, codes)), start=1
        ):
            if profile is None:
                failed.append(ticker)
            else:
                profiles.append(profile)
            if index % 100 == 0:
                log.debug(
                    "Company pages fetched", extra={"done": index, "total": len(codes)}
                )

    return profiles, failed
