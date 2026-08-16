"""Exceptions raised by the scrapers, worded for the person at the prompt."""

from __future__ import annotations


class ScraperError(Exception):
    """Base class for every failure the CLI should report without a traceback."""


class NoTradingDataError(ScraperError):
    """DSE published no trade rows for the requested date."""


class BlockDataUnavailableError(ScraperError):
    """Block trades for the requested date were never captured.

    dsebd.org only ever serves the latest session in mst.txt, so anything
    older has to come out of the local archive.
    """
