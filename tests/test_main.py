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
        self.patches = [
            patch.dict("os.environ", {}, clear=True),
            patch.object(state, "STATE_FILE", str(Path(self.tmp.name) / "state.json")),
            patch.object(main, "load_config", return_value=copy.deepcopy(CONFIG)),
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
            return jobs

        with patch.object(main.fetchers, "fetch_company", return_value=[copy.deepcopy(JOB), second]), \
                patch.object(main.jfit, "devops_fit", side_effect=[{"score": 90}, {"score": 10}]), \
                patch.object(main.score, "score_jobs", side_effect=assign_ai), \
                patch.object(main.notify, "dispatch", return_value={
                    "telegram": {state.key(JOB): True, state.key(second): True},
                }) as dispatch:
            self.assertEqual(main.run(), 0)
        self.assertEqual([job["id"] for job in dispatch.call_args.args[0]], ["second", "new"])


if __name__ == "__main__":
    unittest.main()
