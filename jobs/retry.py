"""
Shared reliability helpers for calls to external services (Dawarich,
Immich). The goals these exist to satisfy:

- No call blocks forever: every attempt carries a hard HTTP timeout
  (Config.HTTP_TIMEOUT_SECONDS) — this module doesn't set timeouts
  itself, but every caller must pass one to `requests`.
- No runaway loop: retries are bounded (Config.EXTERNAL_MAX_ATTEMPTS),
  and CircuitBreaker bounds how many resources a batch job will try
  against an already-down service before giving up for this run.
- Recoverable without intervention: giving up just means "the next
  scheduled run tries again" — nothing needs a manual retry, and
  nothing is lost (state that would need retrying is never marked
  as done).
"""
import random
import re
import time
from urllib.parse import urlsplit

import requests

from config import Config


class ServiceUnavailable(Exception):
    """Raised when an external call exhausts its retries."""


class ServiceRejected(ServiceUnavailable):
    """The service answered but refused the request (a 4xx): a configuration problem such as a wrong URL,
    a plain-HTTP URL for an HTTPS-only server, or a bad API key. Retrying won't help, but the message says why.
    A subclass of ServiceUnavailable so every existing caller treats it as "couldn't get an answer, try later"."""


_SECRET_PARAM = re.compile(r"((?:api[_-]?key|token|secret|password)=)[^&\s'\")]+", re.IGNORECASE)


def scrub(text):
    """Remove secret-looking query parameters from a message before it is logged, stored or shown.
    requests puts the full URL in its exception text, and Dawarich takes its API key as a query parameter."""
    return _SECRET_PARAM.sub(r"\1***", str(text))


def _rejection_message(response):
    """'HTTP 400 from https://host: <title or first words of the body>' with no query string."""
    parts = urlsplit(response.url or "")
    where = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "the service"
    body = response.text or ""
    m = re.search(r"<title>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    snippet = re.sub(r"\s+", " ", (m.group(1) if m else body)).strip()[:160]
    return scrub(f"HTTP {response.status_code} from {where}: {snippet}" if snippet else f"HTTP {response.status_code} from {where}")


def call_with_retry(fn, max_attempts=None, base_delay=None, max_delay=8):
    """
    Calls fn() (a zero-arg callable wrapping one HTTP request), retrying
    on connection errors, timeouts, and 5xx responses with exponential
    backoff + jitter. Does NOT retry 4xx responses — a bad request or
    bad API key won't be fixed by trying again, and retrying it just
    delays surfacing a config problem. Raises ServiceUnavailable if
    every attempt fails.
    """
    max_attempts = max_attempts or Config.EXTERNAL_MAX_ATTEMPTS
    base_delay = base_delay if base_delay is not None else Config.EXTERNAL_RETRY_BASE_DELAY

    last_exception = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500:
                raise ServiceRejected(_rejection_message(e.response)) from None   # `from None`: the chained error holds the URL
            last_exception = e
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exception = e

        if attempt < max_attempts:
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.1)  # jitter
            time.sleep(delay)

    raise ServiceUnavailable(scrub(last_exception)) from None


class CircuitBreaker:
    """
    Tracks consecutive failures within a single batch job run only —
    nothing here persists between runs. Once `tripped`, the caller
    should stop processing the rest of the batch: the service is
    almost certainly down entirely, not just failing for one
    resource, so trying every remaining item is slow and pointless.
    The next scheduled run starts a fresh breaker and simply picks up
    whatever's still unprocessed.
    """
    def __init__(self, threshold=None):
        self.threshold = threshold or Config.EXTERNAL_CIRCUIT_THRESHOLD
        self.consecutive_failures = 0

    @property
    def tripped(self):
        return self.consecutive_failures >= self.threshold

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
