"""Three live Daytona cases for the pitch: python pitch_demo.py."""
import copy
import json
import os
import sys
import time
from pathlib import Path

from harness import run_harness
from harness.runtime import DaytonaScheduleExecutionRuntime

ROOT = Path(__file__).resolve().parent


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # Keep the pitch reproducible without an optional inference service.
    os.environ.pop("NOSANA_API_KEY", None)
    base = json.loads((ROOT / "examples/primary_input.json").read_text(encoding="utf-8"))
    base["weekday"] = "SAT"
    cases = [
        ("매장 정리를 선호하는 사용자", {"preferred_jobs": ["매장 정리"]}),
        ("물류 업무를 선호하도록 변경", {"preferred_jobs": ["물류"]}),
        ("시간 협의 불허: 맞는 공고가 없으면 빈 결과", {"allow_negotiable_proposals": False}),
    ]
    runtime = DaytonaScheduleExecutionRuntime()
    output = ROOT / ".runtime" / "pitch"
    output.mkdir(parents=True, exist_ok=True)
    winners = []
    print("LIVE DEMO | 600개 합성 공고 → Daytona 계획 실행 → 개인화 추천", flush=True)
    print("토요일 14:00–20:00 · 강남 출발 → 용산 귀가 · 주간 목표 250,000원")
    print("이동 시간은 데모 추정치입니다. 시간 변경 제안은 고용주 확인이 필요합니다.\n", flush=True)
    for index, (label, overrides) in enumerate(cases, 1):
        payload = copy.deepcopy(base)
        payload.update(overrides)
        print(f"CASE {index}: {label}", flush=True)
        started = time.perf_counter()
        result = run_harness(payload, runtime=runtime)
        elapsed = time.perf_counter() - started
        (output / f"case-{index}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / f"input-{index}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        meta = result["meta"]
        assert meta["runtime_provider"] == "daytona" and meta["execution_ok"], "Real Daytona execution required"
        assert meta["jobs_loaded"] == 600, "Canonical full dataset required"
        print(f"  [Daytona] sandbox={meta['sandbox_id']}")
        print(f"  [Filter] {meta['jobs_loaded']} loaded → {meta['jobs_recruiting']} recruiting → {meta['jobs_final_candidates']} feasible")
        if index < 3:
            blocks = result["schedule"]
            assert [b["type"] for b in blocks] == ["travel", "job", "travel"]
            assert blocks[0]["start"] >= "14:00" and blocks[-1]["end"] <= "20:00"
            job = blocks[1]
            winners.append(job["job_id"])
            for block in blocks:
                label_text = block.get("title") or f"{block.get('from')} → {block.get('to')}"
                print(f"  {block['start']}–{block['end']} {label_text}")
            if meta.get("requires_employer_confirmation"):
                print(f"  [협의 필요] 게시 시간 {job.get('published_start')}–{job.get('published_end')}; 위 시간은 변경 제안")
            print(f"  예상 수입 {result['summary']['daily_income']:,.0f}원 | 목표 기여 {result['summary']['target_progress_percent']}%")
            for reason in result["recommendation"]["reasons"][:3]:
                print(f"  · {reason}")
        else:
            assert result["schedule"] == [], "Strict window must not invent a fitting shift"
            print("  맞는 공고 없음: 시간을 임의로 만들어 추천하지 않습니다.")
        print(f"  PASS | {elapsed:.1f}s | ranking={meta['ranking_provider']}\n", flush=True)
    assert winners[0] != winners[1], "Preference change should change the selected job"
    print("3/3 LIVE E2E PASS · 선호 변경에 따른 추천 변경 확인")
    print(f"전체 실제 응답 JSON: {output}")


if __name__ == "__main__":
    main()
