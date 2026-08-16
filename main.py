"""Entry point: python main.py [scraper=<key>] [date=<YYYY-MM-DD>]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from dse_scrapers.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
