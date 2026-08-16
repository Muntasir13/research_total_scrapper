"""Hydra entry point: pick a scraper, pick a date, run it.

Hydra owns the run directory (`outputs/<date>/<time>/`), so the workbook, the
JSON log and a snapshot of the resolved config all land together.

Overrides use Hydra's `key=value` form:

    python main.py scraper=trade-block date=2026-08-12
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import textwrap

import hydra
from hydra.core.hydra_config import HydraConfig
from omegaconf import DictConfig

from . import base
from .base import PROJECT_ROOT, RunContext
from .errors import ScraperError

# Importing the package registers every scraper on the menu.
from . import scrapers  # noqa: F401

log = logging.getLogger(__name__)

# Absolute, because Hydra resolves a relative config_path against the *module*
# path when the decorated function lives outside __main__ — which would send it
# looking for a `config` package rather than the directory at the project root.
CONFIG_PATH = str(PROJECT_ROOT / "config")


class InvalidDateError(ScraperError):
    """The date override could not be read."""


def parse_day(value: object) -> dt.date:
    """Accept a date object, an ISO string, or the words today and yesterday."""
    if isinstance(value, dt.date):
        return value

    text = str(value).strip().lower()
    if text in ("", "today", "none", "null"):
        return dt.date.today()
    if text == "yesterday":
        return dt.date.today() - dt.timedelta(days=1)
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise InvalidDateError(
            f"'{value}' is not a date. Use YYYY-MM-DD, today or yesterday."
        ) from None


# Wide enough that a two-word column name like "Change %" is not split across
# lines, narrow enough to sit inside an 80-column console.
MENU_WIDTH = 78


def _menu_text() -> str:
    """The scraper list, rendered as the prompt for input()."""
    lines = ["", "  DSE Scrapers", "  " + "=" * (MENU_WIDTH - 4)]
    for number, scraper in enumerate(base.available(), start=1):
        lines.append(f"  {number}. {scraper.title}  [{scraper.key}]")
        lines.extend(
            textwrap.wrap(
                ", ".join(scraper.columns),
                width=MENU_WIDTH,
                initial_indent="       ",
                subsequent_indent="       ",
            )
        )
        lines.append("")
    lines.append("  q. Quit")
    lines.append("")
    lines.append(f"  Select a scraper (1-{len(base.available())}, or q): ")
    return "\n".join(lines)


def choose_scraper() -> type[base.Scraper] | None:
    """Loop until the user picks a listed scraper or quits."""
    options = base.available()
    while True:
        answer = input(_menu_text()).strip().lower()
        if answer in ("q", "quit", "exit"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        log.warning("Not on the menu", extra={"choice": answer})


def choose_day() -> dt.date | None:
    today = dt.date.today()
    while True:
        answer = input(f"  Trading date [{today:%Y-%m-%d}]: ").strip()
        if answer.lower() in ("q", "quit", "exit"):
            return None
        try:
            return parse_day(answer)
        except InvalidDateError as error:
            log.warning(str(error), extra={"input": answer})


def execute(
    scraper_class: type[base.Scraper], trading_day: dt.date, context: RunContext
) -> int:
    """Run one scraper and log what it produced. Returns an exit code."""
    log.info(
        "Starting scraper",
        extra={
            "scraper": scraper_class.key,
            "trading_date": trading_day.isoformat(),
            "run_dir": str(context.run_dir),
        },
    )
    try:
        result = scraper_class().run(context)
    except ScraperError as error:
        log.error(
            str(error),
            extra={"scraper": scraper_class.key, "trading_date": trading_day.isoformat()},
        )
        return 1
    except Exception:
        log.exception(
            "Scraper failed unexpectedly", extra={"scraper": scraper_class.key}
        )
        return 1

    published = context.publish()
    log.info(
        "Finished",
        extra={
            "scraper": scraper_class.key,
            "rows": result.row_count,
            "workbook": str(result.output_path),
            "extra_files": [str(path) for path in result.extra_files],
            "published": [str(path) for path in published],
        },
    )
    return 0


@hydra.main(version_base=None, config_path=CONFIG_PATH, config_name="default")
def main(cfg: DictConfig) -> None:
    """Hydra passes the resolved config; the exit code goes out via SystemExit."""
    run_dir = HydraConfig.get().runtime.output_dir

    if cfg.list_scrapers:
        for scraper in base.available():
            log.info(
                "Available scraper",
                extra={
                    "key": scraper.key,
                    "title": scraper.title,
                    "columns": scraper.columns,
                },
            )
        sys.exit(0)

    if cfg.scraper:
        try:
            scraper_class = base.get(str(cfg.scraper))
        except KeyError as error:
            log.error(error.args[0], extra={"requested": str(cfg.scraper)})
            sys.exit(2)
    else:
        scraper_class = choose_scraper()
        if scraper_class is None:
            log.info("No scraper selected, exiting")
            sys.exit(0)

    try:
        trading_day = parse_day(cfg.date) if cfg.scraper else choose_day()
    except InvalidDateError as error:
        log.error(str(error), extra={"date": str(cfg.date)})
        sys.exit(2)
    if trading_day is None:
        log.info("No date selected, exiting")
        sys.exit(0)

    context = RunContext(
        trading_day=trading_day, run_dir=run_dir, scraper_key=scraper_class.key
    )
    sys.exit(execute(scraper_class, trading_day, context))
