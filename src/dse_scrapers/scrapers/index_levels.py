"""Scraper 5 - index levels, previous close against latest close.

Five Bangladeshi indices from DSE, thirteen international ones from
investing.com, and Sri Lanka from the Colombo exchange's own API.

**This scraper ignores the requested trading date.** None of the three
sources can be asked for a historical level: investing.com exposes only the
current last and previous close, and DSE carries CDSET and DSMEX for the live
session alone. It therefore always reports the most recent session and writes
the actual as-of date of every row, so the sheet says what it really holds.
"""

from __future__ import annotations

import logging
import time

import pandas as pd

from ..base import RunContext, RunResult, Scraper, register
from ..sources import index_values

log = logging.getLogger(__name__)

COLUMN_ORDER = [
    "Index",
    "Country",
    "Date",
    "Yesterday Value",
    "Today Value",
    "Change",
    "Change %",
]

# investing.com slugs, in the order the sheet should list them. Nikkei is the
# 225, the only index of that name.
INTERNATIONAL_INDICES = [
    ("SENSEX 30", "India", "sensex"),
    ("NIFTY 50", "India", "s-p-cnx-nifty"),
    ("DJIA", "USA", "us-30"),
    ("S&P 500", "USA", "us-spx-500"),
    ("FTSE 100", "UK", "uk-100"),
    ("Nikkei 225", "Japan", "japan-ni225"),
    ("SSE Composite", "China", "shanghai-composite"),
    ("CSI 300", "China", "csi300"),
    ("KSE100", "Pakistan", "karachi-100"),
    # Sri Lanka slots in here, from cse.lk rather than investing.com.
    ("SET", "Thailand", "thailand-set"),
    ("KOSPI", "South Korea", "kospi"),
    ("VNI", "Vietnam", "vn"),
    ("Hang Seng", "Hong Kong", "hang-sen-40"),
]

# Sri Lanka is listed between KSE100 and SET, matching the requested order.
SRI_LANKA_POSITION = 9


@register
class IndexLevelsScraper(Scraper):
    key = "index-levels"
    title = "Index Levels"
    columns = COLUMN_ORDER

    def run(self, context: RunContext) -> RunResult:
        session = context.session

        quotes = list(index_values.fetch_dse_indices(session))
        log.info(
            "Bangladeshi indices",
            extra={
                "count": len(quotes),
                "session": next((q.as_of.isoformat() for q in quotes if q.as_of), None),
            },
        )

        international = []
        for name, country, slug in INTERNATIONAL_INDICES:
            international.append(
                index_values.fetch_investing_index(session, name, country, slug)
            )
            time.sleep(index_values.REQUEST_SPACING_SECONDS)

        international.insert(
            SRI_LANKA_POSITION, index_values.fetch_cse_sri_lanka(session)
        )
        quotes.extend(international)

        frame = pd.DataFrame(
            [
                {
                    "Index": q.name,
                    "Country": q.country,
                    "Date": q.as_of,
                    "Yesterday Value": q.yesterday,
                    "Today Value": q.today,
                }
                for q in quotes
            ]
        )

        change = frame["Today Value"] - frame["Yesterday Value"]
        frame["Change"] = change.round(4)
        frame["Change %"] = (
            change / frame["Yesterday Value"].where(frame["Yesterday Value"] != 0) * 100
        ).round(4)
        frame = frame[COLUMN_ORDER]

        self._log_coverage(frame, context)

        output_path = context.output_path("DSE Index Levels")
        frame.to_excel(output_path, index=False, sheet_name="Sheet1")
        log.debug("Wrote workbook", extra={"path": str(output_path), "rows": len(frame)})

        return RunResult(output_path=output_path, row_count=len(frame))

    def _log_coverage(self, frame: pd.DataFrame, context: RunContext) -> None:
        missing = frame[frame["Today Value"].isna()]["Index"].tolist()
        if missing:
            log.warning(
                "Indices with no level",
                extra={"count": len(missing), "indices": missing},
            )

        dates = sorted({d.isoformat() for d in frame["Date"].dropna()})
        log.info(
            "Index levels collected",
            extra={
                "indices": len(frame),
                "with_value": int(frame["Today Value"].notna().sum()),
                "as_of_dates": dates,
                "requested_date_ignored": context.trading_day.isoformat(),
            },
        )
