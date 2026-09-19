"""User-context parsing and validation: parse_user_context(payload) -> dict."""
from __future__ import annotations

import unittest

from tests.support import PRIMARY_PAYLOAD, payload


def parse(data):
    from harness.models import parse_user_context

    return parse_user_context(data)


class PrimaryInputTests(unittest.TestCase):
    def test_primary_acceptance_input_parses(self):
        ctx = parse(PRIMARY_PAYLOAD)
        self.assertEqual(ctx["start_location"], "서울 강남")
        self.assertEqual(ctx["home_location"], "서울 용산")
        self.assertEqual(ctx["availability"]["start"], "14:00")
        self.assertEqual(ctx["availability"]["end"], "20:00")
        self.assertEqual(ctx["weekly_income_target"], 250000)
        self.assertEqual(ctx["skills"], ["POS 경험 6개월", "보건증"])
        self.assertEqual(ctx["preferred_jobs"], ["의류 행사", "매장 정리"])
        self.assertEqual(ctx["avoid_jobs"], ["설거지", "주방 보조"])
        self.assertEqual(ctx["travel_preferences"], ["퇴근 경로 인근"])

    def test_context_is_a_plain_dictionary(self):
        ctx = parse(PRIMARY_PAYLOAD)
        self.assertIsInstance(ctx, dict)
        self.assertFalse(hasattr(ctx, "to_dict"), "seams use plain dicts, not objects")

    def test_optional_lists_default_to_empty_and_target_may_be_null(self):
        data = {
            "start_location": "서울 강남",
            "home_location": "서울 용산",
            "availability": {"start": "14:00", "end": "20:00"},
        }
        ctx = parse(data)
        for key in ("travel_preferences", "skills", "preferred_jobs", "avoid_jobs"):
            self.assertEqual(ctx[key], [], key)
        self.assertIsNone(ctx.get("weekly_income_target"))

    def test_free_text_list_fields_are_split_not_hallucinated(self):
        ctx = parse(payload(skills="POS 경험 6개월, 보건증", avoid_jobs="설거지\n주방 보조"))
        self.assertEqual(ctx["skills"], ["POS 경험 6개월", "보건증"])
        self.assertEqual(ctx["avoid_jobs"], ["설거지", "주방 보조"])


class ValidationTests(unittest.TestCase):
    def _invalid(self, **overrides):
        with self.assertRaises(ValueError):
            parse(payload(**overrides))

    def test_missing_locations_are_rejected(self):
        data = payload()
        del data["start_location"]
        with self.assertRaises(ValueError):
            parse(data)
        data = payload()
        data["home_location"] = ""
        with self.assertRaises(ValueError):
            parse(data)

    def test_missing_availability_is_rejected(self):
        data = payload()
        del data["availability"]
        with self.assertRaises(ValueError):
            parse(data)

    def test_non_hhmm_times_are_rejected(self):
        self._invalid(availability={"start": "2pm", "end": "20:00"})
        self._invalid(availability={"start": "14:00", "end": "8 PM"})
        self._invalid(availability={"start": "25:00", "end": "26:00"})
        self._invalid(availability={"start": "14:60", "end": "20:00"})
        self._invalid(availability={"start": "2026-09-19T14:00:00+09:00", "end": "20:00"})

    def test_start_must_precede_end_same_day(self):
        self._invalid(availability={"start": "20:00", "end": "14:00"})
        self._invalid(availability={"start": "14:00", "end": "14:00"})

    def test_invalid_income_target_is_rejected(self):
        self._invalid(weekly_income_target="a lot")
        self._invalid(weekly_income_target=-1)
        self._invalid(weekly_income_target=float("inf"))

    def test_error_messages_are_explicit(self):
        with self.assertRaises(ValueError) as caught:
            parse(payload(availability={"start": "20:00", "end": "14:00"}))
        message = str(caught.exception)
        self.assertTrue(message.strip())
        self.assertGreater(len(message), 10, f"message should explain the problem: {message!r}")


if __name__ == "__main__":
    unittest.main()
