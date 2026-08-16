"""Scraper 3 - market capitalisation for every listed equity and mutual fund.

    MCAP   = closing price x outstanding shares
    FFMCAP = MCAP x free float %

Both in BDT millions, DSE's own reporting unit. `Index (%)` is each
constituent's share of the **DSEX** total, so it is blank for anything
outside the index. Debt - corporate bonds, debentures and government
securities - is excluded entirely.

A Total row closes the sheet.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from ..base import RunContext, RunResult, Scraper, register
from ..errors import NoTradingDataError
from ..sources import company_profile, day_end, dse_index, pe_ratios

log = logging.getLogger(__name__)

COLUMN_ORDER = [
    "Ticker",
    "MCAP",
    "FFMCAP",
    "Index (%)",
    "LTM Earnings",
    "LTM EPS",
    "LTM P/E",
    "P/NAV",
    "Annualized Earnings",
    "Annualized EPS",
    "Annualized P/E",
    "Dividend Yield",
    "Total Dividend",
]

# Per-share figures and ratios cannot be summed. Earnings could be, but the
# market-wide total is swamped by a couple of distressed banks - FIRSTSBANK
# alone annualises to -767bn - so it reads as a defect rather than a figure.
TOTALLED_COLUMNS = ["MCAP", "FFMCAP", "Index (%)"]

# What counts as a listed company for market-cap purposes. Everything else on
# the board is debt: Corporate Bond, Debenture, Debt.
EQUITY_TYPES = {"Equity", "Mutual Funds"}

MILLION = 1_000_000

TOTAL_LABEL = "Total"

# How far our annualised EPS may sit from DSE's own P/E 1 before it is worth
# a warning. DSE publishes that ratio to two decimals, so a few tenths of a
# percent is quantisation rather than a parse fault.
PE1_TOLERANCE = 0.01


@register
class MarketCapScraper(Scraper):
    key = "market-cap"
    title = "Market Capitalisation"
    columns = COLUMN_ORDER

    def run(self, context: RunContext) -> RunResult:
        session = context.session
        day = context.trading_day

        trades = day_end.fetch_trade_data(session, day)
        closing = trades.set_index("InstrumentName")["CLOSEP"]
        log.info("Fetched closing prices", extra={"instruments": len(closing)})

        codes = sorted(day_end.fetch_instrument_names(session))
        profiles, failed = company_profile.fetch_profiles(codes, context.cache_dir)
        if failed:
            log.warning(
                "Company pages could not be read",
                extra={"count": len(failed), "tickers": failed},
            )

        # Anything DSE would not name a type for is excluded, but it is not
        # debt and must not be counted as such. Saying so is what keeps a
        # dropped instrument from passing for a routine exclusion - the sheet
        # is otherwise a row shorter with nothing to show for it.
        unclassified = sorted(
            p.ticker
            for p in profiles
            if p.instrument_type in company_profile.INDETERMINATE_TYPES
        )
        if unclassified:
            log.warning(
                "Instruments excluded with no usable type",
                extra={"count": len(unclassified), "tickers": unclassified},
            )

        listed = [p for p in profiles if p.instrument_type in EQUITY_TYPES]
        log.info(
            "Filtered to equities and mutual funds",
            extra={
                "kept": len(listed),
                "excluded_as_debt": len(profiles) - len(listed) - len(unclassified),
                "excluded_unclassified": len(unclassified),
            },
        )
        self._log_stale_interims(listed, day)

        frame = pd.DataFrame(
            [
                {
                    "Ticker": p.ticker,
                    "shares": p.outstanding_shares,
                    "ff_pct": p.free_float_pct,
                    "CLOSEP": closing.get(p.ticker),
                    "nav": p.nav_per_share,
                    "annualised_eps": p.annualised_eps,
                    "cash_dps": p.cash_dividend_per_share,
                    "total_dividend": p.total_dividend_pct,
                }
                for p in listed
            ]
        )

        self._log_unpriced(frame)
        frame = frame.dropna(subset=["shares", "CLOSEP"])
        frame = frame[frame["CLOSEP"] > 0]
        if frame.empty:
            raise NoTradingDataError(
                f"No instrument had both a closing price and a share count for "
                f"{day:%Y-%m-%d}."
            )

        mcap = frame["CLOSEP"] * frame["shares"] / MILLION
        ffmcap = mcap * frame["ff_pct"] / 100

        dsex = dse_index.fetch_dsex_constituents(session)
        in_dsex = frame["Ticker"].isin(dsex)
        dsex_total = mcap[in_dsex].sum()
        log.info(
            "DSEX weighting base",
            extra={
                "constituents_priced": int(in_dsex.sum()),
                "constituents_listed": len(dsex),
                "dsex_mcap_mn": round(float(dsex_total), 2),
            },
        )
        missing_from_sheet = sorted(dsex - set(frame["Ticker"]))
        if missing_from_sheet:
            log.warning(
                "DSEX constituents absent from the sheet",
                extra={"count": len(missing_from_sheet), "tickers": missing_from_sheet},
            )

        weight = pd.Series(pd.NA, index=frame.index, dtype="Float64")
        if dsex_total > 0:
            weight[in_dsex] = mcap[in_dsex] / dsex_total * 100

        frame["MCAP"] = mcap.round(2)
        frame["FFMCAP"] = ffmcap.round(2)
        frame["Index (%)"] = weight.round(4)

        self._add_valuation_columns(frame, session)

        frame = frame[COLUMN_ORDER].sort_values("Ticker").reset_index(drop=True)
        frame = self._append_total(frame)

        output_path = context.output_path("DSE Market Cap")
        frame.to_excel(output_path, index=False, sheet_name="Sheet1")
        log.debug("Wrote workbook", extra={"path": str(output_path), "rows": len(frame)})

        return RunResult(output_path=output_path, row_count=len(frame))

    def _add_valuation_columns(self, frame: pd.DataFrame, session) -> None:
        """Earnings, P/E, P/NAV and dividend columns.

        Two earnings bases, both expressed against the scraped day's close so
        every ratio on the row shares one price:

        * **Annualized** - the latest year-to-date interim EPS scaled to a full
          year, read straight off the company page. Matches DSE's own P/E 1 to
          within 0.05%, and unlike that ratio it still resolves for
          loss-makers, where DSE prints n/a.
        * **LTM** - recovered by inverting DSE's published Trailing P/E, which
          is the only route to it: a company page carries this year's interims
          and last year's audited accounts, but not last year's interims, so
          the trailing figure cannot be rebuilt from it. That inversion uses
          latest_PE.php's own close, since the page is always the live session
          and carries no date parameter.
        """
        pe_table = pe_ratios.fetch_pe_table(session)
        joined = frame.join(pe_table, on="Ticker")

        # EPS is a fundamental, so recover it at the price DSE divided by...
        ltm_eps = joined["pe_close"] / joined["trailing_pe"].where(
            joined["trailing_pe"] > 0
        )
        annualised_eps = joined["annualised_eps"]

        # ...then price every ratio off the day actually being scraped.
        close = frame["CLOSEP"]
        shares = frame["shares"]

        frame["LTM EPS"] = ltm_eps.round(4)
        frame["LTM Earnings"] = (ltm_eps * shares / MILLION).round(2)
        frame["LTM P/E"] = (close / ltm_eps.where(ltm_eps > 0)).round(2)

        frame["Annualized EPS"] = annualised_eps.round(4)
        frame["Annualized Earnings"] = (annualised_eps * shares / MILLION).round(2)
        frame["Annualized P/E"] = (
            close / annualised_eps.where(annualised_eps > 0)
        ).round(2)

        nav = frame["nav"].where(frame["nav"] > 0)
        frame["P/NAV"] = (close / nav).round(2)

        frame["Dividend Yield"] = (frame["cash_dps"] / close * 100).round(2)
        frame["Total Dividend"] = frame["total_dividend"].round(2)

        self._log_pe1_agreement(joined, annualised_eps)

        log.info(
            "Valuation columns",
            extra={
                "ltm_eps": int(frame["LTM EPS"].notna().sum()),
                "annualized_eps": int(frame["Annualized EPS"].notna().sum()),
                "p_nav": int(frame["P/NAV"].notna().sum()),
                "dividend_yield": int(frame["Dividend Yield"].notna().sum()),
                "rows": len(frame),
            },
        )

    def _log_pe1_agreement(self, joined: pd.DataFrame, annualised_eps) -> None:
        """Check our annualised EPS against DSE's own P/E 1.

        DSE defines P/E 1 as being based on the latest interim financials with
        basic EPS, which is what `annualised_eps` reconstructs. Dividing
        latest_PE.php's own close by our EPS should therefore reproduce its
        ratio — measured at 243/244 within 0.5% when the column was built.

        Both sides come off that page's price, not the scraped day's, since
        it is DSE's own ratio being reproduced. Nothing here reaches the
        sheet: it exists so a parsing regression in the interim table shows
        up as a warning instead of as quietly wrong earnings.
        """
        if "interim_pe" not in joined:
            log.debug("latest_PE.php carried no P/E 1 column, skipping the check")
            return

        published = joined["interim_pe"].where(joined["interim_pe"] > 0)
        implied = joined["pe_close"] / annualised_eps.where(annualised_eps > 0)
        divergence = ((implied - published).abs() / published).dropna()
        if divergence.empty:
            return

        off = divergence[divergence > PE1_TOLERANCE]
        log.info(
            "Annualized EPS agrees with DSE's P/E 1",
            extra={
                "compared": len(divergence),
                "within_tolerance": len(divergence) - len(off),
                "worst_divergence_pct": round(float(divergence.max()) * 100, 3),
            },
        )
        if len(off):
            log.warning(
                "Annualized EPS diverges from DSE's P/E 1",
                extra={
                    "count": len(off),
                    "tolerance_pct": PE1_TOLERANCE * 100,
                    "tickers": joined.loc[off.index, "Ticker"].tolist()[:15],
                },
            )

    def _log_stale_interims(self, profiles, day) -> None:
        """Flag annualised EPS built on a filing more than 15 months old.

        UNIONBANK's newest populated column is its FY2024 annual, so its
        "annualized" figure is really a year-and-a-half-old one. The number
        reads as current either way.
        """
        cutoff = day - dt.timedelta(days=15 * 30)
        stale = sorted(
            (p.ticker, p.annualised_period_end.isoformat())
            for p in profiles
            if p.annualised_eps is not None
            and p.annualised_period_end
            and p.annualised_period_end < cutoff
        )
        if stale:
            log.warning(
                "Annualized EPS based on a filing over 15 months old",
                extra={"count": len(stale), "oldest": stale[:15]},
            )

    def _log_unpriced(self, frame: pd.DataFrame) -> None:
        """Report anything dropped, so a silent shrinkage is visible."""
        for column, reason in (("shares", "no outstanding share count"), ("CLOSEP", "no closing price")):
            missing = frame[frame[column].isna()]["Ticker"].tolist()
            if missing:
                log.warning(
                    "Instruments excluded",
                    extra={"reason": reason, "count": len(missing), "tickers": missing[:25]},
                )
        zero_priced = frame[frame["CLOSEP"] == 0]["Ticker"].tolist()
        if zero_priced:
            log.warning(
                "Instruments excluded",
                extra={
                    "reason": "closing price of zero",
                    "count": len(zero_priced),
                    "tickers": zero_priced[:25],
                },
            )

    def _append_total(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Close the sheet with column totals.

        Summed from the rounded cells, not the underlying values, so that
        =SUM() over any column matches this row exactly. The cost is that
        Index (%) lands a rounding whisker off 100 rather than dead on it.

        MCAP and FFMCAP total every row; Index (%) only totals the DSEX
        constituents, since the sheet is wider than the index.
        """
        row = {"Ticker": TOTAL_LABEL}
        for column in TOTALLED_COLUMNS:
            places = 4 if column == "Index (%)" else 2
            row[column] = round(float(frame[column].dropna().sum()), places)
        return pd.concat([frame, pd.DataFrame([row])], ignore_index=True)
