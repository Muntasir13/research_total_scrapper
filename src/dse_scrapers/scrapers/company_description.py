"""Scraper 2 - company description for every listed instrument.

One row per instrument: what it is, which board it trades on, and how much
of it is actually in public hands.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from ..base import RunContext, RunResult, Scraper, register
from ..errors import NoTradingDataError
from ..sources import company_profile, day_end

log = logging.getLogger(__name__)

COLUMN_ORDER = [
    "Ticker",
    "Category",
    "Sector",
    "Year End",
    "Outstanding Shares",
    "FF%",
    "Sponsor/Director",
    "Govt",
    "Institute",
    "Foreign",
    "Public",
]

# The shareholding split behind FF%, which is 100 - Sponsor/Director - Govt.
# All five come from the same snapshot, so they sum to 100 and the free float
# can be checked against its own inputs.
SHAREHOLDING_COLUMNS = ["Sponsor/Director", "Govt", "Institute", "Foreign", "Public"]


@register
class CompanyDescriptionScraper(Scraper):
    key = "company-description"
    title = "Company Description"
    columns = COLUMN_ORDER

    def run(self, context: RunContext) -> RunResult:
        session = context.session

        codes = sorted(day_end.fetch_instrument_names(session))
        if not codes:
            raise NoTradingDataError(
                "company_listing.php returned no instruments. The page layout "
                "may have changed."
            )
        log.info("Listed instruments", extra={"count": len(codes)})

        profiles, failed = company_profile.fetch_profiles(codes, context.cache_dir)
        log.info(
            "Fetched company pages",
            extra={"succeeded": len(profiles), "failed": len(failed)},
        )
        if failed:
            log.warning(
                "Company pages could not be read",
                extra={"count": len(failed), "tickers": failed},
            )

        frame = pd.DataFrame(
            [
                {
                    "Ticker": p.ticker,
                    "Category": p.category,
                    "Sector": p.sector,
                    "Year End": p.year_end,
                    "Outstanding Shares": p.outstanding_shares,
                    "FF%": p.free_float_pct,
                    "Sponsor/Director": s.sponsor_director if s else None,
                    "Govt": s.govt if s else None,
                    "Institute": s.institute if s else None,
                    "Foreign": s.foreign if s else None,
                    "Public": s.public if s else None,
                }
                for p in profiles
                for s in (p.shareholding,)
            ],
            columns=COLUMN_ORDER,
        )
        # Nullable integer, so the two instruments DSE leaves blank don't turn
        # the whole column into floats and write as "289923349.0".
        frame["Outstanding Shares"] = frame["Outstanding Shares"].astype("Int64")

        self._fill_missing_categories(frame, session)
        self._log_stale_free_float(profiles, context)
        self._log_shareholding_consistency(frame)
        self._log_gaps(frame)

        frame = frame.sort_values("Ticker").reset_index(drop=True)
        output_path = context.output_path("DSE Company Description")
        frame.to_excel(output_path, index=False, sheet_name="Sheet1")
        log.debug("Wrote workbook", extra={"path": str(output_path), "rows": len(frame)})

        return RunResult(output_path=output_path, row_count=len(frame))

    def _fill_missing_categories(self, frame: pd.DataFrame, session) -> None:
        """Top up blank categories from the by-category board.

        The board only lists instruments that traded today, so it cannot be
        the primary source - but it covers the odd page that omits its own
        Market Category.
        """
        missing = frame["Category"].isna()
        if not missing.any():
            return

        board = company_profile.fetch_category_board(session)
        if not board:
            return

        filled = frame.loc[missing, "Ticker"].map(board)
        frame.loc[missing, "Category"] = filled
        recovered = int(filled.notna().sum())
        if recovered:
            log.info("Categories recovered from the board", extra={"count": recovered})

    def _log_stale_free_float(self, profiles, context: RunContext) -> None:
        """Flag free floats computed from a shareholding snapshot over a year old.

        DSE leaves some pages un-refreshed for years - APOLOISPAT's latest
        real split is from 2021 - and the number looks current either way.
        """
        cutoff = context.trading_day - dt.timedelta(days=365)
        stale = sorted(
            (p.ticker, p.shareholding_as_on.isoformat())
            for p in profiles
            if p.shareholding_as_on and p.shareholding_as_on < cutoff
        )
        if stale:
            log.warning(
                "Free float based on a shareholding snapshot over a year old",
                extra={"count": len(stale), "oldest": stale[:15]},
            )

    def _log_shareholding_consistency(self, frame: pd.DataFrame) -> None:
        """Check the split against itself: it should total 100 and give FF%.

        DSE states all five to two decimals, so a hundredth of rounding is
        normal; anything larger means a snapshot was read wrong.
        """
        split = frame[SHAREHOLDING_COLUMNS]
        present = split.notna().all(axis=1)
        if not present.any():
            return

        total = split[present].sum(axis=1)
        off = frame.loc[present][(total - 100).abs() > 0.05]
        if len(off):
            log.warning(
                "Shareholding split does not total 100",
                extra={
                    "count": len(off),
                    "tickers": off["Ticker"].tolist()[:15],
                },
            )

        implied = 100 - split.loc[present, "Sponsor/Director"] - split.loc[present, "Govt"]
        drift = (implied - frame.loc[present, "FF%"]).abs()
        if (drift > 0.05).any():
            log.warning(
                "FF% does not match its own shareholding split",
                extra={"count": int((drift > 0.05).sum())},
            )

        log.info(
            "Shareholding split",
            extra={
                "instruments_with_split": int(present.sum()),
                "totalling_100": int((total - 100).abs().le(0.05).sum()),
            },
        )

    def _log_gaps(self, frame: pd.DataFrame) -> None:
        """Report blanks, so a parsing regression is visible rather than silent."""
        for column in COLUMN_ORDER[1:]:
            blank = frame[frame[column].isna()]["Ticker"].tolist()
            if blank:
                log.warning(
                    "Column has no value for some instruments",
                    extra={
                        "column": column,
                        "count": len(blank),
                        "tickers": blank[:25],
                    },
                )
