"""The optional weekly semantic layer: what it may change, and what it may not.

Every test here injects its provider. Nothing in this file opens a socket, and
the one thing each case is really about is the same question asked twice: did a
sentence or a number reach the response without being checked first?

The success case pins the two halves of the promise — the plans come back in
the model's order with reasons rendered from *its own* evidence bank, and every
other byte of the answer is the deterministic one. The failure cases all end in
the same place: the deterministic answer, unchanged, with a status code saying
why. A partial application would be the actual bug.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness.weekly import build_weekly_recommendations
from harness.weekly.semantic import (
    DEFAULT_MODEL,
    MAX_EVIDENCE_PER_PLAN,
    SemanticConfig,
    SemanticError,
    _http_invoke,
    build_evidence_bank,
    enrich_weekly_response,
    load_provider_config,
    validate_selection,
)

from tests.test_weekly_fixtures import canonical_payload, canonical_rows

CONFIG_ENV = {
    "NOSANA_API_KEY": "test-key-not-a-real-secret",
    "WEEKLY_LLM_BASE_URL": "https://inference.example.invalid/v1",
    "WEEKLY_LLM_MODEL": "test/model-1",
}


def deterministic() -> tuple[dict, dict]:
    """The real pipeline's answer for the canonical request."""
    payload = canonical_payload()
    return payload, build_weekly_recommendations(copy.deepcopy(payload), canonical_rows())


def config():
    return load_provider_config(CONFIG_ENV)


def reply(selection: list[dict]) -> str:
    return json.dumps({"plans": selection}, ensure_ascii=False)


def first_ids(bank: dict, plan_id: str, count: int = 2) -> list[str]:
    return [item["id"] for item in bank[plan_id][:count]]


def recorder(content, *, model="test/model-1"):
    """An ``invoke`` that answers with ``content`` and records its arguments."""
    calls: list[dict] = []

    def invoke(cfg, messages, deadline, clock):
        calls.append({"config": cfg, "messages": messages, "deadline": deadline})
        if isinstance(content, BaseException):
            raise content
        text = content(cfg, messages) if callable(content) else content
        return text, model

    invoke.calls = calls
    return invoke


def hard_parts(response: dict) -> list[dict]:
    """Everything a plan carries except the two fields this layer may touch."""
    parts = []
    for plan in response["plans"]:
        stripped = {k: v for k, v in plan.items() if k not in ("reason", "fitScore")}
        parts.append(stripped)
    return sorted(parts, key=lambda plan: plan["id"])


class EvidenceBankTests(unittest.TestCase):
    def test_every_sentence_restates_the_response_or_the_request(self):
        payload, response = deterministic()
        bank = build_evidence_bank(payload, response)
        self.assertEqual(set(bank), {plan["id"] for plan in response["plans"]})
        for plan in response["plans"]:
            texts = [item["text"] for item in bank[plan["id"]]]
            self.assertIn(plan["reason"], texts)
            # The trade-off numbers a reader could check against the plan.
            self.assertTrue(any(f"{plan['metrics']['monthlyIncome']:,}원" in t for t in texts))
            self.assertTrue(
                any(f"{plan['metrics']['weeklyTravelMinutes']}분" in t for t in texts)
            )
            for job in plan["jobs"]:
                self.assertTrue(any(job["title"] in t for t in texts))
                self.assertTrue(any(f"{job['hourlyWage']:,}원" in t for t in texts))

    def test_the_bank_stays_inside_the_budgeted_size(self):
        payload, response = deterministic()
        bank = build_evidence_bank(payload, response)
        for plan_id, items in bank.items():
            self.assertLessEqual(len(items), MAX_EVIDENCE_PER_PLAN, plan_id)
            self.assertEqual(len({item["id"] for item in items}), len(items))

    def test_warnings_are_kept_most_relevant_first_and_deduplicated(self):
        payload, response = deterministic()
        plan = copy.deepcopy(response["plans"][0])
        plan["warnings"] = [
            {"code": "QUALIFICATIONS_UNVERIFIED", "message": "확인이 필요한 조건 A", "jobId": "j1"},
            {"code": "QUALIFICATIONS_UNVERIFIED", "message": "확인이 필요한 조건 B", "jobId": "j2"},
            {"code": "BELOW_TARGET", "message": "목표에 못 미칩니다."},
        ]
        plan["jobs"] = []
        texts = [item["text"] for item in build_evidence_bank(payload, {"plans": [plan]})[plan["id"]]]
        warnings = [text for text in texts if text.startswith("주의: ")]
        self.assertEqual(warnings, ["주의: 목표에 못 미칩니다.", "주의: 확인이 필요한 조건 A"])

    def test_evidence_ids_are_scoped_to_one_plan(self):
        payload, response = deterministic()
        bank = build_evidence_bank(payload, response)
        prefixes = {plan_id: {item["id"][:3] for item in items} for plan_id, items in bank.items()}
        self.assertEqual(len(prefixes), len(response["plans"]))
        self.assertEqual(len({next(iter(p)) for p in prefixes.values()}), len(prefixes))


class EnrichmentSuccessTests(unittest.TestCase):
    def setUp(self):
        self.payload, self.response = deterministic()
        self.bank = build_evidence_bank(self.payload, self.response)
        self.ids = [plan["id"] for plan in self.response["plans"]]

    def enrich(self, content, **kwargs):
        invoke = recorder(content)
        result = enrich_weekly_response(
            self.payload, self.response, config=config(), invoke=invoke, **kwargs
        )
        return result, invoke

    def test_order_follows_fit_score_and_reasons_come_from_evidence(self):
        selection = [
            {"planId": self.ids[0], "fitScore": 0.10, "reasonEvidenceIds": first_ids(self.bank, self.ids[0])},
            {"planId": self.ids[1], "fitScore": 0.90, "reasonEvidenceIds": first_ids(self.bank, self.ids[1], 1)},
            {"planId": self.ids[2], "fitScore": 0.50, "reasonEvidenceIds": first_ids(self.bank, self.ids[2], 3)},
        ]
        result, invoke = self.enrich(reply(selection))

        self.assertEqual([plan["id"] for plan in result["plans"]], [self.ids[1], self.ids[2], self.ids[0]])
        self.assertEqual([plan["fitScore"] for plan in result["plans"]], [0.9, 0.5, 0.1])
        for plan in result["plans"]:
            chosen = [item["text"] for item in self.bank[plan["id"]]]
            self.assertTrue(plan["reason"])
            # Not one word the model wrote itself.
            for sentence in plan["reason"].split(". "):
                self.assertTrue(any(sentence in text for text in chosen))
        self.assertEqual(len(invoke.calls), 1)

    def test_metadata_says_a_live_model_answered(self):
        selection = [
            {"planId": plan_id, "fitScore": 0.5, "reasonEvidenceIds": first_ids(self.bank, plan_id, 1)}
            for plan_id in self.ids
        ]
        result, _ = self.enrich(reply(selection))
        meta = result["meta"]
        self.assertEqual(result["source"], "llm")
        self.assertEqual(meta["engine"], "hybrid")
        self.assertIs(meta["llmUsed"], True)
        self.assertEqual(meta["llmProvider"], "nosana")
        self.assertEqual(meta["llmModel"], "test/model-1")
        self.assertEqual(meta["llmStatus"], "success")
        self.assertIsInstance(meta["llmLatencyMs"], int)

    def test_nothing_but_order_reason_and_fit_score_changes(self):
        before = copy.deepcopy(self.response)
        selection = [
            {"planId": plan_id, "fitScore": score, "reasonEvidenceIds": first_ids(self.bank, plan_id, 2)}
            for plan_id, score in zip(self.ids, (0.2, 0.8, 0.4))
        ]
        result, _ = self.enrich(reply(selection))

        self.assertEqual(hard_parts(result), hard_parts(before))
        for key in ("requestId", "generatedAt", "availableSlots", "candidateCount"):
            self.assertEqual(result[key], before[key])
        for key in ("filteredFrom", "funnel", "filterOrder", "search", "disclosures"):
            self.assertEqual(result["meta"][key], before["meta"][key])
        self.assertEqual(self.response, before)  # the input was not mutated

    def test_equal_scores_keep_the_deterministic_order(self):
        selection = [
            {"planId": plan_id, "fitScore": 0.5, "reasonEvidenceIds": first_ids(self.bank, plan_id, 1)}
            for plan_id in reversed(self.ids)
        ]
        result, _ = self.enrich(reply(selection))
        self.assertEqual([plan["id"] for plan in result["plans"]], self.ids)

    def test_the_prompt_carries_evidence_and_no_secret(self):
        selection = [
            {"planId": plan_id, "fitScore": 0.5, "reasonEvidenceIds": first_ids(self.bank, plan_id, 1)}
            for plan_id in self.ids
        ]
        _, invoke = self.enrich(reply(selection))
        sent = json.dumps(invoke.calls[0]["messages"], ensure_ascii=False)
        self.assertNotIn(CONFIG_ENV["NOSANA_API_KEY"], sent)
        for plan_id in self.ids:
            self.assertIn(plan_id, sent)
            self.assertIn(self.bank[plan_id][0]["id"], sent)

    def test_fenced_json_with_leading_whitespace_is_still_read(self):
        selection = [
            {"planId": plan_id, "fitScore": 0.5, "reasonEvidenceIds": first_ids(self.bank, plan_id, 1)}
            for plan_id in self.ids
        ]
        result, _ = self.enrich("\n\n  ```json\n" + reply(selection) + "\n```")
        self.assertEqual(result["meta"]["llmStatus"], "success")


class EnrichmentFallbackTests(unittest.TestCase):
    """Every way an answer can fail to be verifiable ends in the same place."""

    def setUp(self):
        self.payload, self.response = deterministic()
        self.bank = build_evidence_bank(self.payload, self.response)
        self.ids = [plan["id"] for plan in self.response["plans"]]
        self.before = copy.deepcopy(self.response)

    def assertFellBack(self, result, status):
        self.assertEqual(result["source"], "fallback")
        self.assertEqual(result["meta"]["engine"], "deterministic")
        self.assertIs(result["meta"]["llmUsed"], False)
        self.assertEqual(result["meta"]["llmStatus"], status)
        self.assertNotIn("llmProvider", result["meta"])
        self.assertNotIn("llmModel", result["meta"])
        # The deterministic answer, down to the reason sentences and the order.
        self.assertEqual(result["plans"], self.before["plans"])
        self.assertEqual([plan["id"] for plan in result["plans"]], self.ids)
        for plan in result["plans"]:
            self.assertNotIn("fitScore", plan)

    def enrich(self, content, **kwargs):
        invoke = recorder(content)
        result = enrich_weekly_response(
            self.payload, self.response, config=config(), invoke=invoke, **kwargs
        )
        return result, invoke

    def selection(self, **overrides):
        entries = [
            {"planId": plan_id, "fitScore": 0.5, "reasonEvidenceIds": first_ids(self.bank, plan_id, 1)}
            for plan_id in self.ids
        ]
        entries[0].update(overrides)
        return entries

    def test_not_valid_json(self):
        result, _ = self.enrich("나는 첫 번째 플랜을 추천합니다.")
        self.assertFellBack(result, "invalid_response")

    def test_truncated_json(self):
        result, _ = self.enrich('{"plans": [{"planId": "plan_')
        self.assertFellBack(result, "invalid_response")

    def test_unknown_plan_id(self):
        result, _ = self.enrich(reply(self.selection(planId="plan_does_not_exist")))
        self.assertFellBack(result, "invalid_response")

    def test_incomplete_plan_set(self):
        entries = self.selection()[:2]
        result, _ = self.enrich(reply(entries))
        self.assertFellBack(result, "invalid_response")

    def test_repeated_plan_id(self):
        entries = self.selection()
        entries[1]["planId"] = entries[0]["planId"]
        result, _ = self.enrich(reply(entries))
        self.assertFellBack(result, "invalid_response")

    def test_evidence_id_from_another_plan(self):
        borrowed = first_ids(self.bank, self.ids[1], 1)
        result, _ = self.enrich(reply(self.selection(reasonEvidenceIds=borrowed)))
        self.assertFellBack(result, "invalid_response")

    def test_unknown_evidence_id(self):
        result, _ = self.enrich(reply(self.selection(reasonEvidenceIds=["p9e99"])))
        self.assertFellBack(result, "invalid_response")

    def test_unbounded_evidence_id(self):
        result, _ = self.enrich(reply(self.selection(reasonEvidenceIds=["p1e01" + "x" * 200])))
        self.assertFellBack(result, "invalid_response")

    def test_too_many_evidence_ids(self):
        flood = [item["id"] for item in self.bank[self.ids[0]][:8]]
        result, _ = self.enrich(reply(self.selection(reasonEvidenceIds=flood)))
        self.assertFellBack(result, "invalid_response")

    def test_no_evidence_ids(self):
        result, _ = self.enrich(reply(self.selection(reasonEvidenceIds=[])))
        self.assertFellBack(result, "invalid_response")

    def test_score_above_one(self):
        result, _ = self.enrich(reply(self.selection(fitScore=1.4)))
        self.assertFellBack(result, "invalid_response")

    def test_score_not_a_number(self):
        result, _ = self.enrich(reply(self.selection(fitScore="높음")))
        self.assertFellBack(result, "invalid_response")

    def test_score_not_finite(self):
        entries = self.selection()
        broken = json.dumps({"plans": entries}).replace('"fitScore": 0.5', '"fitScore": NaN', 1)
        result, _ = self.enrich(broken)
        self.assertFellBack(result, "invalid_response")

    def test_model_prose_instead_of_evidence_ids(self):
        # The one thing this layer exists to refuse: a reason the model wrote.
        entries = self.selection()
        entries[0]["reason"] = "이 플랜은 당신의 바리스타 경력과 잘 맞습니다."
        entries[0]["reasonEvidenceIds"] = ["이 플랜은 당신의 바리스타 경력과 잘 맞습니다."]
        result, _ = self.enrich(reply(entries))
        self.assertFellBack(result, "invalid_response")
        self.assertNotIn("바리스타 경력", json.dumps(result, ensure_ascii=False))

    def test_timeout(self):
        result, _ = self.enrich(SemanticError("timeout", "request timed out"))
        self.assertFellBack(result, "timeout")

    def test_provider_error_never_leaks_its_words(self):
        result, _ = self.enrich(SemanticError("provider_error", "HTTP 503 node-xyz private"))
        self.assertFellBack(result, "provider_error")
        self.assertNotIn("node-xyz", json.dumps(result, ensure_ascii=False))

    def test_unexpected_exception_still_answers(self):
        result, _ = self.enrich(RuntimeError("bug in the layer"))
        self.assertFellBack(result, "provider_error")

    def test_no_key_does_not_call_the_provider(self):
        invoke = recorder(reply(self.selection()))
        result = enrich_weekly_response(self.payload, self.response, invoke=invoke, env={})
        self.assertFellBack(result, "not_configured")
        self.assertEqual(invoke.calls, [])

    def test_provider_switched_off_does_not_call_the_provider(self):
        invoke = recorder(reply(self.selection()))
        result = enrich_weekly_response(
            self.payload, self.response, invoke=invoke, env={**CONFIG_ENV, "WEEKLY_LLM_PROVIDER": "off"}
        )
        self.assertFellBack(result, "not_configured")
        self.assertEqual(invoke.calls, [])

    def test_exhausted_budget_does_not_call_the_provider(self):
        result, invoke = self.enrich(reply(self.selection()), budget_seconds=0.2)
        self.assertFellBack(result, "budget_exhausted")
        self.assertEqual(invoke.calls, [])

    def test_the_deadline_handed_to_the_provider_respects_the_budget(self):
        clock = iter([100.0, 100.0, 100.4]).__next__
        invoke = recorder(reply(self.selection()))
        enrich_weekly_response(
            self.payload, self.response, config=config(), invoke=invoke,
            budget_seconds=3.0, clock=clock,
        )
        self.assertEqual(invoke.calls[0]["deadline"], 103.0)


class ConfigTests(unittest.TestCase):
    def test_weekly_names_do_not_read_the_daily_endpoint(self):
        loaded = load_provider_config(
            {"NOSANA_API_KEY": "k", "NOSANA_BASE_URL": "https://old-node.invalid/v1",
             "NOSANA_MODEL": "old-model"}
        )
        self.assertEqual(loaded.base_url, "https://inference.nosana.com/v1")
        self.assertEqual(loaded.model, DEFAULT_MODEL)

    def test_explicit_weekly_settings_win(self):
        loaded = load_provider_config(CONFIG_ENV)
        self.assertEqual(loaded.base_url, "https://inference.example.invalid/v1")
        self.assertEqual(loaded.model, "test/model-1")

    def test_repr_never_carries_the_key(self):
        self.assertNotIn(CONFIG_ENV["NOSANA_API_KEY"], repr(load_provider_config(CONFIG_ENV)))
        self.assertIn("***", repr(load_provider_config(CONFIG_ENV)))


class SlowTransportTests(unittest.TestCase):
    """The one test here that opens a socket — to a server on this machine.

    A provider that answers a byte at a time is the case a per-read timeout
    does not catch: every read succeeds, so nothing ever times out, and the
    request outlives the response deadline while the body dribbles in. The
    budget has to bound the *body*, not just each read of it.
    """

    @classmethod
    def setUpClass(cls):
        class Trickle(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # No Content-Length: the body simply never ends.
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except Exception:
                    pass

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_a_trickled_body_is_cut_off_at_the_deadline(self):
        cfg = SemanticConfig("nosana", "test-key-not-a-real-secret", self.base, "test/model-1")
        started = time.monotonic()
        with self.assertRaises(SemanticError) as caught:
            _http_invoke(cfg, [{"role": "user", "content": "x"}], started + 1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.status, "timeout")
        # The budget, plus the slack of one 0.05s write — not the whole night.
        self.assertLess(elapsed, 3.0, f"body read ran for {elapsed:.2f}s")

    def test_a_trickled_body_leaves_the_weekly_answer_intact(self):
        payload, response = deterministic()
        before = copy.deepcopy(response)
        cfg = SemanticConfig("nosana", "test-key-not-a-real-secret", self.base, "test/model-1")
        started = time.monotonic()
        result = enrich_weekly_response(payload, response, config=cfg, budget_seconds=1.5)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertEqual(result["source"], "fallback")
        self.assertEqual(result["meta"]["llmStatus"], "timeout")
        self.assertEqual(result["plans"], before["plans"])

    def test_no_worker_thread_outlives_the_attempt(self):
        payload, response = deterministic()
        cfg = SemanticConfig("nosana", "test-key-not-a-real-secret", self.base, "test/model-1")
        before = threading.active_count()
        enrich_weekly_response(payload, response, config=cfg, budget_seconds=1.0)
        # The layer runs in its caller's thread and spawns none of its own.
        self.assertLessEqual(threading.active_count(), before + 1)


class ValidateSelectionTests(unittest.TestCase):
    """The validator on its own, so its rules are readable without a pipeline."""

    def setUp(self):
        self.bank = {"plan_a": [{"id": "p1e01", "text": "첫 번째 문장입니다."}],
                     "plan_b": [{"id": "p2e01", "text": "두 번째 문장입니다."}]}

    def test_verified_answer_renders_the_banked_sentence(self):
        verified = validate_selection(
            [{"planId": "plan_a", "fitScore": 1, "reasonEvidenceIds": ["p1e01"]},
             {"planId": "plan_b", "fitScore": 0, "reasonEvidenceIds": ["p2e01"]}],
            self.bank,
        )
        self.assertEqual(verified["plan_a"], (1.0, ["첫 번째 문장입니다."]))
        self.assertEqual(verified["plan_b"], (0.0, ["두 번째 문장입니다."]))

    def test_repeated_evidence_id_renders_once(self):
        bank = {"plan_a": [{"id": "p1e01", "text": "한 문장."}]}
        verified = validate_selection(
            [{"planId": "plan_a", "fitScore": 0.5, "reasonEvidenceIds": ["p1e01", "p1e01"]}], bank
        )
        self.assertEqual(verified["plan_a"][1], ["한 문장."])

    def test_boolean_is_not_a_score(self):
        with self.assertRaises(SemanticError):
            validate_selection(
                [{"planId": "plan_a", "fitScore": True, "reasonEvidenceIds": ["p1e01"]},
                 {"planId": "plan_b", "fitScore": 0.5, "reasonEvidenceIds": ["p2e01"]}],
                self.bank,
            )

    def test_entry_that_is_not_an_object(self):
        with self.assertRaises(SemanticError):
            validate_selection(["plan_a"], self.bank)


if __name__ == "__main__":
    unittest.main()
