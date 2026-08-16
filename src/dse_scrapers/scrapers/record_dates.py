"""Scraper 7 - record dates published on the day.

One row per instrument whose record date DSE announced on the run date. A day
with no announcement produces a file with nothing but its column headers,
which is the normal case: over the 210 days to 15 August 2026 only about a
third of days carried one, and dividend season accounts for most of those.

Bond coupon-payment and trading-suspension notices are excluded - see
`sources/record_dates.py` for why.

**Figures are backfilled when the announcement does not carry them.** A
company often declares its dividend with "Record Date will be notified later"
and publishes the date weeks afterwards; that later notice is what puts the
instrument in this file, so the dividend, EPS, AGM date and NAV are read back
from its own earlier announcements. That costs one extra request per
instrument needing it, not a wider window.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from ..base import RunContext, RunResult, Scraper, register
from ..sources import record_dates

log = logging.getLogger(__name__)

COLUMN_ORDER = [
    "Ticker",
    "Record Date",
    "Cash Dividend",
    "Stock Dividend",
    "EPS",
    "AGM date",
    "NAV",
    "EntryDate",
]

# Fields worth going back for when the day's announcement omits them.
BACKFILL_FIELDS = ("cash_dividend_pct", "stock_dividend_pct", "eps", "agm_date", "nav")


@register
class RecordDatesScraper(Scraper):
    key = "record-dates"
    title = "Record Dates"
    columns = COLUMN_ORDER

    def run(self, context: RunContext) -> RunResult:
        session = context.session
        day = context.trading_day

        announcements = record_dates.fetch_day(session, day)
        log.info(
            "Announcements posted",
            extra={"date": day.isoformat(), "count": len(announcements)},
        )

        entries = []
        for announcement in announcements:
            if record_dates.is_excluded(announcement):
                continue
            record_date = record_dates.record_date_of(announcement)
            if record_date is None:
                continue

            figures = record_dates.extract_figures(announcement.body)
            backfilled_from = None
            if any(
                figures[field] is None for field in BACKFILL_FIELDS
            ) and record_dates.is_about_a_dividend(announcement):
                backfilled_from = self._backfill(
                    session, announcement, figures, record_date
                )

            entries.append(
                record_dates.RecordDateEntry(
                    ticker=announcement.ticker,
                    record_date=record_date,
                    posted=announcement.posted,
                    title=announcement.title,
                    backfilled_from=backfilled_from,
                    **figures,
                )
            )

        entries.sort(key=lambda e: e.ticker)
        self._log_outcome(entries, announcements, day)

        frame = pd.DataFrame(
            [
                {
                    "Ticker": e.ticker,
                    "Record Date": e.record_date,
                    "Cash Dividend": e.cash_dividend_pct,
                    "Stock Dividend": e.stock_dividend_pct,
                    "EPS": e.eps,
                    "AGM date": e.agm_date,
                    "NAV": e.nav,
                    "EntryDate": e.posted,
                }
                for e in entries
            ],
            columns=COLUMN_ORDER,
        )

        output_path = context.output_path("DSE Record Dates")
        frame.to_excel(output_path, index=False, sheet_name="Sheet1")
        log.debug("Wrote workbook", extra={"path": str(output_path), "rows": len(frame)})

        return RunResult(output_path=output_path, row_count=len(frame))

    def _backfill(
        self, session, announcement, figures: dict, record_date: dt.date
    ) -> dt.date | None:
        """Fill missing figures from the instrument's own earlier announcements.

        Only announcements posted on or before this one are considered, newest
        first, so a later revision can never leak backwards into an older row.
        """
        cutoff = announcement.posted - dt.timedelta(
            days=record_dates.BACKFILL_LOOKBACK_DAYS
        )
        try:
            history = record_dates.fetch_instrument(session, announcement.ticker)
        except Exception as error:  # a missing history must not fail the run
            log.warning(
                "Could not read instrument history",
                extra={"ticker": announcement.ticker, "error": str(error)[:120]},
            )
            return None

        earlier = sorted(
            (
                a
                for a in history
                if cutoff <= a.posted <= announcement.posted
                and not (a.posted == announcement.posted and a.title == announcement.title)
            ),
            key=lambda a: a.posted,
            reverse=True,
        )

        source_date = None
        for past in earlier:
            if all(figures[field] is not None for field in BACKFILL_FIELDS):
                break
            found = record_dates.extract_figures(past.body)
            for field in BACKFILL_FIELDS:
                if figures[field] is None and found[field] is not None:
                    # An AGM tied to this record date cannot already have been
                    # held. GP's interim dividend would otherwise inherit the
                    # AGM from its annual declaration, months in the past.
                    if field == "agm_date" and found[field] < record_date:
                        continue
                    figures[field] = found[field]
                    source_date = past.posted if source_date is None else source_date

        if source_date:
            log.info(
                "Filled figures from an earlier announcement",
                extra={
                    "ticker": announcement.ticker,
                    "from": source_date.isoformat(),
                    "for": announcement.posted.isoformat(),
                },
            )
        return source_date

    def _log_outcome(self, entries, announcements, day: dt.date) -> None:
        if not entries:
            log.info(
                "No record date was published",
                extra={
                    "date": day.isoformat(),
                    "announcements_scanned": len(announcements),
                },
            )
            return

        missing = {
            field: sum(1 for e in entries if getattr(e, field) is None)
            for field in BACKFILL_FIELDS
        }
        log.info(
            "Record dates published",
            extra={
                "date": day.isoformat(),
                "instruments": len(entries),
                "tickers": [e.ticker for e in entries],
                "backfilled": sum(1 for e in entries if e.backfilled_from),
                "still_missing": {k: v for k, v in missing.items() if v},
            },
        )
