"""Optional semantic ordering of weekly plans, on top of a finished answer.

This layer runs **after** the deterministic pipeline has already produced its
three feasible plans. It is a presentation step and nothing more:

* it never filters — hard feasibility stays in the Daytona run;
* it never edits a plan's data — ids, types, labels, hashes, jobs, assigned
  shifts, travel, metrics and warnings come through byte-identical;
* it may change only the **order** of ``response['plans']`` and each plan's
  ``reason``, and it may add ``fitScore``.

The model is never trusted to write prose. This module builds an *evidence
bank* — sentences derived only from the response and the request that produced
it — and the model may answer with nothing but plan ids, a score and evidence
ids. A reason is then rendered from the sentences those ids name, so no
sentence a reader sees was authored by the model. Any unverifiable element (an
unknown plan id, a missing plan, an evidence id from another plan, a score that
is not a finite 0..1 number, an overlong token) rejects the *whole* answer: the
response falls back unchanged, because a partly-trusted answer would ship one
unchecked claim beside checked ones.

Configuration is Nosana's OpenAI-compatible ``/chat/completions``
(https://learn.nosana.com/api/llm.html), read from ``WEEKLY_LLM_PROVIDER``,
``WEEKLY_LLM_BASE_URL``, ``WEEKLY_LLM_MODEL`` and the existing
``NOSANA_API_KEY``. Weekly-specific names on purpose: the daily path keeps its
own ``NOSANA_BASE_URL``/``NOSANA_MODEL``, which point at a different node. One
request per answer, to a model named in configuration — there is no discovery
round trip and no second provider. Without a key nothing here touches the
network and the deterministic order is returned as-is.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from .constants import DAY_INDEX

__all__ = [
    "SEMANTIC_BUDGET_SECONDS",
    "SemanticConfig",
    "SemanticError",
    "build_evidence_bank",
    "enrich_weekly_response",
    "load_provider_config",
    "validate_selection",
]

#: The whole semantic attempt happens inside this wall clock.
SEMANTIC_BUDGET_SECONDS = 8.0
#: Below this there is no point starting; the caller's deadline owns the answer.
MIN_BUDGET_SECONDS = 1.0
#: A request needs at least this much of the budget left to be worth sending.
MIN_REQUEST_SECONDS = 0.5

DEFAULT_PROVIDER = "nosana"
SUPPORTED_PROVIDERS = ("nosana",)
DEFAULT_BASE_URL = "https://inference.nosana.com/v1"
#: Named outright rather than discovered: a ``/models`` round trip would spend
#: budget to learn something the operator already knows.
DEFAULT_MODEL = "qwen/qwen3.8-27b"

MAX_RESPONSE_BYTES = 128 * 1024
READ_CHUNK_BYTES = 16 * 1024
#: Floor for a single socket read, so shrinking the timeout can never reach 0.
MIN_SOCKET_TIMEOUT = 0.05
MAX_OUTPUT_TOKENS = 512

#: Deliberately small, and measured rather than guessed: 12 items per plan is
#: what the configured model answers inside the budget (~1.7k prompt tokens,
#: ~4.6s round trip). A longer evidence list buys nothing — what decides an
#: order is the metric trade-offs, the categories and the warnings, not a
#: restatement of every field of every job.
MAX_EVIDENCE_PER_PLAN = 12
MAX_WARNING_EVIDENCE = 4
MAX_EVIDENCE_CHARS = 180
#: Most decision-relevant first; the rest of a long warning list is dropped.
WARNING_PRIORITY = (
    "BELOW_TARGET",
    "LONG_TRAVEL",
    "TIGHT_TRANSFER",
    "SHORT_PERIOD",
    "NON_HOURLY_PAY",
    "HOLIDAY_PAY_NOT_INCLUDED",
    "QUALIFICATIONS_UNVERIFIED",
)
MIN_REASON_EVIDENCE = 1
MAX_REASON_EVIDENCE = 3
#: An id the model echoes back may not be longer than this.
MAX_TOKEN_CHARS = 64
#: Nothing legitimate sends back more entries than there are plans; cap anyway.
MAX_ENTRIES = 16

#: The only ``meta.llmStatus`` values a fallback may report. Never a raw
#: provider message — a status code says what happened without quoting a third
#: party's error text into our contract.
FALLBACK_STATUSES = (
    "not_configured",
    "timeout",
    "provider_error",
    "invalid_response",
    "budget_exhausted",
)


class SemanticError(RuntimeError):
    """A reason we did not get a usable, verified ordering.

    ``status`` is one of :data:`FALLBACK_STATUSES` and is the only thing that
    ever reaches the response; the message stays in this process.
    """

    def __init__(self, status: str, detail: str = ""):
        super().__init__(detail or status)
        self.status = status if status in FALLBACK_STATUSES else "provider_error"


@dataclass(frozen=True)
class SemanticConfig:
    provider: str
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL

    def __repr__(self) -> str:  # never let a key reach a log or a receipt
        return (
            f"SemanticConfig(provider={self.provider!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, api_key='***')"
        )


def load_provider_config(env: dict | None = None) -> SemanticConfig | None:
    """Read configuration, or ``None`` when inference must not be attempted.

    ``WEEKLY_LLM_PROVIDER`` defaults to ``nosana``; any other value (``off``,
    ``none``, a provider we do not support) disables the layer outright. The key
    is the daily path's ``NOSANA_API_KEY``; the endpoint and model are
    weekly-specific so the daily path's own values keep working unchanged.
    """
    source = os.environ if env is None else env
    provider = (source.get("WEEKLY_LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        return None
    key = (source.get("NOSANA_API_KEY") or "").strip()
    if not key:
        return None
    base = (source.get("WEEKLY_LLM_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    model = (source.get("WEEKLY_LLM_MODEL") or "").strip() or DEFAULT_MODEL
    return SemanticConfig(provider, key, base.rstrip("/"), model)


# --------------------------------------------------------------------------
# evidence — every sentence the reader can end up seeing


def build_evidence_bank(
    payload: dict[str, Any], response: dict[str, Any]
) -> dict[str, list[dict[str, str]]]:
    """``plan id -> [{id, text}]``, derived only from ``response`` and ``payload``.

    Nothing here consults the job dataset, the network or the clock. Every
    sentence restates a value that is already in the answer being sent, so a
    rendered reason cannot outrun what the response itself discloses.
    """
    profile = payload.get("profile") if isinstance(payload, dict) else None
    target = _num((profile or {}).get("targetAmount"))
    bank: dict[str, list[dict[str, str]]] = {}
    for index, plan in enumerate(response.get("plans") or [], start=1):
        if not isinstance(plan, dict) or not isinstance(plan.get("id"), str):
            continue
        prefix = f"p{index}e"
        bank[plan["id"]] = [
            {"id": f"{prefix}{number:02d}", "text": text}
            for number, text in enumerate(
                _plan_sentences(plan, target)[:MAX_EVIDENCE_PER_PLAN], start=1
            )
        ]
    return bank


def _plan_sentences(plan: dict[str, Any], target: float | None) -> list[str]:
    """One plan's evidence, ordered most decision-relevant first.

    The list is short on purpose. The trade-offs come first (income against
    hours against travel), then one compact line per job, then the warnings
    that would change a reader's mind — not every field the plan carries.
    """
    out: list[str] = []
    reason = plan.get("reason")
    if isinstance(reason, str) and reason.strip():
        out.append(reason.strip())

    metrics = plan.get("metrics") if isinstance(plan.get("metrics"), dict) else {}
    jobs = [job for job in (plan.get("jobs") or []) if isinstance(job, dict)]

    income = _num(metrics.get("monthlyIncome"))
    if income is not None:
        out.append(f"월 예상 수입은 {_won(income)}입니다.")
    rate = _num(metrics.get("targetAchievementRate"))
    if rate is not None and target:
        out.append(f"목표 {_won(target)}의 {round(rate * 100)}% 수준입니다.")
    hours = _num(metrics.get("weeklyWorkHours"))
    if hours is not None:
        out.append(f"주 근무 시간은 {_plain(hours)}시간입니다.")
    travel = _num(metrics.get("weeklyTravelMinutes"))
    if travel is not None:
        out.append(f"주간 이동 시간은 {_plain(travel)}분입니다.")
    effective = _num(metrics.get("effectiveHourlyWage"))
    if effective is not None:
        out.append(f"이동 시간까지 포함한 실질 시급은 {_won(effective)}입니다.")

    days = sorted(
        {
            shift.get("day")
            for job in jobs
            for shift in (job.get("assignedShifts") or [])
            if isinstance(shift, dict) and shift.get("day") in DAY_INDEX
        },
        key=lambda day: DAY_INDEX[day],
    )
    if days:
        out.append(f"주 {len(days)}일({', '.join(days)}) 근무합니다.")

    out.extend(_job_sentence(job) for job in jobs)
    out.extend(_warning_sentences(plan))
    return [_clip(text) for text in out]


def _job_sentence(job: dict[str, Any]) -> str:
    """One line per job: what it is, what it pays, how much of the week it takes."""
    parts = [
        part
        for part in (job.get("category"), job.get("location"))
        if isinstance(part, str) and part
    ]
    wage = _num(job.get("hourlyWage"))
    if wage is not None:
        parts.append(f"시급 {_won(wage)}")
    job_hours = _num(job.get("weeklyHours"))
    if job_hours is not None:
        parts.append(f"주 {_plain(job_hours)}시간")
    job_pay = _num(job.get("weeklyPay"))
    if job_pay is not None:
        parts.append(f"주급 {_won(job_pay)}")
    rating, reviews = _num(job.get("rating")), _num(job.get("reviewCount"))
    if rating is not None and reviews is not None:
        parts.append(f"평점 {_plain(rating)}({_plain(reviews)}건)")
    negotiable = [
        word
        for word, flag in (
            ("시간", job.get("timeNegotiable")),
            ("요일", job.get("daysNegotiable")),
        )
        if flag is True
    ]
    if negotiable:
        parts.append("·".join(negotiable) + " 협의 가능")
    if not parts:
        return f"{_title(job)} 공고가 포함되어 있습니다."
    return f"{_title(job)}: " + ", ".join(parts) + "."


def _warning_sentences(plan: dict[str, Any]) -> list[str]:
    """The warnings a reader would weigh, one per code, most relevant first."""
    seen: dict[str, str] = {}
    for warning in plan.get("warnings") or []:
        if not isinstance(warning, dict) or not isinstance(warning.get("message"), str):
            continue
        code = warning.get("code")
        message = warning["message"].strip()
        if not isinstance(code, str) or not message or code in seen:
            continue
        seen[code] = message
    def rank(item: tuple[str, str]) -> int:
        code = item[0]
        return WARNING_PRIORITY.index(code) if code in WARNING_PRIORITY else len(WARNING_PRIORITY)

    ranked = sorted(seen.items(), key=rank)
    return [f"주의: {message}" for _, message in ranked[:MAX_WARNING_EVIDENCE]]


def _title(job: dict[str, Any]) -> str:
    for key in ("title", "company", "jobId"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "이 공고"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def _plain(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(round(value, 2))


def _won(value: float) -> str:
    return f"{int(round(value)):,}원"


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_EVIDENCE_CHARS else text[: MAX_EVIDENCE_CHARS - 1] + "…"


# --------------------------------------------------------------------------
# the one bounded request


def _context(payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """What the user asked for, restated from the request only."""
    profile = payload.get("profile") if isinstance(payload, dict) else None
    search = payload.get("search") if isinstance(payload, dict) else None
    profile = profile if isinstance(profile, dict) else {}
    search = search if isinstance(search, dict) else {}
    raw = profile.get("constraints")
    constraints = raw if isinstance(raw, dict) else {}
    return {
        "role": profile.get("role"),
        "home": profile.get("home"),
        "targetAmount": profile.get("targetAmount"),
        "priority": search.get("priority"),
        "jobCount": search.get("jobCount"),
        "categories": search.get("categories"),
        "minBlockHours": constraints.get("minBlockHours"),
        "allowNight": constraints.get("allowNight"),
        "wantWeeklyHolidayPay": constraints.get("wantWeeklyHolidayPay"),
        "age": constraints.get("age"),
        "candidateCount": response.get("candidateCount"),
    }


SYSTEM_PROMPT = (
    "You order weekly part-time plans for one user. Every plan below is already "
    "feasible: a deterministic planner built and checked it. You choose only the "
    "ORDER and which supplied evidence sentences explain each plan.\n"
    'Reply with JSON only: {"plans":[{"planId":"<id>","fitScore":<0..1>,'
    '"reasonEvidenceIds":["<evidence id>", ...]}]}\n'
    "Rules: include every planId exactly once; fitScore is a number between 0 and 1 "
    "where higher means a better fit for this user's stated conditions; pick "
    f"{MIN_REASON_EVIDENCE} to {MAX_REASON_EVIDENCE} evidence ids per plan, and only "
    "from that same plan's own evidence list. Never write prose, never invent a fact, "
    "number, job, skill or qualification, never name an id that is not listed."
)


def _messages(
    payload: dict[str, Any],
    response: dict[str, Any],
    bank: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:
    plans = [
        {
            "planId": plan["id"],
            "type": plan.get("type"),
            "label": plan.get("label"),
            "evidence": bank[plan["id"]],
        }
        for plan in response.get("plans") or []
        if isinstance(plan, dict) and plan.get("id") in bank
    ]
    body = {"user": _context(payload, response), "plans": plans}
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
    ]


def _request_json(
    url: str,
    config: SemanticConfig,
    deadline: float,
    payload: dict | None,
    clock: Callable[[], float],
) -> dict:
    remaining = deadline - clock()
    if remaining < MIN_REQUEST_SECONDS:
        raise SemanticError("budget_exhausted", "no time left for a request")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        # One bounded attempt, no retry. The socket timeout bounds each read;
        # the deadline check inside _read_bounded bounds a body that is
        # trickled a few bytes at a time and would otherwise outlive us.
        with urllib.request.urlopen(request, timeout=remaining) as stream:
            body = _read_bounded(stream, deadline, clock)
    except SemanticError:
        raise
    except urllib.error.HTTPError:
        raise SemanticError("provider_error", "provider returned an error status") from None
    except TimeoutError:
        raise SemanticError("timeout", "request timed out") from None
    except urllib.error.URLError as exc:
        status = "timeout" if isinstance(exc.reason, TimeoutError) else "provider_error"
        raise SemanticError(status, "request failed") from None
    except Exception as exc:
        raise SemanticError("provider_error", f"request failed: {type(exc).__name__}") from None
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise SemanticError("provider_error", "response was not valid JSON") from None
    if not isinstance(parsed, dict):
        raise SemanticError("provider_error", "response was not a JSON object")
    return parsed


def _read_bounded(stream, deadline: float, clock: Callable[[], float]) -> bytes:
    """Read a body under a wall clock, not merely under a per-read timeout.

    ``read()`` would keep asking the socket until it had the whole chunk, so a
    body trickled a few bytes at a time could outlive the deadline between two
    checks. ``read1()`` returns after one underlying read, and the socket's own
    timeout is shrunk to the time actually left before each of them — so a
    stalled read cannot outlive the deadline either.
    """
    read_once = getattr(stream, "read1", None) or stream.read
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise SemanticError("timeout", "response body outlived the budget")
        _shrink_timeout(stream, remaining)
        try:
            chunk = read_once(READ_CHUNK_BYTES)
        except TimeoutError:
            raise SemanticError("timeout", "response body stalled") from None
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise SemanticError("provider_error", "response exceeded the size limit")
        chunks.append(chunk)


def _shrink_timeout(stream, seconds: float) -> None:
    """Best effort: no single socket read may outlive what is left of the budget.

    Reaching for the response's socket is the only way to retighten a timeout
    that :func:`urllib.request.urlopen` fixed when it opened the connection. If
    the object does not expose one, the deadline check in the read loop still
    bounds the body, just one read less tightly.
    """
    try:
        stream.fp.raw._sock.settimeout(max(MIN_SOCKET_TIMEOUT, seconds))
    except Exception:
        pass


def _http_invoke(
    config: SemanticConfig,
    messages: list[dict[str, str]],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, str]:
    """One chat completion. Returns ``(content, model)``; raises SemanticError."""
    model = config.model
    body = _request_json(
        f"{config.base_url}/chat/completions",
        config,
        deadline,
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            # This model reasons aloud by default, which spends the whole
            # budget before any answer is emitted. Off, it returns the same
            # verified ordering in roughly half the time.
            "chat_template_kwargs": {"enable_thinking": False},
        },
        clock,
    )
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise SemanticError("provider_error", "response had no choices")
    message = choices[0].get("message")
    # Only ``content``. Some models return a separate reasoning field beside it;
    # that is the model thinking aloud, not its answer.
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise SemanticError("invalid_response", "response had no message content")
    return content, model


# --------------------------------------------------------------------------
# validation — all of it, or none of it


def _parse_selection(content: str) -> list:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise SemanticError("invalid_response", "no JSON object in the model reply")
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        raise SemanticError("invalid_response", "model reply was not valid JSON") from None
    entries = parsed.get("plans") if isinstance(parsed, dict) else None
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise SemanticError("invalid_response", "model reply had no usable plans array")
    return entries


def validate_selection(
    entries: list, bank: dict[str, list[dict[str, str]]]
) -> dict[str, tuple[float, list[str]]]:
    """``plan id -> (score, rendered reason sentences)``, or raise.

    Every claim is checked against ``bank``: the plan id set must match exactly,
    each score must be a finite number in ``0..1``, and each evidence id must
    belong to *that* plan. One failure rejects the whole answer.
    """
    verified: dict[str, tuple[float, list[str]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SemanticError("invalid_response", "entry was not an object")
        plan_id = entry.get("planId")
        if not isinstance(plan_id, str) or len(plan_id) > MAX_TOKEN_CHARS:
            raise SemanticError("invalid_response", "planId was not a bounded string")
        if plan_id not in bank:
            raise SemanticError("invalid_response", "entry named an unknown plan id")
        if plan_id in verified:
            raise SemanticError("invalid_response", "entry repeated a plan id")

        score = entry.get("fitScore")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise SemanticError("invalid_response", "fitScore was not a number")
        score = float(score)
        if score != score or score in (float("inf"), float("-inf")):
            raise SemanticError("invalid_response", "fitScore was not finite")
        if not 0.0 <= score <= 1.0:
            raise SemanticError("invalid_response", "fitScore was outside 0..1")

        ids = entry.get("reasonEvidenceIds")
        if not isinstance(ids, list) or not (
            MIN_REASON_EVIDENCE <= len(ids) <= MAX_REASON_EVIDENCE
        ):
            raise SemanticError("invalid_response", "reasonEvidenceIds was not a bounded list")
        by_id = {item["id"]: item["text"] for item in bank[plan_id]}
        sentences: list[str] = []
        for raw in ids:
            if not isinstance(raw, str) or not raw or len(raw) > MAX_TOKEN_CHARS:
                raise SemanticError("invalid_response", "evidence id was not a bounded string")
            token = raw.strip()
            if token not in by_id:
                # This also catches an id that exists but belongs to another
                # plan: the bank is per plan, so the lookup *is* the check.
                raise SemanticError("invalid_response", "evidence id is not this plan's")
            text = by_id[token]
            if text not in sentences:
                sentences.append(text)
        verified[plan_id] = (round(score, 3), sentences)

    if set(verified) != set(bank):
        raise SemanticError("invalid_response", "the plan id set was not complete")
    return verified


# --------------------------------------------------------------------------
# the public seam


def enrich_weekly_response(
    payload: dict[str, Any],
    response: dict[str, Any],
    *,
    config: SemanticConfig | None = None,
    invoke: Callable[..., tuple[str, str]] | None = None,
    budget_seconds: float = SEMANTIC_BUDGET_SECONDS,
    env: dict | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Return a copy of ``response``, semantically ordered when that is possible.

    Never raises and never mutates ``response``. On success the copy carries the
    plans in the model's order, a reason rendered from verified evidence, a
    ``fitScore`` per plan, and ``source='llm'`` with ``meta.engine='hybrid'``.
    On any failure the copy is the deterministic answer unchanged apart from
    ``meta.llmStatus``, which says why — as a code, never a provider's words.

    ``config`` and ``invoke`` are the injection points: a test supplies both and
    no network call happens.
    """
    result = deepcopy(response)
    started = clock()
    if config is None:
        config = load_provider_config(env)
    if config is None:
        return _fallback(result, "not_configured", 0)
    if budget_seconds < MIN_BUDGET_SECONDS:
        return _fallback(result, "budget_exhausted", 0)

    deadline = started + budget_seconds
    try:
        bank = build_evidence_bank(payload, result)
        plans = [
            plan
            for plan in (result.get("plans") or [])
            if isinstance(plan, dict) and plan.get("id") in bank
        ]
        if not bank or len(plans) != len(result.get("plans") or []):
            # Our own answer is not renderable as evidence; treat that exactly
            # like an unusable model answer and change nothing.
            raise SemanticError("invalid_response", "response carried no usable plans")
        content, model = (invoke or _http_invoke)(
            config, _messages(payload, result, bank), deadline, clock
        )
        verified = validate_selection(_parse_selection(content), bank)
    except SemanticError as exc:
        return _fallback(result, exc.status, _elapsed_ms(started, clock))
    except Exception:
        # A defect in this layer must not cost the caller its answer.
        return _fallback(result, "provider_error", _elapsed_ms(started, clock))

    ordered = sorted(
        enumerate(plans), key=lambda pair: (-verified[pair[1]["id"]][0], pair[0])
    )
    for _, plan in ordered:
        score, sentences = verified[plan["id"]]
        plan["reason"] = " ".join(sentences)
        plan["fitScore"] = score
    result["plans"] = [plan for _, plan in ordered]
    result["source"] = "llm"
    meta = result.setdefault("meta", {})
    meta["engine"] = "hybrid"
    meta["llmUsed"] = True
    meta["llmProvider"] = config.provider
    meta["llmModel"] = model
    meta["llmLatencyMs"] = _elapsed_ms(started, clock)
    meta["llmStatus"] = "success"
    return result


def _fallback(result: dict[str, Any], status: str, latency_ms: int) -> dict[str, Any]:
    """The deterministic answer, said plainly to be the deterministic answer."""
    result["source"] = "fallback"
    meta = result.setdefault("meta", {})
    meta["engine"] = "deterministic"
    meta["llmUsed"] = False
    meta["llmLatencyMs"] = latency_ms
    meta["llmStatus"] = status if status in FALLBACK_STATUSES else "provider_error"
    meta.pop("llmProvider", None)
    meta.pop("llmModel", None)
    return result


def _elapsed_ms(started: float, clock: Callable[[], float]) -> int:
    return max(0, round((clock() - started) * 1000))
