"""A ``robots.txt`` reader with longest-match precedence, failing closed.

Why not :mod:`urllib.robotparser`: it returns the verdict of the **first**
matching rule in a group. Both providers in scope publish a group shaped like::

    User-agent: *
    Disallow: /
    Allow: /job/

Under first-match that reads as "nothing is allowed", which would make the
whole feature look forbidden when it is not. The documented behaviour of the
real crawlers, and of the REP specification, is **longest match wins**, with an
``Allow`` winning a tie. That is what :func:`RobotsPolicy.allows` implements.

Group selection is also specification-shaped: the most specific matching
user-agent token wins, the ``*`` group is used only when no named group
matches, and **every** group naming that token is merged (a file may repeat
``User-agent: *`` and the rules are cumulative). This matters because both
providers ship *separate, stricter* groups for named crawlers, so a reader must
match its own token honestly rather than fall into a group written for someone
else.

Supported syntax is deliberately bounded to what the two providers in scope
publish: ``User-agent``, ``Allow``, ``Disallow``, the ``*`` wildcard and the
``$`` end anchor, with ``#`` comments. ``Crawl-delay`` is ignored (this package
already waits a fixed second between requests, which is stricter than any
delay either provider publishes), and ``Sitemap`` and other non-group fields
are ignored. Anything unrecognised is skipped, never guessed at.

Failure is closed. An unreachable, oversized or unparseable ``robots.txt``
yields a policy that allows nothing, with the reason recorded.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from harness.sources.public_jobs import policy as _policy

__all__ = ["RobotsPolicy", "parse_robots_txt", "closed_policy"]


def _normalize_path(path: str) -> str:
    """Path plus query, as the rules are written against."""
    return path if path.startswith("/") else "/" + path


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """Compile one ``Allow``/``Disallow`` value into an anchored prefix regex.

    ``*`` matches any run of characters and a trailing ``$`` anchors the end of
    the path; everything else is literal.
    """
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    parts = [re.escape(chunk) for chunk in body.split("*")]
    regex = ".*".join(parts)
    return re.compile("^" + regex + ("$" if anchored_end else ""))


class _Rule:
    __slots__ = ("allow", "pattern", "regex", "specificity")

    def __init__(self, allow: bool, pattern: str) -> None:
        self.allow = allow
        self.pattern = pattern
        self.regex = _pattern_to_regex(pattern)
        # Length of the rule text, minus the metacharacters, is the REP
        # tie-breaker: the rule that says the most about the path wins.
        self.specificity = len(pattern.replace("*", "").replace("$", ""))

    def matches(self, path: str) -> bool:
        return self.regex.match(path) is not None


class RobotsPolicy:
    """The rules of one ``robots.txt``, as they apply to one user-agent."""

    def __init__(
        self,
        rules: list,
        *,
        user_agent: str,
        group: str,
        source_url: str | None = None,
        default_allow: bool = True,
        reason: str | None = None,
    ) -> None:
        self.rules = rules
        self.user_agent = user_agent
        #: Which ``User-agent:`` group we matched. Recorded so a reader can
        #: prove it did not borrow a group written for another crawler.
        self.group = group
        self.source_url = source_url
        #: What to answer when no rule matches at all.
        self.default_allow = default_allow
        #: Why this policy is closed, when it is.
        self.reason = reason

    @property
    def is_closed(self) -> bool:
        return self.reason is not None

    def allows(self, url_or_path: str) -> bool:
        """Longest-match verdict for ``url_or_path``. Allow wins a tie."""
        if self.reason is not None:
            return False
        split = urlsplit(url_or_path)
        path = _normalize_path(unquote(split.path) or "/")
        if split.query:
            path = path + "?" + split.query
        best: _Rule | None = None
        for rule in self.rules:
            if not rule.matches(path):
                continue
            if best is None or rule.specificity > best.specificity:
                best = rule
            elif rule.specificity == best.specificity and rule.allow:
                best = rule
        if best is None:
            return self.default_allow
        return best.allow

    def explain(self, url_or_path: str) -> dict:
        """The verdict plus the rule that produced it, for error payloads."""
        allowed = self.allows(url_or_path)
        return {
            "allowed": allowed,
            "group": self.group,
            "robots_url": self.source_url,
            "reason": self.reason,
        }


def closed_policy(*, user_agent: str, source_url: str | None, reason: str) -> RobotsPolicy:
    """A policy that allows nothing, carrying the reason it is closed."""
    return RobotsPolicy(
        [],
        user_agent=user_agent,
        group="none",
        source_url=source_url,
        default_allow=False,
        reason=reason,
    )


def _agent_tokens(line: str) -> str:
    return line.strip().lower()


def parse_robots_txt(
    text: str,
    *,
    user_agent: str | None = None,
    source_url: str | None = None,
) -> RobotsPolicy:
    """Parse ``robots.txt`` text into a :class:`RobotsPolicy`.

    The user-agent is matched case-insensitively on substring, exactly as
    crawlers do: a group named ``Yeti`` matches a product token containing
    ``yeti``. Our own token matches nothing but ``*`` on both providers, which
    is the point - see :data:`harness.sources.public_jobs.policy.USER_AGENT`.
    """
    agent = (user_agent or _policy.USER_AGENT).lower()

    # group agents -> rules, preserving the file's order
    groups: list = []  # list of (agents:set, rules:list)
    current_agents: set = set()
    starting_group = False

    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if not starting_group:
                current_agents = set()
                starting_group = True
            current_agents.add(_agent_tokens(value))
            continue
        if field in ("allow", "disallow"):
            if starting_group:
                groups.append((set(current_agents), []))
                starting_group = False
            if not groups:
                # rules before any User-agent line: treat as the wildcard group
                groups.append(({"*"}, []))
            if value == "" and field == "disallow":
                # `Disallow:` with an empty value means "allow everything"
                continue
            groups[-1][1].append(_Rule(field == "allow", value))
            continue
        # Sitemap and other non-group fields are ignored on purpose.

    # most specific matching named token wins; `*` only if nothing named does
    best_token = None
    best_token_len = -1
    has_wildcard = False
    for agents, _rules in groups:
        for token in agents:
            if token == "*":
                has_wildcard = True
                continue
            if token and token in agent and len(token) > best_token_len:
                best_token = token
                best_token_len = len(token)

    chosen_token = best_token or ("*" if has_wildcard else None)
    if chosen_token is None:
        # A parseable file with no applicable group means no restriction was
        # published for us; the providers in scope always publish one, so this
        # is only reachable for an empty robots.txt.
        return RobotsPolicy(
            [],
            user_agent=agent,
            group="none",
            source_url=source_url,
            default_allow=True,
        )
    # a file may declare the same user-agent more than once; merge them all
    merged: list = []
    for agents, rules in groups:
        if chosen_token in agents:
            merged.extend(rules)
    return RobotsPolicy(
        merged,
        user_agent=agent,
        group=chosen_token,
        source_url=source_url,
        default_allow=True,
    )
