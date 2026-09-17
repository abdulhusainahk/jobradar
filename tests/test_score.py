import copy
import io
import json
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from jobradar import score


CONFIG = {
    "profile": {"skills": ["Terraform", "Kubernetes"]},
    "match": {"candidate_years": 6, "regions_enabled": ["india", "uae"],
              "locations": {"india": ["Mumbai"], "uae": ["Dubai"]}},
}
JOBS = [
    {"id": "a", "company": "Example", "title": "Senior DevOps Engineer",
     "location": "Mumbai, India", "fit": {"score": 80}, "_jd": "Terraform and AWS"},
    {"id": "b", "company": "Example", "title": "Platform Engineer",
     "location": "Dubai, UAE", "fit": {"score": 60}, "_jd": "Kubernetes automation"},
]


def response(value=90, note="Strong infrastructure fit", status=200, finish="STOP",
             decision="keep", relevance="relevant", experience_fit="within_band",
             location_fit="allowed"):
    result = Mock()
    result.status_code = status
    result.headers = {}
    result.json.return_value = {"candidates": [{
        "finishReason": finish,
        "content": {"parts": [{"text": json.dumps({
            "score": value, "note": note, "decision": decision, "relevance": relevance,
            "experience_fit": experience_fit, "location_fit": location_fit,
        })}]},
    }]}
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError("provider rejected request")
    return result


class GeminiScoringTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.environment = patch.dict("os.environ", {
            "AI_SCORING": "on", "GEMINI_API_KEY": "private-test-key",
            "AI_MIN_SCORE": "70", "AI_MAX_ATTEMPTS": "3", "JOBRADAR_MODEL": "",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.session_patch = patch.object(score.requests, "Session")
        self.session = self.session_patch.start().return_value.__enter__.return_value
        self.addCleanup(self.session_patch.stop)
        self.clock_patch = patch.object(score.time, "monotonic", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.sleep_patch = patch.object(score.time, "sleep", side_effect=self.advance)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.jobs = copy.deepcopy(JOBS)

    def advance(self, seconds):
        self.now += seconds

    def test_transient_transport_failure_recovers_before_scoring_threshold(self):
        self.session.post.side_effect = [requests.Timeout(), response(95), response(40)]
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual([job["id"] for job in kept], ["a"])
        self.assertEqual(kept[0]["ai_score"], 95)
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.jobs[0]["fit"]["score"], 80)

    def test_rate_limit_exhaustion_restores_even_prior_ai_rejections(self):
        limited = response(status=429)
        limited.headers = {"Retry-After": "5"}
        self.session.post.side_effect = [response(20), limited, limited, limited]
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual([job["id"] for job in kept], ["a", "b"])
        self.assertTrue(all(not any(key.startswith("ai_") for key in job) for job in kept))
        self.assertEqual([job["fit"]["score"] for job in kept], [80, 60])
        self.assertEqual(self.session.post.call_count, 4)

    def test_invalid_key_retries_then_stops_ai_for_remaining_jobs(self):
        self.session.post.return_value = response(status=403)
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual(kept, JOBS)
        self.assertEqual(self.session.post.call_count, 3)

    def test_invalid_structured_scores_never_hide_keyword_matches(self):
        # A bool passes isinstance(value, int); a score >100 must not pass either.
        for value in [True, 101, "95"]:
            with self.subTest(value=value):
                self.session.post.reset_mock(side_effect=True)
                self.session.post.return_value = response(value)
                kept = score.score_jobs(copy.deepcopy(JOBS), CONFIG)
                self.assertEqual(kept, JOBS)
                self.assertEqual(self.session.post.call_count, 3)

    def test_blocked_response_can_recover_on_retry(self):
        self.session.post.side_effect = [response(finish="SAFETY"), response(100), response(70)]
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual([job["ai_score"] for job in kept], [100, 70])

    def test_missing_key_keeps_low_keyword_scores_despite_ai_threshold(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual(kept, JOBS)
        self.session.post.assert_not_called()

    def test_provider_exception_cannot_leak_key_or_prompt_to_logs(self):
        self.session.post.side_effect = requests.ConnectionError(
            "private-test-key Candidate secret-profile provider rejected request"
        )
        log = io.StringIO()
        with redirect_stderr(log):
            kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual(kept, JOBS)
        self.assertNotIn("private-test-key", log.getvalue())
        self.assertNotIn("secret-profile", log.getvalue())

    def test_decision_precedence_retains_uncertainty_below_threshold(self):
        self.jobs.append({**copy.deepcopy(JOBS[1]), "id": "c"})
        self.session.post.side_effect = [
            response(95),
            response(99, decision="reject", relevance="irrelevant"),
            response(5, decision="uncertain", experience_fit="uncertain"),
        ]
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual([job["id"] for job in kept], ["a", "c"])
        self.assertEqual(kept[1]["ai_decision"], "uncertain")
        self.assertEqual(self.jobs[1]["ai_decision"], "reject")

    def test_missing_description_cannot_produce_a_definitive_ai_rejection(self):
        self.jobs[0]["_jd"] = ""
        self.session.post.return_value = response(0, decision="reject", relevance="irrelevant")
        kept = score.score_jobs(self.jobs[:1], CONFIG)
        self.assertEqual([job["id"] for job in kept], ["a"])
        self.assertEqual(kept[0]["ai_decision"], "uncertain")

    def test_unresolved_evidence_is_not_treated_as_a_clear_mismatch(self):
        self.session.post.side_effect = [
            response(10, decision="reject", experience_fit="uncertain"),
            response(10, decision="reject", relevance="irrelevant", experience_fit="uncertain"),
        ]
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual([job["id"] for job in kept], ["a"])
        self.assertEqual(kept[0]["ai_decision"], "uncertain")

    def test_invalid_decision_retries_then_returns_no_partial_assessments(self):
        self.session.post.return_value = response(decision="perhaps")
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual(kept, JOBS)
        self.assertEqual(self.session.post.call_count, 3)

    def test_requests_and_retries_obey_configured_pacing(self):
        starts = []
        replies = [response(status=503), response(95), response(90)]

        def post(*args, **kwargs):
            starts.append(self.now)
            self.advance(2)
            return replies.pop(0)

        self.session.post.side_effect = post
        with patch.dict("os.environ", {"AI_REQUESTS_PER_MINUTE": "10"}):
            kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual([job["ai_score"] for job in kept], [95, 90])
        self.assertEqual(starts, [0, 6, 12])

    def test_retryinfo_and_header_waits_are_both_respected(self):
        limited = response(status=429)
        limited.headers = {"Retry-After": "5"}
        limited.json.return_value = {"error": {"details": [{
            "@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12.250s",
        }]}}
        starts = []
        replies = [limited, response(95), response(90)]

        def post(*args, **kwargs):
            starts.append(self.now)
            return replies.pop(0)

        self.session.post.side_effect = post
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual(len(kept), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 12.25)

    def test_http_date_retry_after_is_not_shortened(self):
        limited = response(status=429)
        limited.headers = {"Retry-After": "Thu, 01 Jan 2026 00:00:45 GMT"}
        starts = []
        replies = [limited, response(95)]

        def post(*args, **kwargs):
            starts.append(self.now)
            return replies.pop(0)

        self.session.post.side_effect = post
        with patch.object(score, "datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            kept = score.score_jobs(self.jobs[:1], CONFIG)
        self.assertEqual(kept[0]["ai_score"], 95)
        self.assertGreaterEqual(starts[1], 45)

    def test_malformed_retry_metadata_uses_a_quota_window_instead_of_crashing(self):
        limited = response(status=429)
        limited.headers = {"Retry-After": "NaN"}
        limited.json.return_value = {"error": {"details": 7}}
        starts = []
        replies = [limited, response(95)]

        def post(*args, **kwargs):
            starts.append(self.now)
            return replies.pop(0)

        self.session.post.side_effect = post
        kept = score.score_jobs(self.jobs[:1], CONFIG)
        self.assertEqual(kept[0]["ai_score"], 95)
        self.assertGreaterEqual(starts[1], 60)

    def test_daily_or_disabled_quota_falls_back_without_early_retries(self):
        for violation in [
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "100"},
            {"quotaId": "GenerateRequestsPerMinutePerProject", "quotaValue": "0"},
        ]:
            with self.subTest(violation=violation):
                limited = response(status=429)
                limited.json.return_value = {"error": {"details": [{
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [violation],
                }]}}
                self.session.post.reset_mock(side_effect=True)
                self.session.post.return_value = limited
                kept = score.score_jobs(self.jobs, CONFIG)
                self.assertEqual(kept, JOBS)
                self.assertEqual(self.session.post.call_count, 1)

    def test_excessive_cooldown_is_deferred_not_capped_into_an_early_retry(self):
        limited = response(status=429)
        limited.json.return_value = {"error": {"details": [{
            "@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s",
        }]}}
        self.session.post.return_value = limited
        kept = score.score_jobs(self.jobs, CONFIG)
        self.assertEqual(kept, JOBS)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.now, 0)


if __name__ == "__main__":
    unittest.main()
