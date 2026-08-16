"""The DSE news archive, shared by every disclosure-derived source.

`news_archive.php` is only a form; the endpoint behind it is `old_news.php`:

    old_news.php?startDate=&endDate=&criteria=4&archive=news   every instrument
    old_news.php?inst=<CODE>&criteria=3&archive=news           one instrument

The date-range form returns all instruments at once, so a window costs a
single request — though the payload grows fast: ~22 KB for two days, ~1.8 MB
for ninety. History is capped at two years like the rest of the archive.

Items flatten to a fixed four-field shape, which is what `ITEM` matches:

    Trading Code: X  News Title: Y  News: Z  Post Date: YYYY-MM-DD
"""

from __future__ import annotations

import re

OLD_NEWS_URL = "https://www.dsebd.org/old_news.php"

ITEM = re.compile(
    r"Trading Code:\s*(?P<code>\S+)\s*"
    r"News Title:\s*(?P<title>.*?)\s*"
    r"News:\s*(?P<body>.*?)\s*"
    r"Post Date:\s*(?P<posted>\d{4}-\d{2}-\d{2})",
    re.DOTALL,
)
