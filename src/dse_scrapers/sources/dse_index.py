"""Index membership, from the per-index constituent boards on dsebd.org.

Only DSEX membership is published as a column — `market-cap`'s `Index (%)` is
a share of the DSEX total. DS30's board is the same shape at
`dse30_share.php`, should a blue-chip weighting ever be wanted.
"""

from __future__ import annotations

import logging

import requests

from ..http import get_text
from ._html import trading_codes

log = logging.getLogger(__name__)

DSEX_URL = "https://www.dsebd.org/dseX_share.php"


def fetch_dsex_constituents(session: requests.Session) -> set[str]:
    """Trading codes currently in the DSEX broad index."""
    codes = set(trading_codes(get_text(session, DSEX_URL)))
    log.debug("DSEX constituents", extra={"count": len(codes)})
    return codes
