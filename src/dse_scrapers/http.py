"""Shared HTTP session for every scraper.

dsebd.org and cdbl.com.bd are both plain HTTPS, but two things trip up a
default `requests` session on a Windows desktop:

1. Antivirus and corporate proxies that scan TLS re-sign every certificate
   with a root they install into the Windows trust store. certifi does not
   know about that root, so verification fails even though the browser is
   perfectly happy. We therefore verify against certifi *plus* the Windows
   store.
2. Python 3.13 turned on `VERIFY_X509_STRICT` by default. Those locally
   installed roots are frequently non-compliant in ways the public CAs are
   not (for example basicConstraints not marked critical), so strict mode
   rejects them. We clear that one flag and keep full chain verification.
"""

from __future__ import annotations

import os
import ssl
import threading
import time
from functools import lru_cache
from pathlib import Path

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from urllib3.util.retry import Retry

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DSE-Scrapers/1.0"

# For hosts that serve a stripped or different page to non-browser agents —
# investing.com and tradingeconomics.com both do. dsebd.org and cdbl.com.bd
# do not care, so they get the honest USER_AGENT above.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 90

# Rebuild the bundle at most once a day, so a newly installed root is picked
# up without regenerating it on every session.
BUNDLE_MAX_AGE_SECONDS = 24 * 60 * 60

_BUNDLE_LOCK = threading.Lock()


def _ca_bundle(cache_dir: Path) -> Path:
    """A CA bundle combining certifi with the local machine's roots.

    Written through a temporary file and an atomic replace: scrapers build
    sessions on several threads at once, and a reader must never see a
    half-written bundle.
    """
    bundle = cache_dir / "ca_bundle.pem"

    with _BUNDLE_LOCK:
        if bundle.exists() and bundle.stat().st_size > 0:
            if time.time() - bundle.stat().st_mtime < BUNDLE_MAX_AGE_SECONDS:
                return bundle

        parts = [Path(certifi.where()).read_text(encoding="utf-8").strip()]

        if hasattr(ssl, "enum_certificates"):  # Windows only
            for store in ("ROOT", "CA"):
                try:
                    certificates = ssl.enum_certificates(store)
                except OSError:
                    continue
                for der, encoding, _trust in certificates:
                    if encoding == "x509_asn":
                        parts.append(ssl.DER_cert_to_PEM_cert(der).strip())

        cache_dir.mkdir(parents=True, exist_ok=True)
        staging = bundle.with_name(f"ca_bundle.{os.getpid()}.tmp")
        staging.write_text("\n".join(parts) + "\n", encoding="ascii")
        os.replace(staging, bundle)

    return bundle


@lru_cache(maxsize=None)
def _ssl_context(cache_dir: Path) -> ssl.SSLContext:
    """One context per cache directory, shared across threads.

    SSLContext is safe to share, and building it is the expensive part.
    """
    context = ssl.create_default_context(cafile=str(_ca_bundle(cache_dir)))
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


class _ContextAdapter(HTTPAdapter):
    def __init__(self, context: ssl.SSLContext, **kwargs) -> None:
        self._context = context
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        kwargs["ssl_context"] = self._context
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize, block=block, **kwargs
        )


def build_session(cache_dir: Path) -> requests.Session:
    """A session that retries transient failures and trusts local roots."""
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.mount(
        "https://", _ContextAdapter(_ssl_context(cache_dir), max_retries=retry)
    )
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def get_text(session: requests.Session, url: str, **kwargs) -> str:
    """GET a URL and return decoded text, raising on a non-200."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    response = session.get(url, **kwargs)
    response.raise_for_status()
    return response.text
