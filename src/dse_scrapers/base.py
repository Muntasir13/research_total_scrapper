"""The contract every scraper follows, plus the registry the menu reads."""

from __future__ import annotations

import datetime as dt
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import requests

from .http import build_session

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RunContext:
    """Everything a scraper needs to produce one day's file.

    Files are written twice. `run_dir` is Hydra's directory under `logs/`,
    where a dated copy sits beside that run's log and config snapshot and is
    never overwritten. They are then published to `outputs/<scraper>/` under a
    stable name, so that folder always holds only the newest of each dataset.
    """

    trading_day: dt.date
    run_dir: Path
    scraper_key: str
    _published: dict[Path, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_dir", Path(self.run_dir))

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def latest_dir(self) -> Path:
        """Where this scraper's newest output lives."""
        return PROJECT_ROOT / "outputs" / self.scraper_key

    @cached_property
    def session(self) -> requests.Session:
        return build_session(self.cache_dir)

    def output_path(self, stem: str, suffix: str = ".xlsx") -> Path:
        """A dated path in the run directory, registered for publishing."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"{stem}_{self.trading_day:%Y-%m-%d}{suffix}"
        self._published[path] = f"{stem}{suffix}"
        return path

    def publish(self) -> list[Path]:
        """Copy this run's files to outputs/<scraper>/, replacing what's there."""
        if not self._published:
            return []
        self.latest_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for source, stable_name in self._published.items():
            if source.exists():
                target = self.latest_dir / stable_name
                shutil.copy2(source, target)
                written.append(target)
        return written


@dataclass
class RunResult:
    """What the CLI logs once a scraper finishes."""

    output_path: Path
    row_count: int
    extra_files: list[Path] = field(default_factory=list)


class Scraper(ABC):
    """One dataset, one trading day, one Excel file."""

    key: str
    title: str
    # The sheet's columns, in order. Point this at the scraper's own
    # COLUMN_ORDER rather than restating it, so the menu cannot drift from
    # what the workbook actually contains.
    columns: list[str]

    @abstractmethod
    def run(self, context: RunContext) -> RunResult:
        """Fetch, assemble and write the day's file."""


_REGISTRY: dict[str, type[Scraper]] = {}


def register(scraper: type[Scraper]) -> type[Scraper]:
    """Add a scraper to the menu. Used as a class decorator."""
    if scraper.key in _REGISTRY:
        raise ValueError(f"Duplicate scraper key: {scraper.key}")
    _REGISTRY[scraper.key] = scraper
    return scraper


def available() -> list[type[Scraper]]:
    """Registered scrapers, in registration order."""
    return list(_REGISTRY.values())


def get(key: str) -> type[Scraper]:
    try:
        return _REGISTRY[key]
    except KeyError:
        known = ", ".join(_REGISTRY) or "none registered"
        raise KeyError(f"Unknown scraper '{key}'. Available: {known}") from None
