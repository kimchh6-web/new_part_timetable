"""Turning chosen weeks into the contract's JSON, without rounding off the truth.

Two rules govern everything in this module.

**Nothing is invented.** A field the dataset does not carry comes back ``null``,
not a default. 주휴수당 is never added to income and ``weeklyHolidayPayIncluded``
is therefore always ``false``; what the pipeline *can* check — whether a single
employer's assigned hours reach the 15-hour threshold, and whether the posting
itself claims to pay it — is reported separately, as two facts rather than one
conclusion. Qualifications the request has no way to speak to (licences,
experience, 우대사항) are listed as unverified instead of assumed met.

**Extra fields are additive.** Beyond the contract, plans carry ``hash``, jobs
carry ``holidayPay``/``unverifiedQualifications``/``assignment``, and three
warning codes outside the contract's five (``HOLIDAY_PAY_NOT_INCLUDED``,
``QUALIFICATIONS_UNVERIFIED``, ``NON_HOURLY_PAY``) may appear. A client that
renders ``warning.message`` and ignores unknown codes is unaffected; a client
that switches on the code sees only the five it knows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .candidates import FILTER_ORDER
from .constants import (
    BALANCED_PRIORITY_WEIGHT,
    DAY_INDEX,
    LONG_TRAVEL_MINUTES,
    MAX_BENEFITS,
    MAX_WEEKLY_WORK_HOURS,
    MAX_WEEKLY_WORK_HOURS_BASIS,
    SNIPPET_CHARS,
    TIGHT_TRANSFER_MINUTES,
    WEEKLY_HOLIDAY_MIN_HOURS,
)
from .plans import PLAN_LABELS, plan_id
from .slots import SLOT_DISCLOSURE
from .travel import TRAVEL_DISCLOSURE, TRAVEL_ESTIMATE_MODE

KST = timezone(timedelta(hours=9))

HOLIDAY_PAY_DISCLOSURE = (
    "monthlyIncome은 시급 × 배정 시간으로만 계산한 기본급입니다. 주휴수당은 포함하지 "
    f"않았습니다(weeklyHolidayPayIncluded=false). 고용주별 주 {WEEKLY_HOLIDAY_MIN_HOURS}시간 "
    "충족 여부는 공고별로 따로 표시하지만, 실제 지급 여부는 계약·출근율 등 이 요청에 없는 "
    "정보에 달려 있어 단정하지 않습니다."
)

QUALIFICATION_DISCLOSURE = (
    "자격 요건(면허·경력·우대사항·기타 요구사항)은 이 요청에 판단 근거가 없습니다. "
    "충족한 것으로 가정하지 않았고, 공고별로 확인이 필요한 항목을 그대로 내려보냅니다."
)

WEEKLY_CAP_DISCLOSURE = (
    f"주 {MAX_WEEKLY_WORK_HOURS}시간 상한은 이 데모의 제품 정책입니다. 법정 근로시간을 "
    "판정한 것이 아니고 법적 근거를 주장하지 않습니다. 실제 근로시간 규제는 계약 형태·나이·"
    "사업장에 따라 달라지므로 이 응답으로 판단하지 마십시오."
)

BALANCED_PRIORITY_DISCLOSURE = (
    f"priority가 rating 또는 flexibility면 균형안 순위에 실질 시급의 최대 "
    f"{round(BALANCED_PRIORITY_WEIGHT * 100)}%까지 가산합니다. rating은 공고 평점÷5, "
    "flexibility는 협의 가능 플래그 3개 중 충족 개수÷3이며, 평점이 없는 공고는 0점으로 "
    "둡니다(모르는 값을 좋게 치지 않습니다). 최적화 solver가 아니라 공개된 가중치 한 개입니다."
)

DATASET_DISCLOSURE = (
    "공고 데이터는 데모용 합성 데이터셋(600건)입니다. 실시간 크롤링 결과가 아니며 "
    "마감일(deadline) 경과 여부는 판정하지 않고 status가 recruiting인 공고만 사용합니다."
)


#: Said instead of :data:`DATASET_DISCLOSURE` when the rows came from the
#: reviewed public-job artifact. Two modes, kept apart: rows the collector
#: fetched itself (``live``) and rows an authorized party handed over
#: (``authorized_import``). Neither is the synthetic dataset, and neither is
#: described as the other.
LIVE_SOURCE_DISCLOSURE = (
    "공고 데이터는 운영자가 검토해 가져온 실제 공개 채용 공고입니다. 합성 데모 "
    "데이터셋(600건)이 아닙니다. 수집 시점 이후의 마감·변경은 반영되지 않으므로 "
    "지원 전에 원문 공고를 확인하십시오."
)

IMPORT_SOURCE_DISCLOSURE = (
    "공고 데이터는 권한을 받은 경로로 제공된 실제 채용 공고입니다(제공 수입). "
    "실시간 크롤링 결과가 아니고 합성 데모 데이터셋(600건)도 아닙니다. 제공 시점 "
    "이후의 마감·변경은 반영되지 않으므로 지원 전에 원문 공고를 확인하십시오."
)

#: Said when the rows came out of the operator's persistent job store. It
#: names the one thing that is different from a freshly reviewed artifact:
#: the rows were collected earlier and kept, and only those observed
#: recruiting within the store's freshness window were used.
STORED_SOURCE_DISCLOSURE = (
    "공고 데이터는 운영자 서버의 공고 저장소에 쌓여 있던 실제 채용 공고 중, 최근 "
    "24시간 안에 모집중으로 관측된 것만 사용했습니다. 합성 데모 데이터셋(600건)이 "
    "아닙니다. 저장된 값은 수집 시점의 관측이므로 그 이후의 마감·변경은 반영되지 "
    "않았습니다. 지원 전에 공고 원문을 다시 확인하세요."
)

#: Added to the above when the stored rows do not agree on one data mode:
#: the badge must not describe an authorized import as a live fetch, or the
#: other way round, just because they arrived in the same snapshot.
STORED_MIXED_MODE_DISCLOSURE = (
    "이 응답의 공고들은 수집 경로가 섞여 있습니다(직접 수집한 것과 권한을 받아 "
    "제공받은 것). 한 가지로 묶어 말하지 않고 섞였다고 그대로 밝힙니다."
)

UNKNOWN_SOURCE_DISCLOSURE = (
    "공고 데이터의 출처를 서버가 밝히지 않았습니다. 실제 공고인지 데모 데이터인지 "
    "이 응답만으로는 단정할 수 없습니다."
)

IMPORTED_TRAVEL_DISCLOSURE = (
    "가져온 공고에는 역에서 근무지까지의 도보 시간이 공개되어 있지 않아, 값이 없는 "
    "공고는 데모 추정치 15분으로 계산했습니다. 추정치일 뿐 상한이 아니며 실제 도보 "
    "시간은 더 길 수도 짧을 수도 있습니다. 경로를 조회한 값이 아닙니다."
)

IMPORTED_PROJECTION_DISCLOSURE = (
    "monthlyIncome은 이번 주 배정을 매주 반복한다고 가정하고 4.3을 곱한 추정값입니다. "
    "보장된 수입이 아니며, 근무 기간이 정해진 공고라면 그 기간까지만 유효합니다. "
    "공고에 반복 근무가 명시되지 않았거나 하루·날짜 지정 공고는 후보에서 제외했습니다."
)

IMPORTED_DEADLINE_DISCLOSURE = (
    "마감이 날짜로 공개된 공고는 지난 것을 제외했지만, '채용시 마감'처럼 날짜가 없는 "
    "마감 표기는 판정하지 않았습니다. 모집 상태는 수집 시점의 표기일 뿐입니다."
)

IMPORTED_QUALIFICATION_DISCLOSURE = (
    "실제 공고의 자격 요건(면허·경력·나이 등)은 이 요청으로 확인할 수 없습니다. "
    "충족한 것으로 간주하지 않았고, 확인이 필요한 항목으로만 표시합니다."
)


def apply_job_source(
    response: dict[str, Any], provenance: dict[str, Any] | None
) -> dict[str, Any]:
    """Make the response say where its rows actually came from.

    ``provenance`` is written by the controller (never by a user payload) and
    carries ``job_source``/``data_mode`` plus the source counts. This function
    puts those on ``meta`` and swaps the dataset disclosure for one that is
    true of the rows that were actually used — the synthetic-dataset sentence
    must not survive on a response built from real postings, and the reverse
    must not happen either.

    It deliberately does **not** touch ``response['source']``: that field names
    the ranking engine (``fallback`` = deterministic, no LLM) and has nothing
    to do with where the job rows came from.
    """
    if not isinstance(response, dict) or not isinstance(provenance, dict):
        return response
    meta = response.setdefault("meta", {})
    job_source = provenance.get("job_source") or "unknown"
    data_mode = provenance.get("data_mode") or "unknown"
    meta["job_source"] = job_source
    meta["data_mode"] = data_mode
    counts = provenance.get("source_counts")
    if isinstance(counts, dict):
        meta["source_counts"] = dict(counts)
    permission = provenance.get("source_permission")
    if isinstance(permission, dict):
        # What the operator recorded, not a finding of this pipeline.
        meta["source_permission"] = dict(permission)

    notes = [
        note
        for note in (meta.get("disclosures") or [])
        if note not in (DATASET_DISCLOSURE, QUALIFICATION_DISCLOSURE)
    ]
    meta["disclosures"] = notes + source_disclosures(job_source, data_mode)
    return response


def source_disclosures(job_source: str, data_mode: str) -> list[str]:
    """The dataset statements that are true for one job source."""
    if job_source == "demo_json" and data_mode in ("demo", "unknown"):
        return [DATASET_DISCLOSURE, QUALIFICATION_DISCLOSURE]
    if job_source == "public_web" and data_mode == "live":
        head = [LIVE_SOURCE_DISCLOSURE]
    elif job_source == "public_web" and data_mode == "authorized_import":
        head = [IMPORT_SOURCE_DISCLOSURE]
    elif job_source == "job_store" and data_mode in ("live", "authorized_import", "mixed"):
        # Rows kept in the operator's store. The sentence about *where they
        # came from* is still the one that matches each row's own data mode,
        # and a snapshot carrying both says so rather than choosing.
        head = [STORED_SOURCE_DISCLOSURE]
        if data_mode == "live":
            head.append(LIVE_SOURCE_DISCLOSURE)
        elif data_mode == "authorized_import":
            head.append(IMPORT_SOURCE_DISCLOSURE)
        else:
            head.append(STORED_MIXED_MODE_DISCLOSURE)
    else:
        return [UNKNOWN_SOURCE_DISCLOSURE, QUALIFICATION_DISCLOSURE]
    return head + [
        IMPORTED_TRAVEL_DISCLOSURE,
        IMPORTED_PROJECTION_DISCLOSURE,
        IMPORTED_DEADLINE_DISCLOSURE,
        IMPORTED_QUALIFICATION_DISCLOSURE,
    ]


def build_response(
    *,
    request: dict[str, Any],
    slots: list[dict[str, Any]],
    funnel: dict[str, Any],
    search: dict[str, Any],
    selected: list[dict[str, Any]],
    total_latency_ms: int,
    now: datetime | None,
) -> dict[str, Any]:
    """Assemble the full ``200`` body."""
    plans = [_plan(outcome, request) for outcome in selected]
    return {
        "requestId": request_id(request),
        "generatedAt": _timestamp(now),
        "source": "fallback",
        "availableSlots": slots,
        "candidateCount": funnel["candidateCount"],
        "plans": plans,
        "meta": {
            "llmLatencyMs": 0,
            "totalLatencyMs": total_latency_ms,
            "filteredFrom": funnel["filteredFrom"],
            "engine": "deterministic",
            "llmUsed": False,
            "travelEstimateMode": TRAVEL_ESTIMATE_MODE,
            "weeklyWorkHoursCap": {
                "hours": MAX_WEEKLY_WORK_HOURS,
                "basis": MAX_WEEKLY_WORK_HOURS_BASIS,
            },
            "funnel": {key: funnel[key] for key in ("filteredFrom", *FILTER_ORDER)},
            "filterOrder": funnel["filterOrder"],
            "search": search,
            "unknownRequestFields": request["unknownFields"],
            "disclosures": disclosures(request),
        },
    }


def disclosures(request: dict[str, Any]) -> list[str]:
    """Statements that are true of every plan in the response."""
    notes = [
        TRAVEL_DISCLOSURE,
        SLOT_DISCLOSURE,
        HOLIDAY_PAY_DISCLOSURE,
        QUALIFICATION_DISCLOSURE,
        DATASET_DISCLOSURE,
        WEEKLY_CAP_DISCLOSURE,
        "공고에 게시된 근무 시각은 조정하지 않습니다. timeNegotiable은 표시만 하고 계산에 쓰지 "
        "않습니다.",
        "자정을 넘기는 공고(예: 22:00~06:00)는 날짜를 추정해야 해서 후보에서 제외했습니다.",
        "추천은 제한된 후보 풀 안에서의 완전 탐색 결과입니다. 전체 최적해임을 주장하지 않습니다.",
    ]
    if request["search"]["priority"] in ("rating", "flexibility"):
        notes.append(BALANCED_PRIORITY_DISCLOSURE)
    if request["profile"]["constraints"]["age"] is None:
        notes.append("age가 없어 연령 조건은 확인하지 않았습니다.")
    if request["unknownFields"]:
        notes.append(
            "계약에 없는 요청 필드는 무시했습니다: "
            + ", ".join(request["unknownFields"])
        )
    return notes


def request_id(request: dict[str, Any]) -> str:
    """Deterministic id: the same request always logs under the same handle."""
    canonical = json.dumps(_stripped(request), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "req_" + digest[:10].upper()


# --------------------------------------------------------------------------
# plans


def _plan(outcome: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    profile = request["profile"]
    target = profile["targetAmount"]
    monthly = round(outcome["monthlyIncome"])
    plan_type = outcome["type"]
    pinned = set(request["regenerate"]["pinnedJobIds"])

    jobs = [
        _plan_job(candidate, outcome, pinned=candidate["jobId"] in pinned)
        for candidate in sorted(outcome["candidates"], key=lambda c: c["jobId"])
    ]
    metrics = {
        "monthlyIncome": monthly,
        "targetAchievementRate": round(monthly / target, 2) if target else 0.0,
        "weeklyWorkHours": _tidy(outcome["weeklyWorkHours"]),
        "weeklyTravelMinutes": int(outcome["weeklyTravelMinutes"]),
        "effectiveHourlyWage": round(outcome["effectiveHourlyWage"]),
        "weeklyHolidayPayIncluded": False,
    }
    return {
        "id": plan_id(plan_type, outcome["hash"]),
        "hash": outcome["hash"],
        "type": plan_type,
        "label": PLAN_LABELS[plan_type],
        "reason": _reason(plan_type, outcome, metrics, request),
        "metrics": metrics,
        "jobs": jobs,
        "warnings": _warnings(outcome, jobs, metrics, request),
    }


def _reason(
    plan_type: str,
    outcome: dict[str, Any],
    metrics: dict[str, Any],
    request: dict[str, Any],
) -> str:
    days = sorted(
        {shift["day"] for c in outcome["candidates"] for shift in c["assigned"]},
        key=lambda day: DAY_INDEX[day],
    )
    priority = request["search"]["priority"]
    balanced_head = "근무·이동 시간을 합친 실질 시급이 가장 높습니다."
    if priority in ("rating", "flexibility"):
        axis = "평점" if priority == "rating" else "일정 협의 가능성"
        balanced_head = (
            f"실질 시급에 요청한 우선순위({axis})를 가산해 가장 높은 조합입니다."
        )
    head = {
        "maxIncome": "탐색한 조합 중 주급이 가장 높습니다.",
        "minTravel": "탐색한 조합 중 주간 이동 시간이 가장 짧습니다.",
        "balanced": balanced_head,
    }[plan_type]
    rate = metrics["targetAchievementRate"]
    fit = (
        f"목표 {int(request['profile']['targetAmount']):,}원의 {round(rate * 100)}% 수준입니다."
    )
    return (
        f"{head} 주 {len(days)}일({', '.join(days)}) 근무, "
        f"주 {metrics['weeklyWorkHours']}시간, 이동 {metrics['weeklyTravelMinutes']}분. {fit}"
    )


def _plan_job(
    candidate: dict[str, Any], outcome: dict[str, Any], *, pinned: bool
) -> dict[str, Any]:
    job = candidate["job"]
    flexibility = job.get("scheduleFlexibility") or {}
    pay_detail = job.get("payDetail") or {}
    contact = job.get("contact") or {}

    shifts = []
    for shift in sorted(
        candidate["assigned"], key=lambda s: (DAY_INDEX[s["day"]], s["startMinutes"])
    ):
        travel = outcome["travelByShift"][
            (candidate["jobId"], shift["day"], shift["startMinutes"])
        ]
        shifts.append(
            {
                "day": shift["day"],
                "start": shift["start"],
                "end": shift["end"],
                "travel": dict(travel),
            }
        )

    return {
        "jobId": candidate["jobId"],
        "pinned": pinned,
        "title": _text(job.get("title")),
        "company": _text(job.get("company")),
        "platform": _text(job.get("platform")),
        "category": _text(job.get("category")),
        "location": _text(job.get("location")),
        "address": _text(job.get("address")),
        "hourlyWage": job.get("hourlyWage"),
        "rating": _number(job.get("rating")),
        "reviewCount": _number(job.get("reviewCount")),
        "thumbnail": _text(job.get("thumbnail")),
        "descriptionSnippet": _snippet(job.get("description")),
        "sourceUrl": _text(job.get("sourceUrl")),
        "contact": {
            "manager": _text(contact.get("manager")),
            "phone": _text(contact.get("phone")),
            "kakao": _text(contact.get("kakao")),
            "applyUrl": _text(contact.get("applyUrl")),
            "preferred": _text(contact.get("preferred")),
        },
        "timeNegotiable": flexibility.get("timeNegotiable") is True,
        "daysNegotiable": flexibility.get("daysNegotiable") is True,
        "minWeeks": _number(job.get("minWeeks")),
        "benefits": _benefits(job.get("benefits")),
        "assignedShifts": shifts,
        "weeklyHours": _tidy(candidate["weeklyHours"]),
        "weeklyPay": round(candidate["weeklyPay"]),
        "payType": _text(pay_detail.get("payType")),
        "assignment": {
            "publishedShiftCount": candidate["publishedShiftCount"],
            "assignedShiftCount": len(candidate["assigned"]),
            "droppedShifts": candidate["droppedShifts"],
            "minDaysPerWeek": flexibility.get("minDaysPerWeek"),
            "maxDaysPerWeek": flexibility.get("maxDaysPerWeek"),
        },
        "holidayPay": {
            "includedInIncome": False,
            "employerHoursThresholdMet": candidate["weeklyHours"]
            >= WEEKLY_HOLIDAY_MIN_HOURS,
            "postingClaimsWeeklyHolidayPay": _tri_state(
                pay_detail.get("weeklyHolidayPay")
            ),
        },
        "unverifiedQualifications": _unverified(job.get("qualifications")),
    }


def _warnings(
    outcome: dict[str, Any],
    jobs: list[dict[str, Any]],
    metrics: dict[str, Any],
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    by_id = {c["jobId"]: c for c in outcome["candidates"]}

    for job in jobs:
        candidate = by_id[job["jobId"]]
        # One warning per job, not one per shift: four identical "MON/TUE/THU/FRI
        # 편도 43분" lines tell the reader nothing the day list does not.
        long_travel = [
            shift
            for shift in job["assignedShifts"]
            if shift["travel"]["legMinutes"] >= LONG_TRAVEL_MINUTES
        ]
        if long_travel:
            worst = max(shift["travel"]["legMinutes"] for shift in long_travel)
            warnings.append(
                _warning(
                    "LONG_TRAVEL",
                    f"{', '.join(s['day'] for s in long_travel)} 편도 이동이 최대 {worst}분입니다.",
                    job["jobId"],
                )
            )
        tight = [
            shift
            for shift in job["assignedShifts"]
            if shift["travel"]["slackMinutes"] < TIGHT_TRANSFER_MINUTES
        ]
        if tight:
            least = min(shift["travel"]["slackMinutes"] for shift in tight)
            warnings.append(
                _warning(
                    "TIGHT_TRANSFER",
                    f"{', '.join(s['day'] for s in tight)} 출발 여유가 최소 {least}분뿐입니다.",
                    job["jobId"],
                )
            )
        if _is_short_period(candidate["job"]):
            warnings.append(
                _warning(
                    "SHORT_PERIOD",
                    f"단기 공고입니다({_text(candidate['job'].get('workPeriod')) or '기간 미상'}).",
                    job["jobId"],
                )
            )
        if job["payType"] and job["payType"] != "hourly":
            warnings.append(
                _warning(
                    "NON_HOURLY_PAY",
                    f"급여 형태가 {job['payType']}입니다. 주급은 시급 × 배정 시간으로 추정한 값입니다.",
                    job["jobId"],
                )
            )
        if job["unverifiedQualifications"]:
            warnings.append(
                _warning(
                    "QUALIFICATIONS_UNVERIFIED",
                    "확인이 필요한 지원 조건이 있습니다: "
                    + ", ".join(job["unverifiedQualifications"]),
                    job["jobId"],
                )
            )
        if job["assignment"]["droppedShifts"]:
            dropped = ", ".join(s["day"] for s in job["assignment"]["droppedShifts"])
            warnings.append(
                _warning(
                    "DAY_SUBSET",
                    f"공고의 {dropped} 근무는 배정하지 않았습니다. 일수 협의가 가능한 공고입니다.",
                    job["jobId"],
                )
            )

    if metrics["monthlyIncome"] < request["profile"]["targetAmount"]:
        warnings.append(
            _warning(
                "BELOW_TARGET",
                f"월 예상 수입 {metrics['monthlyIncome']:,}원으로 목표 "
                f"{int(request['profile']['targetAmount']):,}원에 못 미칩니다.",
            )
        )
    if request["profile"]["constraints"]["age"] is None:
        warnings.append(
            _warning("AGE_UNVERIFIED", "age를 받지 못해 연령 조건을 확인하지 못했습니다.")
        )
    warnings.append(
        _warning(
            "HOLIDAY_PAY_NOT_INCLUDED",
            f"수입은 기본급만 계산했습니다. 주휴수당은 포함하지 않았습니다. "
            f"(고용주별 주 {WEEKLY_HOLIDAY_MIN_HOURS}시간 충족: "
            f"{len(outcome['holidayThresholdJobIds'])}곳)",
        )
    )
    return warnings


def _warning(code: str, message: str, job_id: str | None = None) -> dict[str, Any]:
    warning: dict[str, Any] = {"code": code, "message": message}
    if job_id is not None:
        warning["jobId"] = job_id
    return warning


# --------------------------------------------------------------------------
# field helpers — every one of them prefers ``null`` over a guess


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _tri_state(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _snippet(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if len(text) <= SNIPPET_CHARS:
        return text
    return text[:SNIPPET_CHARS] + "…"


def _benefits(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)][:MAX_BENEFITS]


def _unverified(qualifications: Any) -> list[str]:
    """Facts the weekly request simply does not contain an answer for."""
    if not isinstance(qualifications, dict):
        return []
    notes: list[str] = []
    if qualifications.get("experienceRequired") is True:
        notes.append("경력 필요")
    for key, label in (("licenses", "자격증"), ("requirements", "요구사항"), ("preferred", "우대")):
        values = qualifications.get(key)
        if isinstance(values, list):
            notes.extend(f"{label}: {item}" for item in values if isinstance(item, str))
    return notes


def _is_short_period(job: dict[str, Any]) -> bool:
    period = job.get("workPeriod")
    if isinstance(period, str) and period.startswith("단기"):
        return True
    min_weeks = job.get("minWeeks")
    return isinstance(min_weeks, int) and not isinstance(min_weeks, bool) and min_weeks <= 1


def _tidy(hours: float) -> float | int:
    rounded = round(float(hours), 2)
    return int(rounded) if rounded == int(rounded) else rounded


def _stripped(request: dict[str, Any]) -> dict[str, Any]:
    profile = request["profile"]
    return {
        "profile": {
            "role": profile["role"],
            "home": profile["home"],
            "fixedSchedules": [
                {k: v for k, v in item.items() if not k.endswith("Minutes")}
                for item in profile["fixedSchedules"]
            ],
            "constraints": profile["constraints"],
            "targetAmount": profile["targetAmount"],
        },
        "search": {
            "jobCount": request["search"]["jobCount"],
            "categories": request["search"]["categories"],
            "priority": request["search"]["priority"],
        },
        "regenerate": request["regenerate"],
    }


def _timestamp(now: datetime | None) -> str:
    moment = now if now is not None else datetime.now(KST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    return moment.astimezone(KST).isoformat(timespec="seconds")


__all__ = [
    "BALANCED_PRIORITY_DISCLOSURE",
    "DATASET_DISCLOSURE",
    "IMPORTED_DEADLINE_DISCLOSURE",
    "IMPORTED_PROJECTION_DISCLOSURE",
    "IMPORTED_QUALIFICATION_DISCLOSURE",
    "IMPORTED_TRAVEL_DISCLOSURE",
    "IMPORT_SOURCE_DISCLOSURE",
    "LIVE_SOURCE_DISCLOSURE",
    "UNKNOWN_SOURCE_DISCLOSURE",
    "apply_job_source",
    "source_disclosures",
    "HOLIDAY_PAY_DISCLOSURE",
    "KST",
    "QUALIFICATION_DISCLOSURE",
    "WEEKLY_CAP_DISCLOSURE",
    "build_response",
    "disclosures",
    "request_id",
]
