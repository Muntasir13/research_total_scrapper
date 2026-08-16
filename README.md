# DSE Scrapers

Scrapers for Dhaka Stock Exchange data. Each one exports **one dataset, for one
trading day, to one Excel file**. Pick which to run from a menu at the command
prompt.

## Scrapers

### `trade-block` — Daily Trade and Block data

One row per instrument DSE published for the day, in the column order the IDLC
SL Uptick app expects:

| Column                                                             | Source                                                                                                                                           |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `TradingDate`                                                      | A real Excel date cell, formatted `YYYY-MM-DD`. The original Uptick workbook stored a bare serial (`46246`); this diverges from it deliberately. |
| `Exchange`                                                         | literal `DSE`                                                                                                                                    |
| `InstrumentName`                                                   | trading code                                                                                                                                     |
| `ISIN`                                                             | CDBL, matched by company name                                                                                                                    |
| `LTP` `HIGH` `LOW` `OPENP` `CLOSEP` `YCP` `Trade` `Value` `Volume` | `day_end_archive.php` — **public market only**                                                                                                   |
| `MaxPrice` `MinPrice` `Trades` `Quantity` `ValueInMn`              | `mst.txt` — **block market**, zero when the scrip had none                                                                                       |

The two halves are additive, not overlapping. On 2026-08-12 the public columns
summed to 267,572 trades and the block columns to 102, against the 267,674 DSE
reports as the day's total.

`Total Share`, `FreeFloat` and `DSEX` from the older Uptick sheet are **not**
produced here — they have no source on dsebd.org and belong to a later scraper.

### `company-description` — Company Description

One row per listed instrument: `Ticker, Category, Sector, Year End,
Outstanding Shares, FF%, Sponsor/Director, Govt, Institute, Foreign, Public`.
Built from one `displayCompany.php` page per instrument (637 of them, ~75
seconds across 6 threads).

**Free float is not published as a number.** DSE gives a shareholding split
and the market convention is everything outside sponsor and government hands:

```
FF% = 100 - Sponsor/Director - Govt
```

The five components of that split are columns in their own right, so the free
float can be checked against its own inputs. All five come from the same
snapshot and total 100. Two integrity checks run each time and log a warning
on failure: that the components sum to 100, and that they reproduce `FF%`. On
13 August 2026 all 401 instruments carrying a split passed both.

Each page carries three shareholding snapshots and the **first is a year-end
figure that can be years stale** (YPL's is Jun 2021), so the scraper always
takes the block with the latest date. Snapshots that are entirely zeros are
placeholders, not a real 0% split, and are skipped. Anything whose newest
snapshot is over a year old is logged as a warning — the number looks current
either way.

Blanks are expected: treasury bonds and corporate bonds have no year end and
no shareholding (236 instruments, blank across FF% and all five components
together), and a couple of old debentures publish `-` for outstanding shares.

### `market-cap` — Market Capitalisation

`Ticker, MCAP, FFMCAP, Index (%)`, closed by a Total row.

```
MCAP   = closing price x outstanding shares      (BDT millions)
FFMCAP = MCAP x FF% / 100                        (BDT millions)
```

Closing price comes from `day_end_archive.php` for the trading day; shares and
free float from the same company pages as `company-description`.

Covers **equities and mutual funds only** — corporate bonds, debentures and
government securities are dropped on `Type of Instrument`, which took 637
instruments down to 395.

`Index (%)` is each stock's share of the **DSEX** total market cap, so it is
blank for the 76 rows outside the index. The Total row sums the rounded cells
rather than the underlying values, so `=SUM()` over a column matches it
exactly; that leaves Index (%) a rounding whisker off 100 (100.0006 across 319
constituents) rather than dead on it. Only MCAP, FFMCAP and Index (%) are
totalled — ratios and per-share figures cannot be summed, and an earnings
total is swamped by a couple of distressed banks (FIRSTSBANK alone annualises
to −767bn).

**Valuation columns.** Two earnings bases, both priced off the scraped day's
close so every ratio on a row shares one price:

|                | Source                                                                           | Coverage  |
| -------------- | -------------------------------------------------------------------------------- | --------- |
| **Annualized** | latest year-to-date interim EPS x 12 / months elapsed, read off the company page | 381 / 395 |
| **LTM**        | DSE's published Trailing P/E, inverted to an EPS                                 | 213 / 395 |

Annualized is computed rather than taken from DSE's P/E 1 because DSE prints
`n/a` for loss-makers; computing it directly still yields a real negative, and
it agrees with P/E 1 to within 0.25% where both exist (243/244 within 0.5%).

LTM has to come from inverting DSE's ratio: a company page carries the current
year's interims and last year's audited accounts, but **not** last year's
interims, so a trailing figure cannot be rebuilt from it. That inversion uses
`latest_PE.php`'s own close, since that page is always the live session and
takes no date parameter. It is blank wherever DSE prints `n/a`.

```
LTM/Annualized Earnings = EPS x outstanding shares      (BDT millions)
P/E                     = close / EPS                   (blank when EPS <= 0)
P/NAV                   = close / NAV per share         (latest year, 5-year table)
Dividend Yield          = cash dividend % x par / close (%)
Total Dividend          = cash % + stock %, latest year that declared either
```

Loss-makers keep their negative EPS and earnings but get a blank P/E — 138
rows. Annualized EPS built on a filing more than 15 months old is logged as a
warning; 62 instruments qualify, and BDWELDING's newest is from 2019.

### `mf-nav` — Mutual Fund NAV

`MF, Date, Cost Price / NAV, Mkt Price / Nav` — one row per listed mutual
fund (35), with per-unit NAV on both bases.

There is no NAV page on dsebd.org; funds disclose it as news. The archive
search behind `news_archive.php` is `old_news.php`, and its date-range form
returns every instrument at once, so a whole window is one request:

```
old_news.php?startDate=&endDate=&criteria=4&archive=news
```

Each disclosure reads _"On the close of operation on 12-Aug-2026 ... Tk. 6.56
per unit on the basis of current market price and Tk. 11.51 per unit on the
basis of cost price"_. Three things make the parse fiddly:

- Both bases appear **twice** — once per unit, once as total net assets — so
  the figures are anchored on `per unit`.
- The two are matched independently, so sentence order cannot break them.
- Titles vary (`Daily NAV`, `Mutual Fund Daily NAV`) and cannot identify these
  items anyway, because NAVANAPHAR's ticker contains "NAV". A body that parses
  is the test.

`Date` is the _close of operation_ date the NAV is as of, not the publication
date — funds disclose the following day. A fund that skips a day carries its
last NAV forward, which is what the date column is for; those are logged.

The fund universe comes from the sector board
(`by_industrylisting.php` → `ltp_industry.php?area=<id>`), two requests rather
than a company page each. 28 of the 35 funds disclose NAV; the other 7 have
published none in 90 days and appear as blank rows.

### `index-levels` — Index Levels

`Index, Country, Date, Yesterday Value, Today Value, Change, Change %` — 19
indices, five Bangladeshi and fourteen international.

**This scraper ignores the date you pass.** None of its sources can be asked
for a historical level, so it always reports the most recent session and
writes each row's real as-of date. Rows legitimately carry different dates:
markets close in different timezones, so DSE and Pakistan may be a day behind
the US and Europe on the same run.

Three sources:

|                                | Source          | Previous close                     |
| ------------------------------ | --------------- | ---------------------------------- |
| DSEX, DS30, DSES, CDSET, DSMEX | DSE homepage    | first point of the intraday series |
| 13 international               | investing.com   | `last - change`                    |
| CSE All-Share                  | cse.lk JSON API | `value - change`                   |

**Bangladesh** — DSE's index graphs are driven by intraday series embedded
inline as `index_value_*` JavaScript variables. The first point of a series is
the _previous_ close, seeded at 09:59 before the open; the last is the current
level. Verified against `recent_market_information.php`: for DSEX, DSES and
DS30 the first point equals the prior day's published close exactly, which is
what licenses the same trick for CDSET and DSMEX — neither appears in any
daily table. DSEX's variable is `index_value_dsbi`, after the old "DSE Broad
Index".

**International** — investing.com embeds a `__NEXT_DATA__` JSON payload with
the instrument's price object. The previous close is derived as
`last - change` rather than read from `lastClose`, because **once a market
closes investing.com rolls `lastClose` forward to that same close** — FTSE,
Nikkei and Hang Seng were all reporting `lastClose == last` at the time of
writing. The subtraction is correct in both states; checked against each
index's own reported change.

**Sri Lanka** — investing.com resolves `cse-all-share` but serves nothing but
zeros, so this one comes from the Colombo exchange's own API.

### `commodity-prices` — Commodity Prices

`Commodity, Unit, Date, Previous, Price, Change, Change %` — 13 benchmarks
from `tradingeconomics.com/commodities`, listed in the order requested.

**One request serves all 13.** Trading Economics renders every commodity it
tracks into server-side HTML on that single page, so the individual
`/commodity/<slug>` pages are never fetched. Rows are keyed by the name in the
first cell; `Previous` is backed out as `Price - Change`.

**This scraper ignores the date you pass**, for the same reason as
`index-levels` — the page publishes only the current quote. It reports the
latest observation and stamps each row with the date that observation belongs
to.

**Rows carry different dates by design.** Trading Economics dates each row
with its own last observation, so the traded futures print today while the
assessed physical benchmarks lag: Coal, LNG JKM and Iron Ore are routinely a
day behind Brent and Gold. `Change` is the move into that row's own date, not
a common session. Anything more than 5 days behind the run date is logged as a
warning, since the page keeps showing a stale assessment indefinitely and it
looks exactly like a live one.

**The date cell has no year** — it is always `Mon/DD`. The year is inferred as
the current one, rolled back when that would land in the future, which is what
a stale December quote read in January needs.

Two names needed resolving against the page:

| Requested                       | Row used   | Why                                                                                             |
| ------------------------------- | ---------- | ----------------------------------------------------------------------------------------------- |
| LNG Japan / Korea Marker PLATTS | `LNG JKM`  | `/commodity/liquefied-natural-gas-japan-korea` — the page confirms it is the Japan-Korea Marker |
| Iron Ore                        | `Iron Ore` | 62% Fe CFR China in USD/T, not `Iron Ore CNY` (Dalian, CNY/T)                                   |
| Steel                           | `Steel`    | Shanghai rebar in CNY/T, not `HRC Steel` or `Scrap Steel`                                       |

Units are not normalised — they are whatever Trading Economics quotes in, and
they are not all USD (UK Gas is GBp/thm, Steel is CNY/T). The `Unit` column is
what makes a price meaningful, so keep it alongside.

### `record-dates` — Record Dates

`Ticker, Record Date, Cash Dividend, Stock Dividend, EPS, AGM date, NAV,
EntryDate` — one row per instrument whose record date DSE published on the run
date, from the `old_news.php` archive.

**Most days are empty, by design.** Over the 210 days to 15 August 2026 only
62 carried a record date at all; a run that finds none writes the header row
and nothing else. Dividend season accounts for most of the traffic — 19
instruments landed on 3 May 2026 alone.

Bond coupon-payment and trading-suspension notices are excluded. Both quote a
record date, but coupon servicing is not a corporate action on a stock, and a
suspension notice only repeats a date already announced.

Four things about the feed drive the implementation:

**Announcements are split across separate news items.** A long one ends
`(cont.)` and a second item, same trading code and post date, opens
`(Cont. News of TICKER)`. The figures are often only in the second —
MIDASFIN's declaration carries the AGM and record date in the first item and
its EPS and NAV in the continuation — so items are reassembled before any
field is read.

**A record date is always in the future.** DSE publishes the odd wrong one and
corrects it: BNICL's declaration on 19 Apr 2026 read `Record Date: 13.05.2025`,
corrected hours later to `13.05.2026`. Discarding candidates that predate the
announcement resolves that without having to recognise corrections by title.
A revision that moves a date off a holiday is read too — Marico's `25 May`
became `1 June 2026`, and the file reports the revised date.

**Cash Dividend is what this record date entitles holders to**, not the year's
headline figure. Matches are taken in the order they appear, so Marico's
"Final Cash Dividend of 500% ... (Total 2075% ... inclusive of 1575% Interim)"
records the 500%. Where the declared figure _contains_ an interim already
paid, that interim is deducted: LHB's "40% Final Cash Dividend (including 18%
interim cash dividend which has already been paid)" records **22%**.

The deduction only applies when the inclusive clause qualifies the declared
figure itself. Marico and GP both state a total in a parenthetical — "500% ...
(Total 2075% ... inclusive of 1575% Interim)" — where the inclusion belongs to
the total and the declared final is already net; another percentage between
the figure and the clause is what marks that case. "in addition to the interim
dividend of 143%" is the opposite construction and never triggers it.

Percentages carry thousands separators at the top end — Reckitt Benckiser
declared `1,730%`, which reads as 730% if the comma is dropped.

**Figures are backfilled, but only for dividend events.** A company often
declares a dividend saying the record date will follow, then publishes it
weeks later; that later notice is what puts the instrument in the file, so the
dividend, EPS, AGM date and NAV are read back from its own earlier
announcements — one extra request per instrument, not a wider window. Events
with a record date but no dividend of their own — rights issues, EGMs, bond
issues — are excluded from backfill, or Technodrug's EGM to approve a bond
would inherit the dividend it declared months earlier. A backfilled AGM date
earlier than the record date is dropped for the same reason.

## Setup

The virtualenv lives in `.venv/` (`poetry.toml` sets `in-project = true`) and
must be **Python 3.13** — Hydra 1.3.5 crashes on 3.14's stricter `argparse`.

```bash
poetry env use "C:\Users\ASUS\AppData\Local\Programs\Python\Python313\python.exe"
.venv\Scripts\python.exe -m pip install -e .
```

`poetry install` cannot fetch new packages on this machine — see _Things worth
knowing_. pip works, so use it for dependency changes.

## Running

Configuration is [Hydra](https://hydra.cc), so overrides use `key=value`:

```bash
python main.py                                  # interactive menu
python main.py list_scrapers=true               # log the available keys
python main.py scraper=trade-block              # today
python main.py scraper=trade-block date=2026-08-12
```

`date` takes `YYYY-MM-DD`, `today` or `yesterday`. Defaults live in
`config/default.yaml`.

## Output and logging

Every file is written twice, to two folders with different jobs.

**`outputs/` holds only the newest of each dataset**, one folder per scraper,
under a stable filename. Each run replaces what is there:

```
outputs/
  trade-block/          DSE Trade and Block.xlsx
                        DSE Trade and Block missing-isin.csv
  company-description/  DSE Company Description.xlsx
  market-cap/           DSE Market Cap.xlsx
  mf-nav/               DSE Mutual Fund NAV.xlsx
  index-levels/         DSE Index Levels.xlsx
  commodity-prices/     Commodity Prices.xlsx
```

**`logs/` is the history** — Hydra creates a directory per run and nothing in
it is ever overwritten:

```
logs/2026-08-13/09-10-06/
  DSE Market Cap_2026-08-12.xlsx      dated copy of what that run produced
  scraper.log                         JSON log for the run
  .hydra/config.yaml, hydra.yaml, overrides.yaml
```

`.hydra/` is Hydra's snapshot of the resolved config, so any output can be
traced back to exactly what produced it.

There are no `print` statements — everything goes through `logging`, formatted
as JSON by `python-json-logger` (configured in
`config/hydra/job_logging/logger.yaml`):

```json
{
  "timestamp": "2026-08-12 23:36:57,157",
  "level": "INFO",
  "logger": "dse_scrapers.scrapers.trade_and_block",
  "message": "Fetched public-market trades",
  "instruments": 637,
  "source": "day_end_archive.php"
}
```

Context goes in `extra={...}` and becomes top-level JSON keys, so the logs can
be queried rather than grepped. Console gets INFO and above; `scraper.log` also
gets DEBUG.

The one thing not logged is the interactive menu itself, which is the prompt
string passed to `input()` — JSON-formatted menu lines would be unreadable to
choose from.

## Things worth knowing

**Company pages are retried until they are usable.** DSE occasionally serves a
`displayCompany.php` response that returns 200 and parses without error but
carries no record — either the body is cut short (one arrived at 83 KB against
a ~300 KB floor) or the _Type of Instrument_ cell reads the literal `Unknown`.
Roughly 1 fetch in 2,000, which over 637 instruments landed in about a third of
runs.

Left alone this was silent data loss: `market-cap` keeps only recognised
instrument types, so an `Unknown` was filed as debt and the instrument vanished
from the sheet. The same date produced 396, 395 and 394 rows on different runs,
each time losing a different company — and the only visible symptom was a
_DSEX constituents absent_ warning, which fires solely when the lost instrument
happens to be an index constituent. Non-constituents disappeared with no log
line at all.

`fetch_profiles` now checks each response for the closing `</body>` and for a
usable type, and refetches up to `PROFILE_ATTEMPTS` (3) times. `Unknown` is
never a real type — 2,548 consecutive fetches produced none — so retrying on it
is safe. A page that never comes good is reported: an always-truncated one
lands in the `failed` list, and a persistent `Unknown` is logged by `market-cap`
as `excluded_unclassified` rather than being counted among the debt exclusions.

**Block data has no archive.** `mst.txt` only ever holds the latest session, so
every run saves a copy to `data/block_archive/mst_YYYY-MM-DD.txt`, filed under
the date the bulletin itself reports. Past dates are read back from there, and
a date that was never captured fails with a clear message rather than a file
full of zeros. History therefore starts from the first run.

**ISINs are matched by company name.** CDBL publishes ISINs at
`cdbl.com.bd/isin.php` with no trading-code column, so codes are matched
through the company name DSE gives them. Matching is exact-after-normalisation
and covers about 91% of instruments; treasury bonds, perpetual bonds and
debentures mostly miss. Loosening it to prefix or fuzzy matching would reach
~98% while quietly assigning AB Bank's equity ISIN to `ABBLPBOND` — a blank is
recoverable, a plausible wrong ISIN is not.

Unmatched instruments are written beside the output as
`..._missing-isin.csv`. Fill any of them in permanently by adding rows to
`data/isin_overrides.csv`, which always wins over CDBL:

```csv
InstrumentName,ISIN
ABBLPBOND,BD2013ABBPB8
```

**TLS on this machine.** Avast's mail/web shield re-signs HTTPS with a root
that certifi doesn't carry, and Python 3.13 rejects it under
`VERIFY_X509_STRICT`. `src/dse_scrapers/http.py` verifies against certifi plus
the Windows trust store and clears that one strict flag; chain verification
stays on.

The same root is why **`poetry add` / `poetry lock` fail** with what looks like
a dead network. Without a cert override poetry reports `unable to get local
issuer certificate`; pointed at a bundle containing the Avast root it reports
`Basic Constraints of CA cert not marked critical`. `poetry install` still works
when everything it needs is already in its cache. pip is unaffected — it uses
the Windows trust store via `truststore` — so install dependency changes with
pip and treat `poetry.lock` as stale for `hydra-core` and `python-json-logger`
until poetry can reach PyPI.

## Adding a scraper

Drop a module in `src/dse_scrapers/scrapers/`, subclass `Scraper`, decorate it
with `@register`, and import it from that package's `__init__.py`. It appears
on the menu automatically. Reusable fetch-and-parse code belongs in
`src/dse_scrapers/sources/`.

Set `columns = COLUMN_ORDER` on the class rather than restating the column
names: the menu lists a scraper by its columns, and pointing it at the same
constant the workbook is built from means the two cannot drift apart.

## Layout

```
main.py                     entry point
config/
  default.yaml              scraper, date, Hydra run directory
  hydra/job_logging/
    logger.yaml             JSON formatter, console and file handlers
src/dse_scrapers/
  base.py                   Scraper contract, RunContext, registry
  cli.py                    Hydra entry point and interactive menu
  http.py                   shared session, TLS handling
  errors.py                 failures reported without a traceback
  sources/                  one module per upstream source
    _html.py                shared page-trimming, table regexes, code extraction
    _news.py                the old_news.php endpoint and its item shape
  scrapers/                 one module per output file
data/
  block_archive/            raw mst.txt, one per session
  cache/                    CDBL directory, CA bundle
  isin_overrides.csv        hand-pinned ISINs
outputs/<scraper>/          newest file only, replaced each run
logs/<date>/<time>/         run history: dated copy, log, config snapshot
```
