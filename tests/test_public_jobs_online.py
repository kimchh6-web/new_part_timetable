"""Opt-in live smoke test for :mod:`harness.sources.public_jobs`.

Skipped unless **both** of these are set::

    PUBLIC_JOBS_ONLINE_SMOKE=1
    PUBLIC_JOBS_AUTHORIZATION_REFERENCE=<where the provider's consent is recorded>

The second one is the point: both providers are gated in
:mod:`harness.sources.public_jobs.policy` because their member terms condition
use and hand-off of information obtained from the service on the operator's
prior consent. This test does not open that gate on its own - whoever runs it
has to name the record that authorizes it, and that string is echoed into the
output so a run can be traced back to its permission.

The test **distinguishes blocked from pass**. A refusal by the provider
(``blocked_by_provider``, ``blocked_or_error_page``, ``robots_disallowed``,
``robots_unavailable``, ``redirect_not_followed``, a network error) is reported
as a skip with the reason attached: that is information about the provider, not
a defect in this package. Only a malformed envelope fails.
"""

from __future__ import annotations

import json
import os
import unittest

from harness.sources.public_jobs import collect_public_jobs, policy_report

#: Reasons that mean "the provider did not serve us", not "our code is wrong".
BLOCKED_REASONS = frozenset(
    {
        "blocked_by_provider",
        "blocked_or_error_page",
        "robots_disallowed",
        "robots_unavailable",
        "redirect_not_followed",
        "redirect_off_allowlist",
        "network_error",
        "timeout",
        "http_error",
        "response_too_large",
        "no_job_posting_found",
        "posting_closed",
    }
)

ENABLED = os.environ.get("PUBLIC_JOBS_ONLINE_SMOKE") == "1"
AUTHORIZATION_REFERENCE = os.environ.get("PUBLIC_JOBS_AUTHORIZATION_REFERENCE", "")
#: One posting URL per provider, comma separated. Explicit seeds only.
SEED_URLS = [
    url.strip() for url in os.environ.get("PUBLIC_JOBS_SMOKE_URLS", "").split(",") if url.strip()
]


@unittest.skipUnless(ENABLED, "set PUBLIC_JOBS_ONLINE_SMOKE=1 to run the live smoke test")
@unittest.skipUnless(
    AUTHORIZATION_REFERENCE,
    "set PUBLIC_JOBS_AUTHORIZATION_REFERENCE to the record that authorizes live collection",
)
@unittest.skipUnless(SEED_URLS, "set PUBLIC_JOBS_SMOKE_URLS to one posting URL per provider")
class OnlineSmokeTest(unittest.TestCase):
    def test_collect_reports_either_rows_or_a_named_blocker(self):
        authorizations = [
            {
                "provider": policy["provider"],
                "granted_by": os.environ.get(
                    "PUBLIC_JOBS_AUTHORIZATION_BY", "operator of this checkout"
                ),
                "reference": AUTHORIZATION_REFERENCE,
                "scope": "live_smoke_test",
            }
            for policy in [entry for entry in policy_report()]
        ]
        envelope = collect_public_jobs(SEED_URLS[:2], authorizations=authorizations)
        meta = envelope["meta"]

        self.assertEqual("public_web", meta["job_source"])
        self.assertEqual("live", meta["data_mode"])
        self.assertEqual(len(envelope["jobs"]), meta["collected"])
        self.assertLessEqual(meta["attempted"], 5)

        if meta["collected"] == 0:
            reasons = sorted({error["reason"] for error in meta["errors"]})
            self.assertTrue(reasons, "zero rows must come with a stated reason")
            unexpected = [reason for reason in reasons if reason not in BLOCKED_REASONS]
            self.assertEqual(
                [], unexpected, "unexpected failure reasons: %s" % (unexpected,)
            )
            raise unittest.SkipTest(
                "provider did not serve this reader; blocked, not passed: %s"
                % (json.dumps(meta["errors"], ensure_ascii=False)[:800],)
            )

        for job in envelope["jobs"]:
            self.assertEqual("live", job["provenance"]["data_mode"])
            self.assertIn(job["provenance"]["provider"], ("alba", "albamon"))
            self.assertTrue(job["provenance"]["fetched_at"])
            self.assertTrue(job["sourceUrl"])
            self.assertIn(job["status"], ("recruiting", "closed", "unknown", "paused"))
            self.assertIsInstance(job["missing_fields"], list)
            self.assertIsInstance(job["scheduling_eligible"], bool)
            if not job["shifts"]:
                self.assertFalse(
                    job["scheduling_eligible"],
                    "a posting with no published shifts is not schedulable",
                )


if __name__ == "__main__":
    unittest.main()
