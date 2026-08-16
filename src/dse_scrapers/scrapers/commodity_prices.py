"""Scraper 6 - commodity prices from Trading Economics.

Thirteen benchmarks, in the order they were requested, with the prior
observation and the move between the two.

**This scraper ignores the requested trading date**, on the same grounds as
`index-levels`: the commodities page publishes only the current quote, so
there is nothing to ask it for a past date with. It reports the latest
observation and stamps every row with the date that observation belongs to.
Those dates legitimately differ between rows - the traded futures print daily
while the assessed physical benchmarks lag a day.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..base import RunContext, RunResult, Scraper, register
from ..errors import NoTradingDataError
from ..sources import commodities

log = logging.getLogger(__name__)

COLUMN_ORDER = [
    "Commodity",
    "Unit",
    "Date",
    "Previous",
    "Price",
    "Change",
    "Change %",
]

# Requested name -> the name Trading Economics uses on the page, in the order
# the sheet should list them. Only two differ from the request:
#   * "LNG JKM" is the Japan/Korea Marker the request names in full.
#   * Where a plain name and a qualified one both exist, the plain one is the
#     headline benchmark: "Iron Ore" is 62% Fe CFR China in USD/T, against
#     "Iron Ore CNY" priced in Dalian; "Steel" is Shanghai rebar, against
#     "HRC Steel" and "Scrap Steel".
COMMODITIES = [
    ("Brent", "Brent"),
    ("Gold", "Gold"),
    ("Wheat", "Wheat"),
    ("Cotton", "Cotton"),
    ("Soybeans", "Soybeans"),
    ("Sugar", "Sugar"),
    ("LNG Japan / Korea Marker PLATTS", "LNG JKM"),
    ("Iron Ore", "Iron Ore"),
    ("Coal", "Coal"),
    ("UK Gas", "UK Gas"),
    ("Steel", "Steel"),
    ("Containerized Freight Index", "Containerized Freight Index"),
    ("Silver", "Silver"),
]

# Flag a quote that has not moved in this long; the page keeps showing the
# last assessment indefinitely, so a stale row looks exactly like a live one.
STALE_AFTER_DAYS = 5


@register
class CommodityPricesScraper(Scraper):
    key = "commodity-prices"
    title = "Commodity Prices"
    columns = COLUMN_ORDER

    def run(self, context: RunContext) -> RunResult:
        quotes = commodities.fetch_quotes(context.session)
        if not quotes:
            raise NoTradingDataError(
                "No commodity rows found on tradingeconomics.com/commodities. "
                "The page layout may have changed."
            )
        log.info("Commodities on the page", extra={"count": len(quotes)})

        rows = []
        for label, page_name in COMMODITIES:
            quote = quotes.get(page_name)
            if quote is None:
                log.warning(
                    "Commodity not found on the page",
                    extra={"commodity": label, "page_name": page_name},
                )
                rows.append({"Commodity": label})
                continue

            rows.append(
                {
                    "Commodity": label,
                    "Unit": quote.unit,
                    "Date": quote.as_of,
                    "Previous": quote.previous,
                    "Price": quote.price,
                    "Change": quote.change,
                    "Change %": quote.change_pct,
                }
            )

        frame = pd.DataFrame(rows, columns=COLUMN_ORDER)

        self._log_coverage(frame, context)

        output_path = context.output_path("Commodity Prices")
        frame.to_excel(output_path, index=False, sheet_name="Sheet1")
        log.debug("Wrote workbook", extra={"path": str(output_path), "rows": len(frame)})

        return RunResult(output_path=output_path, row_count=len(frame))

    def _log_coverage(self, frame: pd.DataFrame, context: RunContext) -> None:
        missing = frame[frame["Price"].isna()]["Commodity"].tolist()
        if missing:
            log.warning(
                "Commodities with no price",
                extra={"count": len(missing), "commodities": missing},
            )

        dated = frame.dropna(subset=["Date"])
        stale = [
            (row["Commodity"], row["Date"].isoformat())
            for _, row in dated.iterrows()
            if (context.trading_day - row["Date"]).days > STALE_AFTER_DAYS
        ]
        if stale:
            log.warning(
                "Quotes older than the run date",
                extra={"count": len(stale), "commodities": stale},
            )

        log.info(
            "Commodity prices collected",
            extra={
                "commodities": len(frame),
                "with_price": int(frame["Price"].notna().sum()),
                "as_of_dates": sorted({d.isoformat() for d in dated["Date"]}),
                "requested_date_ignored": context.trading_day.isoformat(),
            },
        )
