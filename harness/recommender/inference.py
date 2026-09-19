"""Optional Nosana re-ranking with grounded selector validation.

Owned by harness_c. Optional in the strongest sense: with no ``NOSANA_API_KEY``
nothing here touches the network and the deterministic order is returned as-is.
Docs: https://learn.nosana.com/api/llm.html (OpenAI-compatible /chat/completions).

The model may influence only the ORDER and the choice of evidence. It can never
introduce a job id that is not in the candidate list, and it cannot write prose:
each reason must be ``field:<component>`` (renders the deterministic sentence for
that component verbatim) or ``quote:<text>`` (must appear verbatim in that job's
own posting text). One unverifiable element rejects the whole response -- partial
trust would ship an unchecked claim beside checked ones.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..matching.evidence import job_evidence, job_of
from ..matching.vocabulary import normalize

__all__ = ["NosanaConfig", "load_config", "rerank", "validate_rankings", "InferenceError"]

DEFAULT_BASE_URL = "https://inference.nosana.com/v1"
DISCOVERY_TIMEOUT_SECONDS = 5.0
INFERENCE_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 128 * 1024
MAX_CANDIDATES = 12
MIN_QUOTE_CHARS = 6
MAX_QUOTE_CHARS = 160
MIN_REASONS = 2
MAX_REASONS = 4


class InferenceError(RuntimeError):
    """Any reason we did not get a usable, verified ranking."""


@dataclass(frozen=True)
class NosanaConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str | None = None

    def __repr__(self) -> str:  # never let a key reach a log or a receipt
        return f"NosanaConfig(base_url={self.base_url!r}, model={self.model!r}, api_key='***')"


def load_config(env: dict | None = None) -> NosanaConfig | None:
    """Read configuration, or None when inference must not be attempted."""
    source = os.environ if env is None else env
    key = (source.get("NOSANA_API_KEY") or "").strip()
    if not key:
        return None
    base = (source.get("NOSANA_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    model = (source.get("NOSANA_MODEL") or "").strip() or None
    return NosanaConfig(key, base.rstrip("/"), model)


def _request_json(url: str, config: NosanaConfig, timeout: float, payload: dict | None = None) -> dict:
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
        # One bounded attempt, no retry.
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise InferenceError(f"HTTP {exc.code}") from None
    except Exception as exc:
        raise InferenceError(f"request failed: {type(exc).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise InferenceError("response exceeded the size limit")
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise InferenceError("response was not valid JSON") from None
    if not isinstance(parsed, dict):
        raise InferenceError("response was not a JSON object")
    return parsed


def _discover_model(config: NosanaConfig) -> str:
    payload = _request_json(f"{config.base_url}/models", config, DISCOVERY_TIMEOUT_SECONDS)
    entries = payload.get("data")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip():
                return entry["id"].strip()
    raise InferenceError("no model available from /models")


def _digest(candidate: dict, scored: dict) -> dict:
    job = job_of(candidate)
    return {
        "job_id": job.get("id"),
        "title": job.get("title"),
        "location": job.get("location"),
        "category": job.get("category"),
        "description": (job.get("description") or "")[:300],
        "hourly_pay": job.get("hourly_pay"),
        "required_skills": job.get("required_skills"),
        "preferred_skills": job.get("preferred_skills"),
        "daily_income": candidate.get("daily_income"),
        "home_arrival": candidate.get("home_arrival"),
        "deterministic_score": scored["score"],
        "available_field_reasons": scored["reason_by_component"],
    }


def _messages(ctx: dict, digests: list[dict]) -> list[dict]:
    system = (
        "You re-rank shortlisted same-day part-time shifts. Every candidate already "
        "fits the user's schedule; choose the order and the evidence only.\n"
        'Reply with JSON only: {"rankings":[{"job_id":"<id from the list>",'
        '"score":<0..1>,"reasons":["field:<name>" or "quote:<exact text>", ...]}]}\n'
        "Use only field names present in that job's available_field_reasons. A quote "
        "must be copied character for character from that job's own title, location, "
        "category, description or skills. Never invent a fact, never name an id that "
        "is not listed, never repeat an id, 2 to 4 reasons per job."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"user": ctx, "candidates": digests}, ensure_ascii=False)},
    ]


def _parse_rankings(content: str) -> list:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise InferenceError("no JSON object in the model reply")
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        raise InferenceError("model reply was not valid JSON") from None
    rankings = parsed.get("rankings") if isinstance(parsed, dict) else None
    if not isinstance(rankings, list) or not rankings:
        raise InferenceError("model reply had no rankings")
    return rankings


def validate_rankings(rankings: list, by_id: dict) -> list[tuple[str, float, list[str]]]:
    """Verify every claim, or raise. Returns [(job_id, score, rendered reasons)]."""
    verified: list[tuple[str, float, list[str]]] = []
    seen: set[str] = set()
    for entry in rankings:
        if not isinstance(entry, dict):
            raise InferenceError("ranking entry was not an object")
        job_id = entry.get("job_id")
        if not isinstance(job_id, str) or job_id not in by_id:
            raise InferenceError("ranking referenced an unknown job id")
        if job_id in seen:
            raise InferenceError("ranking repeated a job id")
        seen.add(job_id)

        score = entry.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise InferenceError("ranking score was not a number")
        score = float(score)
        if score != score or not (0.0 <= score <= 1.0):
            raise InferenceError("ranking score was outside 0..1")

        raw = entry.get("reasons")
        if not isinstance(raw, list):
            raise InferenceError("ranking reasons were not a list")

        candidate, scored = by_id[job_id]
        labeled = scored["reason_by_component"]
        evidence = job_evidence(job_of(candidate))
        rendered: list[str] = []
        for reason in raw:
            if not isinstance(reason, str):
                raise InferenceError("a reason was not a string")
            token = reason.strip()
            if token.startswith("field:"):
                component = token[len("field:") :].strip().lower()
                if component not in labeled:
                    raise InferenceError(f"reason cited an unavailable field '{component}'")
                text = labeled[component]
            elif token.startswith("quote:"):
                quote = token[len("quote:") :].strip().strip('"')
                if not (MIN_QUOTE_CHARS <= len(quote) <= MAX_QUOTE_CHARS):
                    raise InferenceError("quoted evidence had an unusable length")
                if normalize(quote) not in evidence:
                    raise InferenceError("quoted evidence is not in that posting")
                text = f'공고 원문: "{quote}"'
            else:
                raise InferenceError("reason was not an allowed evidence selector")
            if text not in rendered:
                rendered.append(text)
        if not (MIN_REASONS <= len(rendered) <= MAX_REASONS):
            raise InferenceError("a job did not carry 2-4 verified reasons")
        verified.append((job_id, round(score, 3), rendered))
    return verified


def rerank(ctx: dict, scored_candidates: list, config: NosanaConfig) -> tuple[list, str]:
    """One bounded attempt. ``scored_candidates`` is [(candidate, scored dict)]."""
    by_id: dict = {}
    digests: list[dict] = []
    for candidate, scored in scored_candidates[:MAX_CANDIDATES]:
        job_id = job_of(candidate).get("id")
        if not isinstance(job_id, str) or not job_id or job_id in by_id:
            continue
        by_id[job_id] = (candidate, scored)
        digests.append(_digest(candidate, scored))
    if not digests:
        raise InferenceError("no candidates with usable ids")

    model = config.model or _discover_model(config)
    response = _request_json(
        f"{config.base_url}/chat/completions",
        config,
        INFERENCE_TIMEOUT_SECONDS,
        {"model": model, "messages": _messages(ctx, digests), "temperature": 0, "max_tokens": 900},
    )
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise InferenceError("response had no choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise InferenceError("response had no message content")
    return validate_rankings(_parse_rankings(content), by_id), model
