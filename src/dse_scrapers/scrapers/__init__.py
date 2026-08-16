"""Importing this package registers every scraper on the CLI menu."""

from . import (
    commodity_prices,
    company_description,
    index_levels,
    market_cap,
    mutual_fund_nav,
    record_dates,
    trade_and_block,
)

__all__ = [
    "commodity_prices",
    "company_description",
    "index_levels",
    "market_cap",
    "mutual_fund_nav",
    "record_dates",
    "trade_and_block",
]
