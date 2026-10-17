import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from jobradar import fetchers, main, state


JOB = {"id": "one", "company": "Example", "title": "Senior DevOps Engineer", "location": "Mumbai, India", "url": "https://example.com/job/one"}
CONFIG = {"companies": [{"name": name, "ats": "workday"} for name in ("Example", "Other", "Third", "Fourth")],
          "match": {"role_keywords": ["devops"], "regions_enabled": ["india"], "locations": {"india": ["India"]}}}


class ReviewHistoryTests(unittest.TestCase):
    def test_failed_ai_rejection_expires_and_profile_changes_reassess_without_resending_delivered(self):
        current = {"version": 2, "initialized": True, "seen": {}, "pending": {}}
        state.reject(current, JOB, temporary=True, policy_version="profile-one")
        self.assertFalse(state.is_new(current, JOB, "profile-one"))
        self.assertTrue(state.is_new(current, JOB, "profile-two"))
        self.assertTrue(state.is_new(current, {**JOB, "location": "Dubai"}, "profile-one"))
        current["review"][state.key(JOB)]["next_review_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.assertTrue(state.is_new(current, JOB, "profile-one"))
        state.enqueue(current, JOB, ["email"])
        self.assertNotIn(state.key(JOB), current["review"])
        state.record_delivery(current, {"email": {state.key(JOB): True}})
        self.assertFalse(state.is_new(current, JOB, "new-profile"))


class SourceRecoveryTests(unittest.TestCase):
    def response(self, status=200, content_type="application/json", body=b'{"total":0,"jobPostings":[]}'):
        response = fetchers.requests.Response()
        response.status_code = status
        response._content = body
        response._content_consumed = True
        response.headers["Content-Type"] = content_type
        return response

    def test_transient_post_retries_and_non_json_circuit_stops_other_queries(self):
        with patch.object(fetchers.requests, "Session") as factory, patch.object(fetchers.time, "sleep"):
            client = factory.return_value.__enter__.return_value
            client.post.side_effect = [self.response(503), self.response()]
            self.assertEqual(fetchers._post("https://example.com/jobs", {}), {"total": 0, "jobPostings": []})
            self.assertEqual(client.post.call_count, 2)
        fetchers.FETCH_ERRORS.clear()
        company = {"name": "Example", "ats": "workday", "host": "example.wd5.myworkdayjobs.com", "site": "careers", "queries": ["devops", "sre"]}
        with patch.object(fetchers.requests, "Session") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = self.response(content_type="text/html", body=b'<html>private-challenge-data</html>')
            self.assertEqual(fetchers.fetch_company(company), [])
            self.assertEqual(client.post.call_count, 1)
        self.assertIn("non-JSON", fetchers.FETCH_ERRORS[0])
        self.assertNotIn("private-challenge-data", str(fetchers.FETCH_ERRORS))

    def test_access_denial_is_not_retried(self):
        with patch.object(fetchers.requests, "Session") as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = self.response(403)
            with self.assertRaisesRegex(fetchers.SourceUnavailable, "HTTP 403"):
                fetchers._post("https://example.com/jobs", {})
            self.assertEqual(client.post.call_count, 1)

    def test_large_quota_hint_is_deferred_without_sleeping_early(self):
        response = self.response(429)
        response.headers["Retry-After"] = "3600"
        with patch.object(fetchers.requests, "Session") as factory, patch.object(fetchers.time, "sleep") as sleep:
            factory.return_value.__enter__.return_value.post.return_value = response
            with self.assertRaises(fetchers.SourceUnavailable):
                fetchers._post("https://example.com/jobs", {})
            sleep.assert_not_called()


class MonitorHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"JOBRADAR_REPORT": str(Path(self.tmp) / "report.json")}, clear=True))
        self.enterContext(patch.object(state, "STATE_FILE", str(Path(self.tmp) / "state.json")))
        self.config = copy.deepcopy(CONFIG)
        self.enterContext(patch.object(main, "load_config", return_value=self.config))
        state.save({"version": 2, "initialized": True, "seen": {}, "pending": {}})

    def test_partial_coverage_is_visible_without_masking_a_critical_failure(self):
        def source(company):
            if company["name"] == "Other":
                fetchers.FETCH_ERRORS.append("Other: unavailable")
            return []
        with patch.object(fetchers, "fetch_company", side_effect=source):
            self.assertEqual(main.run(), 0)
            report = json.loads(Path(os.environ["JOBRADAR_REPORT"]).read_text())
            self.assertEqual(report["coverage"], "degraded")
            self.config["health"] = {"critical_companies": ["Other"]}
            self.assertEqual(main.run(), 1)

    def test_all_source_failure_is_hard_failure(self):
        def source(company):
            fetchers.FETCH_ERRORS.append(company["name"] + ": unavailable")
            return []
        with patch.object(fetchers, "fetch_company", side_effect=source):
            self.assertEqual(main.run(), 1)

    def test_preview_cannot_send_or_change_seen_review_and_outbox(self):
        before = Path(state.STATE_FILE).read_bytes()
        with patch.dict(os.environ, {"JOBRADAR_PREVIEW": "true"}), \
                patch.object(fetchers, "fetch_company", return_value=[copy.deepcopy(JOB)]), \
                patch.object(main.describe, "enrich_jd", return_value="Terraform Kubernetes AWS automation"), \
                patch.object(main.notify, "dispatch") as send:
            self.assertEqual(main.run(), 0)
        send.assert_not_called()
        self.assertEqual(Path(state.STATE_FILE).read_bytes(), before)

    def test_focused_preview_scans_only_selected_employer_and_never_sends(self):
        before = Path(state.STATE_FILE).read_bytes()
        with patch.dict(os.environ, {"JOBRADAR_PREVIEW": "true", "JOBRADAR_PREVIEW_COMPANY": " other "}), \
                patch.object(fetchers, "fetch_company", return_value=[]) as fetch, \
                patch.object(main.notify, "dispatch") as send:
            self.assertEqual(main.run(), 0)
        self.assertEqual([call.args[0]["name"] for call in fetch.call_args_list], ["Other"])
        send.assert_not_called()
        self.assertEqual(Path(state.STATE_FILE).read_bytes(), before)

    def test_unknown_focused_preview_fails_before_any_remote_work(self):
        with patch.dict(os.environ, {"JOBRADAR_PREVIEW": "true", "JOBRADAR_PREVIEW_COMPANY": "Unknown"}), \
                patch.object(fetchers, "fetch_company") as fetch:
            with self.assertRaisesRegex(ValueError, "preview_company"):
                main.run()
        fetch.assert_not_called()

    def test_preview_company_cannot_narrow_a_production_scan(self):
        with patch.dict(os.environ, {"JOBRADAR_PREVIEW_COMPANY": "Other"}), \
                patch.object(fetchers, "fetch_company", return_value=[]) as fetch:
            self.assertEqual(main.run(), 0)
        self.assertEqual(fetch.call_count, 4)

    def test_interrupted_assessment_retains_source_report_without_claiming_completion(self):
        with patch.dict(os.environ, {"JOBRADAR_PREVIEW": "true"}), \
                patch.object(fetchers, "fetch_company", return_value=[copy.deepcopy(JOB)]), \
                patch.object(main.describe, "enrich_jd", return_value="Terraform automation"), \
                patch.object(main.score, "score_jobs", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.run()
        report = json.loads(Path(os.environ["JOBRADAR_REPORT"]).read_text())
        self.assertEqual(report["coverage"], "healthy")
        self.assertEqual(report["phase"], "assessment_pending")
        self.assertEqual(report["delivery"], "not_requested")
        self.assertEqual(len(report["sources"]), 4)

    def test_completed_ai_decision_survives_fallback_for_other_roles(self):
        other = {**JOB, "id": "two"}
        def score(jobs, config):
            jobs[0].update(ai_decision="reject", ai_score=0, ai_note="Clearly unrelated duties")
            return jobs[1:]
        with patch.object(fetchers, "fetch_company", return_value=[copy.deepcopy(JOB), other]), \
                patch.object(main.describe, "enrich_jd", return_value="Terraform Kubernetes AWS automation"), \
                patch.object(main.score, "score_jobs", side_effect=score), \
                patch.object(main.notify, "configured_channels", return_value=["email"]), \
                patch.object(main.notify, "dispatch", return_value={"email": {"Example::two": True}}) as send:
            self.assertEqual(main.run(), 0)
        self.assertEqual([job["id"] for job in send.call_args.args[0]], ["two"])
        self.assertEqual(state.load()["review"]["Example::one"]["mode"], "ai")
        self.assertIn("Example::two", state.load()["seen"])
