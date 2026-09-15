import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobradar import main, state


CONFIG = {
    "companies": [{"name": "Example", "ats": "greenhouse", "token": "example"}],
    "match": {"role_keywords": ["devops"], "regions_enabled": ["india"],
              "locations": {"india": ["Mumbai", "India"]},
              "candidate_years": 6, "max_required_years": 11},
}
JOB = {"id": "new", "company": "Example", "title": "Senior DevOps Engineer",
       "location": "Mumbai, India", "url": "https://example.com/jobs/new", "posted_ts": 1.0}
JD = "6+ years of engineering experience. Terraform Kubernetes Helm AWS CI/CD automation."


class MonitorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = copy.deepcopy(CONFIG)
        self.patches = [
            patch.dict("os.environ", {}, clear=True),
            patch.object(state, "STATE_FILE", str(Path(self.tmp.name) / "state.json")),
            patch.object(main, "load_config", return_value=self.config),
            patch.object(main.notify, "configured_channels", return_value=["telegram"]),
            patch.object(main.describe, "enrich_jd", return_value=JD),
        ]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        main.fetchers.FETCH_ERRORS.clear()

    def test_failed_alert_retries_even_after_role_disappears_from_feed(self):
        state.save({"version": 2, "seen": {"Example::old": "2026-09-01"},
                    "pending": {}, "initialized": True})
        with patch.object(main.fetchers, "fetch_company", side_effect=[[copy.deepcopy(JOB)], []]), \
                patch.object(main.notify, "dispatch", side_effect=[{}, {"telegram": {state.key(JOB): True}}]) as dispatch:
            self.assertEqual(main.run(), 1)
            saved = state.load()
            self.assertNotIn(state.key(JOB), saved["seen"])
            self.assertIn(state.key(JOB), saved["pending"])
            self.assertEqual(main.run(), 0)
        self.assertEqual([call.args[0][0]["id"] for call in dispatch.call_args_list], ["new", "new"])
        self.assertIn(state.key(JOB), state.load()["seen"])
        self.assertEqual(state.load()["pending"], {})

    def test_empty_healthy_baseline_does_not_suppress_first_future_match(self):
        with patch.object(main.fetchers, "fetch_company", side_effect=[[], [copy.deepcopy(JOB)]]), \
                patch.object(main.notify, "dispatch", return_value={"telegram": {state.key(JOB): True}}) as dispatch:
            self.assertEqual(main.run(), 0)
            self.assertTrue(state.load()["initialized"])
            dispatch.assert_not_called()
            self.assertEqual(main.run(), 0)
            self.assertEqual(dispatch.call_args.args[0][0]["id"], "new")

    def test_ai_assessments_rank_above_opposing_heuristic_scores(self):
        state.save({"version": 2, "seen": {}, "pending": {}, "initialized": True})
        second = dict(JOB, id="second", url="https://example.com/jobs/second")

        def assign_ai(jobs, config):
            for job in jobs:
                job["ai_score"] = 20 if job["id"] == "new" else 95
                job["ai_note"] = "Assessed fit"
                job["ai_decision"] = "keep"
            return jobs

        with patch.object(main.fetchers, "fetch_company", return_value=[copy.deepcopy(JOB), second]), \
                patch.object(main.jfit, "devops_fit", side_effect=[{"score": 90}, {"score": 10}]), \
                patch.object(main.score, "score_jobs", side_effect=assign_ai), \
                patch.object(main.notify, "dispatch", return_value={
                    "telegram": {state.key(JOB): True, state.key(second): True},
                }) as dispatch:
            self.assertEqual(main.run(), 0)
        self.assertEqual([job["id"] for job in dispatch.call_args.args[0]], ["second", "new"])

    def test_ai_can_rescue_a_role_before_monitoring_heuristics_drop_it(self):
        self.config["match"].update(drop_monitoring_below=45, drop_out_of_band=True)
        state.save({"version": 2, "seen": {}, "pending": {}, "initialized": True})
        jd = "6+ years engineering experience. On-call monitoring; build self-healing recovery services."

        def assess(jobs, config):
            for job in jobs:
                job.update(ai_decision="keep", ai_score=85, ai_note="Owns engineering, not just monitoring.")
            return jobs

        with patch.object(main.fetchers, "fetch_company", return_value=[copy.deepcopy(JOB)]), \
                patch.object(main.describe, "enrich_jd", return_value=jd), \
                patch.object(main.score, "score_jobs", side_effect=assess), \
                patch.object(main.notify, "dispatch", return_value={"telegram": {state.key(JOB): True}}) as dispatch:
            self.assertEqual(main.run(), 0)
        delivered = dispatch.call_args.args[0]
        self.assertEqual([job["id"] for job in delivered], ["new"])
        self.assertTrue(delivered[0]["fit"]["monitoring_only"])
        self.assertIn(state.key(JOB), state.load()["seen"])

    def test_uncertain_ai_retains_a_role_the_experience_parser_would_drop(self):
        self.config["match"]["drop_out_of_band"] = True
        state.save({"version": 2, "seen": {}, "pending": {}, "initialized": True})
        jd = "2-3 years of overall engineering experience."

        def assess(jobs, config):
            for job in jobs:
                job.update(ai_decision="uncertain", ai_score=5,
                           ai_note="Senior title and junior experience band conflict.",
                           ai_experience_fit="uncertain")
            return jobs

        with patch.object(main.fetchers, "fetch_company", return_value=[copy.deepcopy(JOB)]), \
                patch.object(main.describe, "enrich_jd", return_value=jd), \
                patch.object(main.score, "score_jobs", side_effect=assess), \
                patch.object(main.notify, "dispatch", return_value={"telegram": {state.key(JOB): True}}) as dispatch:
            self.assertEqual(main.run(), 0)
        self.assertEqual(dispatch.call_args.args[0][0]["ai_decision"], "uncertain")
        self.assertIn(state.key(JOB), state.load()["seen"])

    def test_failed_ai_uses_existing_heuristic_rejections_and_ranking(self):
        self.config["match"].update(drop_monitoring_below=45, drop_out_of_band=True)
        state.save({"version": 2, "seen": {}, "pending": {}, "initialized": True})
        jobs = [dict(JOB, id=name) for name in ("monitoring", "junior", "good")]
        descriptions = {"monitoring": "Monitoring dashboards and on-call incidents.",
                        "junior": "2-3 years of overall engineering experience.", "good": JD}
        with patch.dict("os.environ", {"AI_SCORING": "on", "GEMINI_API_KEY": "private-test-key"}), \
                patch.object(main.fetchers, "fetch_company", return_value=jobs), \
                patch.object(main.describe, "enrich_jd", side_effect=lambda job: descriptions[job["id"]]), \
                patch.object(main.score.requests, "Session") as client, \
                patch.object(main.score.time, "sleep"), \
                patch.object(main.notify, "dispatch", return_value={"telegram": {"Example::good": True}}) as dispatch:
            client.return_value.__enter__.return_value.post.side_effect = main.score.requests.Timeout()
            self.assertEqual(main.run(), 0)
        delivered = dispatch.call_args.args[0]
        self.assertEqual([job["id"] for job in delivered], ["good"])
        self.assertFalse(any(key.startswith("ai_") for key in delivered[0]))
        self.assertEqual(set(state.load()["seen"]), {state.key(job) for job in jobs})

    def test_hard_exclusions_do_not_reach_description_or_ai_calls(self):
        self.config["match"].update(exclude_companies=["Excluded"], exclude_keywords=["intern"])
        state.save({"version": 2, "seen": {}, "pending": {}, "initialized": True})
        jobs = [dict(JOB, id="employer", company="Excluded"),
                dict(JOB, id="intern", title="DevOps Intern"),
                dict(JOB, id="country", country_code="US")]
        with patch.object(main.fetchers, "fetch_company", return_value=jobs), \
                patch.object(main.describe, "enrich_jd", side_effect=AssertionError("Excluded job enriched")), \
                patch.object(main.notify, "dispatch", return_value={}) as dispatch:
            self.assertEqual(main.run(), 0)
        self.assertEqual(dispatch.call_args.args[0], [])
        self.assertEqual(state.load()["seen"], {})


if __name__ == "__main__":
    unittest.main()
