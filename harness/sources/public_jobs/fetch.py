"""The only place in this package that touches the network.

Everything here is a bound, not a feature:

* HTTPS on port 443 only, to an allowlisted host, on an allowlisted path;
* ``robots.txt`` checked first and cached per host, failing closed;
* one request at a time, at least
  :data:`~harness.sources.public_jobs.policy.MIN_INTERVAL_SECONDS` apart;
* a 10 s timeout and a 2 MB body cap, enforced while reading, not after;
* redirects are **not followed at all**: a 3xx is reported as
  ``redirect_not_followed``. Following one would mean re-deciding robots,
  path allowlist and the provider gate for a destination the provider chose,
  and getting that wrong is how a reader ends up somewhere it was never
  allowed. The caller can decide to request the new URL explicitly, which
  runs the full gate again;
* no cookies, no session, no credentials, no anti-bot tooling, no headless
  browser. If a page is only readable by defeating a bot check, this package
  reports that and stops.

Failures are returned as ``{'reason': ..., 'detail': ...}`` dicts, never raised
across the seam, so the collector can report a useful status instead of
claiming a live success.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from harness.sources.public_jobs import policy as _policy
from harness.sources.public_jobs import robots as _robots

__all__ = [
    "FetchResult",
    "BoundedFetcher",
    "REASON_BLOCKED_BY_PROVIDER",
]

#: A provider actively refused an unauthenticated reader (403/429, or a bot
#: interstitial). Reported honestly; never worked around.
REASON_BLOCKED_BY_PROVIDER = "blocked_by_provider"

#: Both providers answer HTTP **200** with a short failure page rather than a
#: 4xx/5xx in at least some conditions (알바천국 serves a 3-4 KB
#: "요청하신 페이지에 일시적인 장애가 발생하였습니다" page this way). Treating
#: that as a successful read is how a collector ends up reporting a live pass
#: over nothing, so these markers turn a 200 into a failure.
_ERROR_PAGE_MARKERS = (
    "일시적인 장애가 발생",
    "error-guide__title",
    "error_msg.asp",
    "잠시 후 다시 이용해 주십시오",
    "접근이 제한되었습니다",
    "비정상적인 접근",
    "자동화된 접근",
)

#: A body smaller than this cannot be a posting page on either provider (the
#: live samples are 101 KB and 234 KB). Used only together with a marker.
_SUSPICIOUS_BODY_BYTES = 20_000


def _error_page_reason(body: bytes, content_type: str | None) -> str | None:
    """Detect an HTTP-200 block/error page. ``None`` when the body looks real."""
    if not body:
        return "empty body"
    if content_type and "html" not in content_type.lower() and "text" not in content_type.lower():
        return "unexpected content-type: %s" % (content_type,)
    head = body[:80_000].decode("utf-8", "replace")
    if "�" in head:
        head = head + body[:80_000].decode("cp949", "replace")
    for marker in _ERROR_PAGE_MARKERS:
        if marker in head:
            return "provider returned an error/block page (%s)" % (marker,)
    if len(body) < _SUSPICIOUS_BODY_BYTES and "JobPosting" not in head:
        return (
            "body is %d bytes with no JobPosting block; too small to be a "
            "posting page" % (len(body),)
        )
    return None


class FetchResult(dict):
    """A plain dict: ``{ok, url, final_url, status, body, reason, detail}``."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


def _ok(url: str, final_url: str, status: int, body: bytes, robots_group: str) -> FetchResult:
    return FetchResult(
        ok=True,
        url=url,
        final_url=final_url,
        status=status,
        body=body,
        robots_group=robots_group,
        reason=None,
        detail=None,
    )


def _err(url: str, reason: str, detail: str, status: int | None = None) -> FetchResult:
    return FetchResult(
        ok=False,
        url=url,
        final_url=None,
        status=status,
        body=b"",
        robots_group=None,
        reason=reason,
        detail=detail,
    )


class _RedirectRefused(urllib.error.URLError):
    """Raised instead of following a redirect."""

    def __init__(self, code, target: str) -> None:
        super().__init__("HTTP %s redirect to %r was not followed" % (code, target))
        self.target = target
        self.code = code


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect.

    A same-provider redirect is not automatically safe: it can land on a
    robots-disallowed path (알바천국 answers some job URLs with a redirect to
    ``/error/error_msg.asp``), and a cross-provider hop would carry one
    provider's authorization onto another. Rather than re-run the whole gate
    mid-flight, this version refuses and says so.
    """

    max_repeats = 0
    max_redirections = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise _RedirectRefused(code, newurl)


def _url_problem(url: str) -> tuple[str, str] | None:
    """``None`` when ``url`` is fetchable under the allowlist, else the reason.

    Every rejection is a returned tuple, never an exception: a malformed URL is
    a controlled result like any other refusal.
    """
    if not isinstance(url, str) or not url.strip():
        return ("malformed_url", "url must be a non-empty string")
    try:
        split = urlsplit(url)
        _ = split.port  # raises ValueError on a malformed port
        hostname = split.hostname
        userinfo = (split.username, split.password)
    except ValueError as exc:
        return ("malformed_url", "urlsplit rejected %r: %s" % (url, exc))
    if any(userinfo):
        return (
            "userinfo_not_allowed",
            "credentials in the URL are not accepted; this reader is "
            "unauthenticated by design",
        )
    if split.scheme != _policy.ALLOWED_SCHEME:
        return ("scheme_not_allowed", "only https is allowed, got: %r" % (split.scheme,))
    if split.port not in _policy.ALLOWED_PORTS:
        return ("port_not_allowed", "only port 443 is allowed, got: %r" % (split.port,))
    provider = _policy.provider_for_host((hostname or "").rstrip("."))
    if provider is None:
        return (
            "host_not_allowlisted",
            "host %r is not one of %s" % (split.hostname, sorted(_policy.HOST_ALLOWLIST)),
        )
    path = split.path or "/"
    if not _policy.path_allowed(provider, path):
        return (
            "path_not_allowed",
            "path %r is outside the %s allowlist %s"
            % (path, provider["provider"], list(provider["allowed_path_prefixes"])),
        )
    return None


def _robots_body_problem(raw: bytes, content_type: str | None) -> str | None:
    """``None`` when ``raw`` really is a ``robots.txt``, else why it is not.

    A provider that answers the robots request with an HTML error page, a
    login wall or an empty body has told us nothing about what is permitted.
    Handing that to the parser would yield a group-less policy that defaults
    to *allow*, which is exactly backwards - so each of these closes the door.
    """
    if len(raw) > _policy.MAX_RESPONSE_BYTES:
        return "body exceeds cap"
    if not raw.strip():
        return "empty body"
    if content_type and "html" in content_type.lower():
        return "content-type %r is not a robots.txt" % (content_type,)
    if content_type and not content_type.lower().startswith("text/"):
        return "content-type %r is not text" % (content_type,)
    text = raw.decode("utf-8", "replace")
    lowered = text.lower()
    if "<html" in lowered or "<!doctype" in lowered or "<body" in lowered:
        return "body is HTML, not a robots.txt"
    for marker in _ERROR_PAGE_MARKERS:
        if marker in text:
            return "provider returned an error/block page (%s)" % (marker,)
    if "user-agent:" not in lowered:
        return "body contains no User-agent group"
    return None


def _final_url_problem(requested: str, final_url: str) -> tuple[str, str] | None:
    """Defence in depth: the body must have come from where we asked.

    Redirects are already refused, so a differing final URL means something
    followed one anyway (a custom opener, a future handler). Anything but the
    same host, and a still-allowlisted path, is refused.
    """
    if not final_url or final_url == requested:
        return None
    problem = _url_problem(final_url)
    if problem is not None:
        return ("redirect_off_allowlist", problem[1])
    if (urlsplit(final_url).hostname or "").lower() != (
        urlsplit(requested).hostname or ""
    ).lower():
        return (
            "redirect_off_allowlist",
            "response came from %r, not the requested host" % (final_url,),
        )
    # Same host, still-allowlisted path: the robots verdict for that path is
    # the more specific answer, so BoundedFetcher.fetch reports it first and
    # only then falls back to the blanket redirect refusal.
    return None


class BoundedFetcher:
    """Sequential, rate-limited, robots-respecting reader.

    ``opener``/``sleep``/``clock`` are injectable so the offline tests can
    exercise every branch without a socket. The defaults are the real ones.
    """

    def __init__(self, *, opener=None, sleep=None, clock=None, user_agent: str | None = None) -> None:
        self._opener = opener or urllib.request.build_opener(_RedirectGuard())
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self.user_agent = user_agent or _policy.USER_AGENT
        self._last_request_at: float | None = None
        self._robots_cache: dict = {}
        #: Number of bodies actually read off the network.
        self.request_count = 0

    # -- rate limit ------------------------------------------------------
    def _wait_turn(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = self._clock() - self._last_request_at
        remaining = _policy.MIN_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            self._sleep(remaining)

    # -- raw read --------------------------------------------------------
    def _read(self, url: str) -> FetchResult:
        self._wait_turn()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ko,en;q=0.5",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=_policy.TIMEOUT_SECONDS) as response:
                body = response.read(_policy.MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", None) or response.getcode()
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
        except _RedirectRefused as exc:
            return _err(url, "redirect_not_followed", str(exc))
        except urllib.error.HTTPError as exc:
            reason = (
                REASON_BLOCKED_BY_PROVIDER
                if exc.code in (401, 403, 429)
                else "http_error"
            )
            return _err(url, reason, "HTTP %s %s" % (exc.code, exc.reason), status=exc.code)
        except urllib.error.URLError as exc:
            return _err(url, "network_error", repr(exc.reason))
        except TimeoutError as exc:  # socket timeout surfaces here on 3.10+
            return _err(url, "timeout", repr(exc))
        except OSError as exc:
            return _err(url, "network_error", repr(exc))
        finally:
            self._last_request_at = self._clock()

        self.request_count += 1
        if len(body) > _policy.MAX_RESPONSE_BYTES:
            return _err(
                url,
                "response_too_large",
                "body exceeds %d bytes" % (_policy.MAX_RESPONSE_BYTES,),
                status=status,
            )
        final_problem = _final_url_problem(url, final_url)
        if final_problem is not None:
            return _err(url, final_problem[0], final_problem[1], status=status)
        error_page = _error_page_reason(body, content_type)
        if error_page is not None:
            return _err(url, "blocked_or_error_page", error_page, status=status)
        result = _ok(url, final_url, status, body, robots_group="")
        result["content_type"] = content_type
        return result

    # -- robots ----------------------------------------------------------
    def robots_for(self, policy: dict) -> _robots.RobotsPolicy:
        """Fetch and cache the provider's ``robots.txt``. Fails closed."""
        host = policy["host"]
        cached = self._robots_cache.get(host)
        if cached is not None:
            return cached
        robots_url = policy["robots_url"]
        self._wait_turn()
        request = urllib.request.Request(
            robots_url,
            headers={"User-Agent": self.user_agent, "Accept": "text/plain"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=_policy.TIMEOUT_SECONDS) as response:
                raw = response.read(_policy.MAX_RESPONSE_BYTES + 1)
                robots_content_type = response.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001 - any failure closes the door
            result = _robots.closed_policy(
                user_agent=self.user_agent,
                source_url=robots_url,
                reason="robots_unavailable: %r" % (exc,),
            )
            self._robots_cache[host] = result
            return result
        finally:
            self._last_request_at = self._clock()
        invalid = _robots_body_problem(raw, robots_content_type)
        if invalid is not None:
            result = _robots.closed_policy(
                user_agent=self.user_agent,
                source_url=robots_url,
                reason="robots_unavailable: " + invalid,
            )
        else:
            try:
                result = _robots.parse_robots_txt(
                    raw.decode("utf-8", "replace"),
                    user_agent=self.user_agent,
                    source_url=robots_url,
                )
            except Exception as exc:  # noqa: BLE001
                result = _robots.closed_policy(
                    user_agent=self.user_agent,
                    source_url=robots_url,
                    reason="robots_unparseable: %r" % (exc,),
                )
        self._robots_cache[host] = result
        return result

    # -- the seam the collector uses -------------------------------------
    def fetch(self, url: str) -> FetchResult:
        """Read one allowlisted, robots-permitted URL, or explain why not."""
        problem = _url_problem(url)
        if problem is not None:
            return _err(url, problem[0], problem[1])
        split = urlsplit(url)
        provider = _policy.provider_for_host(split.hostname or "")
        robots = self.robots_for(provider)
        if robots.is_closed:
            return _err(url, "robots_unavailable", robots.reason or "robots closed")
        target = split.path or "/"
        if split.query:
            target = target + "?" + split.query
        if not robots.allows(target):
            return _err(
                url,
                "robots_disallowed",
                "robots.txt group %r disallows %r" % (robots.group, target),
            )
        result = self._read(url)
        if not result.get("ok"):
            return result
        final_url = result.get("final_url") or url
        final_split = urlsplit(final_url)
        final_target = final_split.path or "/"
        if final_split.query:
            final_target = final_target + "?" + final_split.query
        if not robots.allows(final_target):
            return _err(
                url,
                "robots_disallowed",
                "response url %r is disallowed by robots.txt group %r"
                % (final_target, robots.group),
            )
        if final_url != url:
            return _err(
                url,
                "redirect_not_followed",
                "response came from %r; request it explicitly to re-run the "
                "full gate" % (final_url,),
            )
        result["robots_group"] = robots.group
        return result
