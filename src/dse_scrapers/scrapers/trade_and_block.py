"""Scraper 1 - daily trade and block data for every DSE instrument.

Output matches the IDLC SL Uptick app's sheet: one row per instrument that
DSE published for the day, public-market OHLC first, then that instrument's
block-market activity. Instruments with no block trades carry zeros, not
blanks, because that is what the reference file does.
"""

from __future__ import annotations

import csv
import logging

import pandas as pd

from ..base import RunContext, RunResult, Scraper, register
from ..sources import block_market, cdbl_isin, day_end

log = logging.getLogger(__name__)

COLUMN_ORDER = [
    "TradingDate",
    "Exchange",
    "InstrumentName",
    "ISIN",
    "LTP",
    "HIGH",
    "LOW",
    "OPENP",
    "CLOSEP",
    "YCP",
    "Trade",
    "Value",
    "Volume",
    "MaxPrice",
    "MinPrice",
    "Trades",
    "Quantity",
    "ValueInMn",
]

BLOCK_COLUMNS = ["MaxPrice", "MinPrice", "Trades", "Quantity", "ValueInMn"]


@register
class TradeAndBlockScraper(Scraper):
    key = "trade-block"
    title = "Daily Trade and Block data"
    columns = COLUMN_ORDER

    def run(self, context: RunContext) -> RunResult:
        session = context.session
        day = context.trading_day

        trades = day_end.fetch_trade_data(session, day)
        log.info(
            "Fetched public-market trades",
            extra={"instruments": len(trades), "source": "day_end_archive.php"},
        )

        blocks, block_source = block_market.load_block_trades(
            session, day, context.data_dir / "block_archive"
        )
        log.info(
            "Loaded block-market trades",
            extra={"scrips": len(blocks), "source": block_source},
        )

        frame = trades.merge(blocks, on="InstrumentName", how="left")
        frame[BLOCK_COLUMNS] = frame[BLOCK_COLUMNS].fillna(0)
        frame[["Trades", "Quantity"]] = frame[["Trades", "Quantity"]].astype("int64")

        unlisted = sorted(set(blocks["InstrumentName"]) - set(trades["InstrumentName"]))
        if unlisted:
            log.warning(
                "Block scrips absent from the trade table",
                extra={"count": len(unlisted), "instruments": unlisted},
            )

        isins, unmatched = self._resolve_isins(context, frame["InstrumentName"].tolist())
        frame["ISIN"] = frame["InstrumentName"].map(isins)
        log.info(
            "Resolved ISINs",
            extra={"matched": int(frame["ISIN"].notna().sum()), "total": len(frame)},
        )

        # A real date cell, not the bare serial the original Uptick workbook
        # used. A plain date rather than a Timestamp, so Excel formats it
        # YYYY-MM-DD instead of hanging a midnight time off every row.
        frame["TradingDate"] = pd.Series([day] * len(frame), index=frame.index, dtype=object)
        frame["Exchange"] = "DSE"
        frame = frame[COLUMN_ORDER].sort_values("InstrumentName").reset_index(drop=True)

        output_path = context.output_path("DSE Trade and Block")
        frame.to_excel(output_path, index=False, sheet_name="Sheet1")
        log.debug(
            "Wrote workbook", extra={"path": str(output_path), "rows": len(frame)}
        )

        extra_files = []
        if unmatched:
            missing_path = self._write_unmatched(context, unmatched)
            extra_files.append(missing_path)
            log.warning(
                "Instruments left without an ISIN",
                extra={
                    "count": len(unmatched),
                    "listed_in": str(missing_path),
                    "hint": "pin them in data/isin_overrides.csv",
                },
            )

        return RunResult(
            output_path=output_path,
            row_count=len(frame),
            extra_files=extra_files,
        )

    def _resolve_isins(
        self, context: RunContext, codes: list[str]
    ) -> tuple[dict[str, str], list[tuple[str, str]]]:
        company_names = day_end.fetch_instrument_names(context.session)
        directory = cdbl_isin.fetch_isin_directory(context.session, context.cache_dir)
        overrides = cdbl_isin.load_overrides(context.data_dir)
        return cdbl_isin.resolve(codes, company_names, directory, overrides)

    def _write_unmatched(self, context: RunContext, unmatched: list[tuple[str, str]]):
        """List the gaps so they can be pinned in data/isin_overrides.csv."""
        path = context.output_path("DSE Trade and Block missing-isin", suffix=".csv")
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["InstrumentName", "DSE company name", "ISIN"])
            for code, name in sorted(unmatched):
                writer.writerow([code, name, ""])
        return path
