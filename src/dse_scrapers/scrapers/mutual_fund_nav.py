"""Scraper 4 - mutual fund NAV on both bases.

One row per listed mutual fund: its latest published Net Asset Value per
unit, valued at cost and at current market price, with the date that NAV is
as of.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..base import RunContext, RunResult, Scraper, register
from ..errors import NoTradingDataError
from ..sources import mf_nav, sectors

log = logging.getLogger(__name__)

COLUMN_ORDER = ["MF", "Date", "Cost Price / NAV", "Mkt Price / Nav"]

MUTUAL_FUND_SECTOR = "Mutual Funds"


@register
class MutualFundNavScraper(Scraper):
    key = "mf-nav"
    title = "Mutual Fund NAV"
    columns = COLUMN_ORDER

    def run(self, context: RunContext) -> RunResult:
        session = context.session
        day = context.trading_day

        funds = sectors.fetch_sector_constituents(session, MUTUAL_FUND_SECTOR)
        if not funds:
            raise NoTradingDataError(
                f"No instruments found in the {MUTUAL_FUND_SECTOR} sector. The "
                "sector directory may have changed."
            )
        log.info("Listed mutual funds", extra={"count": len(funds)})

        disclosures = mf_nav.fetch_disclosures(session, day)
        latest = mf_nav.latest_on_or_before(disclosures, day)
        log.info(
            "NAV disclosures",
            extra={"parsed": len(disclosures), "funds_with_nav": len(latest)},
        )

        self._log_gaps(funds, latest, day)

        frame = pd.DataFrame(
            [
                {
                    "MF": ticker,
                    "Date": latest[ticker].as_on if ticker in latest else None,
                    "Cost Price / NAV": latest[ticker].cost_nav if ticker in latest else None,
                    "Mkt Price / Nav": latest[ticker].market_nav if ticker in latest else None,
                }
                for ticker in funds
            ],
            columns=COLUMN_ORDER,
        )
        frame = frame.sort_values("MF").reset_index(drop=True)

        output_path = context.output_path("DSE Mutual Fund NAV")
        frame.to_excel(output_path, index=False, sheet_name="Sheet1")
        log.debug("Wrote workbook", extra={"path": str(output_path), "rows": len(frame)})

        return RunResult(output_path=output_path, row_count=len(frame))

    def _log_gaps(self, funds: list[str], latest: dict, day) -> None:
        """Report funds with no NAV, and any whose NAV predates the run date."""
        missing = sorted(set(funds) - set(latest))
        if missing:
            log.warning(
                "Mutual funds with no NAV disclosure in the window",
                extra={"count": len(missing), "funds": missing},
            )

        carried = sorted(
            (ticker, record.as_on.isoformat())
            for ticker, record in latest.items()
            if record.as_on < day
        )
        if carried:
            log.warning(
                "NAV carried forward from an earlier date",
                extra={"count": len(carried), "funds": carried[:15]},
            )

        unexpected = sorted(set(latest) - set(funds))
        if unexpected:
            log.info(
                "NAV disclosed by instruments outside the sector",
                extra={"count": len(unexpected), "tickers": unexpected[:15]},
            )
