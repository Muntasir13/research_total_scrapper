"""Record-date announcements, parsed out of DSE's news archive.

Companies publish a record date as news, through the same `old_news.php` feed
that carries mutual fund NAV — see `mf_nav.py` for the endpoint. Three things
about that feed shape everything here:

**Long announcements are split into separate news items.** The first ends
`(cont.)` and a second, posted the same day under the same trading code,
opens `(Cont. News of TICKER)`. The figures very often live in the *second*
one — MIDASFIN's dividend declaration carries the AGM and record date in the
first item and its EPS and NAV only in the continuation — so an announcement
has to be reassembled before any field is read.

**A record date is always in the future.** DSE occasionally publishes a wrong
one and corrects it the same day: BNICL's declaration on 19 Apr 2026 said
`Record Date: 13.05.2025`, corrected hours later to `13.05.2026`. Discarding
any candidate that predates the announcement itself resolves that without
having to recognise correction notices by title.

**The first dividend figure is the declared one.** MARICO's final dividend
reads "Final Cash Dividend of 500% ... (Total 2075% Cash Dividend for the
Financial Year inclusive of 1575% Interim...)". The record date entitles
holders to the 500%, not the year's total, so matches are taken in the order
they appear rather than by pattern precedence.

Bond coupon-payment and trading-suspension notices are excluded. Both quote a
record date, but the first is debt servicing rather than a corporate action on
a stock, and the second merely repeats a date already announced.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import requests

from ..http import get_text
from ._html import clean, main_content
from ._news import ITEM, OLD_NEWS_URL

log = logging.getLogger(__name__)

# How far back to look when an announcement names a record date but leaves the
# figures to an earlier declaration. Per-instrument queries are used for this,
# so the window costs one small request per ticker rather than a huge fetch.
BACKFILL_LOOKBACK_DAYS = 400

_CONT_HEAD = re.compile(
    r"^\(\s*Cont\.?\s*news\s+of\s+([A-Z0-9&.\-]+)\s*\)\s*:?\s*", re.IGNORECASE
)

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)
_DATE = (
    r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{4}"
    rf"|\d{{1,2}}[-\s](?:{_MONTHS})[-\s]\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)\s+(?:{_MONTHS})\s+\d{{4}}"
    rf"|(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}})"
)
_ORDINAL = re.compile(r"(\d{1,2})(?:st|nd|rd|th)", re.IGNORECASE)
_DATE_FORMATS = (
    "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y",
    "%d-%B-%Y", "%B %d %Y", "%b %d %Y",
)

# The label and the date are often separated by what the date is for —
# "Record date for entitlement of Interim Cash Dividend is August 12, 2026",
# "Record Date for Determination of Entitlement of Rights Issue: 04 August
# 2026". The gap is bounded and may not cross a sentence end, so the pattern
# cannot wander into an unrelated date further down the announcement.
_RECORD_DATE = re.compile(
    rf"record\s+date\b[^.;]{{0,70}}?(?:will\s+be|shall\s+be|would\s+be|is|are|:|-)\s*({_DATE})",
    re.IGNORECASE,
)
# A revision states the new date before the label: "...i.e., 1 June 2026,
# shall be considered as the revised Record Date instead of 25 May 2026."
_RECORD_DATE_REVISED = re.compile(
    rf"({_DATE})\s*,?\s*(?:shall|will|would)\s+be\s+(?:considered|treated)\s+as\s+"
    rf"the\s+(?:revised\s+|new\s+|changed\s+)?record\s+date",
    re.IGNORECASE,
)
# "...instead of 25 May 2026" names the date being replaced, never the new one.
_SUPERSEDED = re.compile(r"instead\s+of\s*$", re.IGNORECASE)
_AGM_DATE = re.compile(
    rf"date\s+of\s+(?:the\s+)?(?:\d+(?:st|nd|rd|th)\s+)?(?:A\.?G\.?M|Annual\s+General\s+Meeting)"
    rf"\s*[:\-]?\s*(?:will\s+be\s+held\s+on\s*)?({_DATE})",
    re.IGNORECASE,
)

# Figures are printed as "Tk. 4.81" or, when negative, "Tk. (3.17)".
_AMOUNT = r"\(?\s*-?[\d,]+\.?\d*\s*\)?"
_EPS = re.compile(
    rf"\b(?:Consolidated\s+|Restated\s+|Basic\s+)?EP[SU]\s+of\s+Tk\.?\s*({_AMOUNT})",
    re.IGNORECASE,
)
_NAV_PER_SHARE = re.compile(
    rf"NAV\s+per\s+share\s+of\s+Tk\.?\s*({_AMOUNT})", re.IGNORECASE
)
_NAV_PER_UNIT_MARKET = re.compile(
    rf"NAV\s+per\s+unit\s+at\s+market\s+price\s+of\s+Tk\.?\s*({_AMOUNT})", re.IGNORECASE
)
_NAV_PER_UNIT_ANY = re.compile(
    rf"NAV\s+per\s+unit\s+(?:at\s+\w+\s+)?(?:price\s+)?of\s+Tk\.?\s*({_AMOUNT})",
    re.IGNORECASE,
)

# Percentages carry thousands separators at the top end — Reckitt Benckiser
# declared "1,730% Final Cash Dividend", which reads as 730% if the comma is
# not part of the number.
_PCT = r"(\d[\d,]*(?:\.\d+)?)\s*%"
# The amount sits on either side of the label, and is joined to it by "of",
# "@" or nothing at all: "3% Cash Dividend", "Cash Dividend of 3%",
# "Cash Dividend @ 25.00%".
_JOIN = r"\s*(?:of|@|at)?\s*"
_QUALIFIER = r"(?:Interim\s+|Final\s+|Annual\s+)*"

_CASH_BEFORE = re.compile(
    rf"{_PCT}\s*(?:\([^)]*\)\s*)?{_QUALIFIER}Cash\s+(?:Dividend|and)", re.IGNORECASE
)
_CASH_AFTER = re.compile(
    rf"{_QUALIFIER}Cash\s+Dividend{_JOIN}{_PCT}", re.IGNORECASE
)
# "recommended a final dividend of 105% of paid-up capital" never says "cash".
# Guarded below so a stock or bonus dividend can never match it.
_DIVIDEND_PLAIN = re.compile(rf"{_QUALIFIER}Dividend{_JOIN}{_PCT}", re.IGNORECASE)

_STOCK_BEFORE = re.compile(
    rf"{_PCT}\s*(?:Stock|Bonus)\s*(?:Dividend|Share)", re.IGNORECASE
)
_STOCK_AFTER = re.compile(
    rf"(?:Stock|Bonus)\s+(?:Dividend|Share)s?{_JOIN}{_PCT}", re.IGNORECASE
)
_NO_DIVIDEND = re.compile(r"(?:recommended|declared|approved)\s+No\s+Dividend", re.IGNORECASE)

# A declared figure sometimes contains an interim already paid, in which case
# the record date entitles holders only to the balance. LHB: "40% Final Cash
# Dividend (including 18% interim cash dividend which has already been paid)"
# is 22% at this record date.
#
# "in addition to the interim dividend of 143%" is the opposite construction —
# the figure is already net — so only the inclusive wording triggers it.
_INTERIM_INCLUDED = re.compile(
    rf"(?:includ\w+|inclusive\s+of)\s+(?:an?\s+)?{_PCT}\s*(?:\w+\s+){{0,3}}?interim",
    re.IGNORECASE,
)
_ANY_PERCENTAGE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%")

# Guard for the unqualified pattern: what precedes the word "Dividend".
_STOCK_WORD = re.compile(r"(?:stock|bonus)\s*$", re.IGNORECASE)

# Titles that quote a record date without it being a corporate action on a stock.
_BOND_COUPON = re.compile(r"coupon\s+payment|maturity\s+date", re.IGNORECASE)
_SUSPENSION = re.compile(r"suspension\s+for\s+record\s+date", re.IGNORECASE)

# Events that carry a record date but no dividend, EPS or NAV of their own.
# Reading those from the company's last dividend declaration would attach
# figures belonging to an unrelated event — Technodrug's EGM to approve a bond
# issue would otherwise inherit the dividend it declared months earlier.
#
# Stated as what to rule out rather than requiring the word "dividend",
# because a follow-up notice often does not repeat it: Marico's revision of a
# dividend record date says only that the original "falls within the holiday
# period". Blocking too much only leaves cells blank; allowing too much fills
# them with the wrong figures.
_NON_DIVIDEND_EVENT = re.compile(
    r"rights\s+issue|E\.?G\.?M\b|extraordinary\s+general\s+meeting|"
    r"\bbond\b|authoriz(?:ed|ation)\s+(?:share\s+)?capital",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Announcement:
    """One announcement, with any continuation items folded back in."""

    ticker: str
    posted: dt.date
    title: str
    body: str


@dataclass(frozen=True)
class RecordDateEntry:
    ticker: str
    record_date: dt.date
    cash_dividend_pct: float | None
    stock_dividend_pct: float | None
    eps: float | None
    agm_date: dt.date | None
    nav: float | None
    posted: dt.date
    title: str
    backfilled_from: dt.date | None = None


def parse_date(raw: str) -> dt.date | None:
    """A date in any of the several formats DSE writes them in.

    Commas are dropped up front rather than being retried as a second pass:
    none of the formats carries one, so "August 12, 2026" and "August 12 2026"
    are the same input once it is gone.
    """
    text = _ORDINAL.sub(r"\1", raw.strip().rstrip(".,")).replace(",", "")
    text = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _amount(raw: str | None) -> float | None:
    """A money figure, reading accounting parentheses as negative."""
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace(" ", "")
    negative = text.startswith("(") and text.endswith(")")
    try:
        value = float(text.strip("()"))
    except ValueError:
        return None
    return -value if negative else value


def _first_percentage(
    body: str, *patterns: re.Pattern, guard=None
) -> tuple[float, int] | None:
    """The earliest percentage any pattern matches, with where the match ended.

    Ordered by position rather than by pattern so that a later restatement
    ("Total 2075% ... inclusive of 1575% Interim") cannot displace the amount
    actually being declared. The end offset lets the caller look at what
    qualifies the figure.
    """
    best: tuple[int, float, int] | None = None
    for pattern in patterns:
        for match in pattern.finditer(body):
            if guard is not None and not guard(body, match):
                continue
            try:
                value = float(match.group(1).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if best is None or match.start() < best[0]:
                best = (match.start(), value, match.end())
    return (best[1], best[2]) if best else None


def _net_of_interim(body: str, cash: float, after: int) -> float:
    """Deduct an interim dividend the declared figure already contains.

    The clause has to qualify *this* figure. Marico and GP both state a total
    in a parenthetical — "500% ... (Total 2075% ... inclusive of 1575%
    Interim)" — where the inclusion belongs to the total and the declared
    final is already net. Another percentage between the figure and the clause
    is what marks that case.
    """
    clause = _INTERIM_INCLUDED.search(body, after)
    if clause is None:
        return cash
    if _ANY_PERCENTAGE.search(body[after:clause.start()]):
        return cash
    try:
        interim = float(clause.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return cash
    balance = cash - interim
    return balance if balance >= 0 else cash


def _not_a_stock_dividend(body: str, match: re.Match) -> bool:
    """Whether an unqualified "Dividend of X%" is a cash one.

    The plain pattern has no "Cash" to anchor on, so it would otherwise read
    "Stock Dividend of 5%" as cash.
    """
    return not _STOCK_WORD.search(body[max(0, match.start() - 12):match.start()])


def parse_announcements(page_text: str) -> list[Announcement]:
    """Every announcement on a news page, continuations folded into their parent.

    Continuations are matched on the trading code named in the `(Cont. News of
    X)` prefix plus the post date, since the item's own Trading Code field is
    the same and the pair is always published together.
    """
    raw = [
        {
            "code": m["code"].strip().upper(),
            "title": m["title"].strip(),
            "body": m["body"].strip(),
            "posted": m["posted"],
        }
        for m in ITEM.finditer(page_text)
    ]

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for item in raw:
        head = _CONT_HEAD.match(item["body"])
        code = head.group(1).upper() if head else item["code"]
        # Group by title as well, so two unrelated announcements for the same
        # ticker on one day stay apart.
        key = (code, item["posted"], item["title"].lower())
        groups.setdefault(key, []).append(item)

    announcements = []
    for (code, posted, _), group in groups.items():
        ordered = sorted(group, key=lambda i: 1 if _CONT_HEAD.match(i["body"]) else 0)
        body = " ".join(_CONT_HEAD.sub("", i["body"]).strip() for i in ordered)
        announcements.append(
            Announcement(
                ticker=code,
                posted=dt.date.fromisoformat(posted),
                title=ordered[0]["title"],
                body=body,
            )
        )
    return announcements


def is_excluded(announcement: Announcement) -> bool:
    """Bond coupon servicing and suspension reminders are not corporate actions."""
    return bool(
        _BOND_COUPON.search(announcement.title) or _SUSPENSION.search(announcement.title)
    )


def is_about_a_dividend(announcement: Announcement) -> bool:
    """Whether figures from an earlier declaration could belong to this event."""
    return not (
        _NON_DIVIDEND_EVENT.search(announcement.title)
        or _NON_DIVIDEND_EVENT.search(announcement.body)
    )


def record_date_of(announcement: Announcement) -> dt.date | None:
    """The record date announced, or None if the announcement sets none.

    Candidates that predate the announcement are discarded — a record date is
    always in the future, so anything earlier is the wrong reading of a
    correction ("Record date will be 13.05.2026 instead of 13.05.2025").

    A revision is checked first: when a company moves a record date off a
    holiday it restates both dates in one sentence, and the new one is the
    answer.
    """
    revised = _RECORD_DATE_REVISED.search(announcement.body)
    if revised:
        value = parse_date(revised.group(1))
        if value is not None and value >= announcement.posted:
            return value

    for match in _RECORD_DATE.finditer(announcement.body):
        if _SUPERSEDED.search(announcement.body[:match.start(1)]):
            continue
        value = parse_date(match.group(1))
        if value is not None and value >= announcement.posted:
            return value
    return None


def extract_figures(body: str) -> dict:
    """Dividend, EPS, AGM date and NAV, as far as the text carries them."""
    found = _first_percentage(body, _CASH_BEFORE, _CASH_AFTER)
    if found is None:
        # Fall back to the unqualified wording only once the explicit "Cash"
        # forms have failed, so a stock-only declaration stays cash-blank.
        found = _first_percentage(body, _DIVIDEND_PLAIN, guard=_not_a_stock_dividend)

    if found is not None:
        # The sheet records what this record date actually entitles holders
        # to, so an interim already paid comes out of the declared figure.
        cash = _net_of_interim(body, found[0], found[1])
    elif _NO_DIVIDEND.search(body):
        cash = 0.0
    else:
        cash = None

    eps = _EPS.search(body)
    nav = (
        _NAV_PER_SHARE.search(body)
        or _NAV_PER_UNIT_MARKET.search(body)
        or _NAV_PER_UNIT_ANY.search(body)
    )
    agm = _AGM_DATE.search(body)

    return {
        "cash_dividend_pct": cash,
        "stock_dividend_pct": (stock[0] if (stock := _first_percentage(
            body, _STOCK_BEFORE, _STOCK_AFTER)) else None),
        "eps": _amount(eps.group(1)) if eps else None,
        "agm_date": parse_date(agm.group(1)) if agm else None,
        "nav": _amount(nav.group(1)) if nav else None,
    }


def fetch_day(session: requests.Session, day: dt.date) -> list[Announcement]:
    """Every announcement DSE posted on one date."""
    page = get_text(
        session,
        OLD_NEWS_URL,
        params={
            "startDate": day.isoformat(),
            "endDate": day.isoformat(),
            "criteria": 4,
            "archive": "news",
        },
    )
    announcements = parse_announcements(clean(main_content(page)))
    log.debug(
        "Fetched a day of news",
        extra={"date": day.isoformat(), "announcements": len(announcements)},
    )
    return announcements


def fetch_instrument(session: requests.Session, ticker: str) -> list[Announcement]:
    """One instrument's news history, for filling in figures announced earlier."""
    page = get_text(
        session,
        OLD_NEWS_URL,
        params={"inst": ticker, "criteria": 3, "archive": "news"},
    )
    return parse_announcements(clean(main_content(page)))
