import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from jobradar import experience, fetchers, filter as matching
from jobradar.describe import enrich_jd


def params(url):
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def amazon_job(jid):
    return {"id_icims": jid, "title": "DevOps Engineer", "job_path": f"/jobs/{jid}",
            "normalized_location": "Bengaluru, India"}

def http_response(data, status=200):
    result = fetchers.requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    result._content_consumed = True
    return result



class FetcherTests(unittest.TestCase):
    def setUp(self):
        fetchers.FETCH_ERRORS.clear()
        self.now = 0.0
        clock = patch.object(fetchers.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        sleeper = patch.object(fetchers.time, "sleep", side_effect=self.advance)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def advance(self, seconds):
        self.now += seconds

    def tearDown(self):
        fetchers.FETCH_ERRORS.clear()

    def test_amazon_query_reaches_later_pages_and_merges_overlap(self):
        def api(url):
            query = params(url)
            if query.get("base_query") != ["devops"]:
                return {"hits": 1, "jobs": [{**amazon_job("generic"), "title": "Warehouse Associate"}]}
            offset = int(query["offset"][0])
            return {"hits": 4, "jobs": {
                0: [amazon_job("1"), amazon_job("2")],
                2: [amazon_job("2"), amazon_job("3")],
            }[offset]}

        with patch.object(fetchers, "_get", side_effect=api):
            jobs = fetchers.amazon({"name": "Amazon", "queries": ["devops"]})
        self.assertEqual([j["id"] for j in jobs], ["1", "2", "3"])
        self.assertFalse(fetchers.FETCH_ERRORS)

    def test_reordered_repeated_page_stops_and_reports_incomplete_source(self):
        pages = [{"hits": 20, "jobs": [amazon_job("1"), amazon_job("2")]},
                 {"hits": 20, "jobs": [amazon_job("2"), amazon_job("1")]}]
        with patch.object(fetchers, "_get", side_effect=pages) as api:
            jobs = fetchers.fetch_company({"name": "Amazon", "ats": "amazon", "queries": ["devops"]})
        self.assertEqual({j["id"] for j in jobs}, {"1", "2"})
        self.assertEqual(api.call_count, 2)
        self.assertIn("Amazon", " ".join(fetchers.FETCH_ERRORS))

    def test_page_failure_preserves_results_and_other_queries_continue(self):
        def api(url):
            query = params(url)
            term, offset = query["base_query"][0], int(query["offset"][0])
            if term == "devops" and offset:
                raise RuntimeError("source unavailable")
            return {"hits": 3 if term == "devops" else 1,
                    "jobs": [amazon_job("1" if term == "devops" else "2")]}

        with patch.object(fetchers, "_get", side_effect=api):
            jobs = fetchers.fetch_company({"name": "Amazon", "ats": "amazon",
                                          "queries": ["devops", "cloud engineer"]})
        self.assertEqual({j["id"] for j in jobs}, {"1", "2"})
        self.assertIn("source unavailable", " ".join(fetchers.FETCH_ERRORS))

    def test_pcsx_query_overlap_does_not_hide_later_pages_or_locations(self):
        def job(jid):
            return {"id": jid, "name": "Platform Engineer", "positionUrl": f"/careers/job/{jid}",
                    "locations": ["New York", "Seattle", "Bengaluru, India", "London"]}

        def api(url, **kwargs):
            query = params(url)
            offset = int(query["start"][0])
            ids = ["shared"] if offset == 0 else [query["query"][0]]
            return {"status": 200, "data": {"count": 2, "positions": [job(j) for j in ids]}}

        with patch.object(fetchers, "_get", side_effect=api):
            jobs = fetchers.pcsx({"name": "Example", "host": "example.eightfold.ai",
                                  "domain": "example.com", "queries": ["devops", "platform"]})
        self.assertEqual({j["id"] for j in jobs}, {"shared", "devops", "platform"})
        self.assertEqual(jobs[0]["location"], "New York; Seattle; Bengaluru, India; London")
        self.assertEqual(jobs[0]["url"], "https://example.eightfold.ai/careers/job/shared")
        self.assertFalse(fetchers.FETCH_ERRORS)

    def test_missing_total_does_not_treat_short_page_as_end(self):
        responses = [{"data": {"positions": [{"id": "1", "name": "DevOps"}]}},
                     {"data": {"positions": [{"id": "2", "name": "SRE"}]}},
                     {"data": {"positions": []}}]
        with patch.object(fetchers, "_get", side_effect=responses):
            jobs = fetchers.pcsx({"name": "Example", "host": "example.eightfold.ai",
                                  "domain": "example.com", "queries": ["devops"]})
        self.assertEqual([j["id"] for j in jobs], ["1", "2"])
        self.assertFalse(fetchers.FETCH_ERRORS)

    def test_workday_resolves_multiple_locations_before_returning_jobs(self):
        company = {"name": "Example", "ats": "workday", "host": "example.wd5.myworkdayjobs.com",
                   "site": "Careers", "queries": ["devops"]}

        def search(url, body):
            if body["offset"] == 0:
                return {"total": 2, "jobPostings": [{"title": "DevOps", "externalPath": "/job/One",
                        "locationsText": "3 Locations", "bulletFields": ["R1"]}]}
            return {"total": 2, "jobPostings": [{"title": "SRE", "externalPath": "/job/Two",
                    "locationsText": "Mumbai, India", "bulletFields": ["R2"]}]}

        detail = {"jobPostingInfo": {"location": "Seattle", "additionalLocations": ["London", "India, Pune"],
                  "jobDescription": "<p>Build infrastructure.</p><p>Automate releases.</p>",
                  "externalUrl": "https://example.wd5.myworkdayjobs.com/Careers/job/One"}}
        with patch.object(fetchers, "_post", side_effect=search), patch.object(fetchers, "_get", return_value=detail):
            jobs = fetchers.workday(company)
        self.assertEqual([j["id"] for j in jobs], ["R1", "R2"])
        self.assertEqual(jobs[0]["location"], "Seattle; London; India, Pune")
        self.assertEqual(enrich_jd(jobs[0]), "Build infrastructure. Automate releases.")
        self.assertEqual(jobs[0]["url"], detail["jobPostingInfo"]["externalUrl"])

    def test_workday_location_failure_keeps_role_and_surfaces_health(self):
        company = {"name": "Example", "ats": "workday", "host": "example.wd5.myworkdayjobs.com",
                   "site": "Careers", "queries": ["devops"]}
        search = {"total": 1, "jobPostings": [{"externalPath": "/job/One", "title": "DevOps",
                                               "locationsText": "2 Locations", "bulletFields": ["R1"]}]}
        with patch.object(fetchers, "_post", return_value=search), patch.object(fetchers, "_get", side_effect=RuntimeError("detail unavailable")):
            jobs = fetchers.fetch_company(company)
        self.assertEqual(jobs[0]["id"], "R1")
        self.assertEqual(jobs[0]["location"], "2 Locations")
        self.assertIn("detail unavailable", " ".join(fetchers.FETCH_ERRORS))

    def test_workday_bad_identity_does_not_drop_valid_peers_or_later_pages(self):
        company = {"name": "NVIDIA", "ats": "workday", "host": "nvidia.wd5.myworkdayjobs.com",
                   "site": "NVIDIAExternalCareerSite", "queries": ["devops"]}
        offsets = []

        def search(url, body):
            offsets.append(body["offset"])
            rows = {
                0: [{"title": "Missing path"}, {"externalPath": "/job/One", "title": "DevOps",
                     "locationsText": "India", "bulletFields": ["R1"]}, {"externalPath": "", "title": "Empty path"}],
                3: [{"externalPath": "/job/Two", "title": "SRE", "locationsText": "India",
                     "bulletFields": ["R2"]}],
            }
            return {"total": 4, "jobPostings": rows[body["offset"]]}

        with patch.object(fetchers, "_post", side_effect=search):
            jobs = fetchers.fetch_company(company)
        self.assertEqual([job["id"] for job in jobs], ["R1", "R2"])
        self.assertEqual(offsets, [0, 3])
        self.assertTrue(fetchers.FETCH_ERRORS)

    def test_workday_all_bad_page_still_reaches_the_next_raw_offset(self):
        company = {"name": "Example", "ats": "workday", "host": "example.wd5.myworkdayjobs.com",
                   "site": "Careers", "queries": ["devops"]}

        def search(url, body):
            rows = {0: [None, {}],
                    2: [{"externalPath": "/job/Valid", "title": "SRE", "bulletFields": ["valid"]}]}
            return {"total": 3, "jobPostings": rows[body["offset"]]}

        with patch.object(fetchers, "_post", side_effect=search):
            jobs = fetchers.fetch_company(company)
        self.assertEqual([job["id"] for job in jobs], ["valid"])
        self.assertTrue(fetchers.FETCH_ERRORS)

    def test_repeated_malformed_page_does_not_loop_forever(self):
        company = {"name": "Example", "ats": "workday", "host": "example.wd5.myworkdayjobs.com",
                   "site": "Careers", "queries": ["devops"]}
        with patch.object(fetchers, "_post", return_value={"total": 100, "jobPostings": [{}]}) as api:
            jobs = fetchers.fetch_company(company)
        self.assertEqual(jobs, [])
        self.assertEqual(api.call_count, 2)
        self.assertTrue(fetchers.FETCH_ERRORS)

    def test_bad_workday_field_does_not_discard_other_jobs(self):
        company = {"name": "Example", "ats": "workday", "host": "example.wd5.myworkdayjobs.com",
                   "site": "Careers", "queries": ["devops"]}
        rows = [{"externalPath": "/job/Bad", "title": ["not text"]},
                {"externalPath": "/job/Good", "title": "SRE", "bulletFields": ["good"]}]
        with patch.object(fetchers, "_post", return_value={"total": 2, "jobPostings": rows}):
            jobs = fetchers.fetch_company(company)
        self.assertEqual([job["id"] for job in jobs], ["good"])
        self.assertTrue(fetchers.FETCH_ERRORS)

    def test_pcsx_denial_retains_results_and_stops_remaining_searches(self):
        company = {"name": "Citi", "ats": "pcsx", "host": "citi.eightfold.ai", "domain": "citi.com",
                   "queries": ["devops", "platform"], "locations": ["India", "Europe"]}
        first = http_response({"data": {"count": 2, "positions": [
            {"id": "saved", "name": "SRE", "locations": ["India"]},
        ]}})
        denied = http_response({"error": "Access denied"}, status=403)
        with patch.object(fetchers.requests, "Session") as client:
            transport = client.return_value.__enter__.return_value.get
            transport.side_effect = [first, denied]
            jobs = fetchers.fetch_company(company)
            self.assertEqual(transport.call_count, 2)
        self.assertEqual([job["id"] for job in jobs], ["saved"])
        self.assertTrue(fetchers.FETCH_ERRORS)

    def test_pcsx_json_access_denial_does_not_trigger_other_queries(self):
        company = {"name": "PayPal", "ats": "pcsx", "host": "paypal.eightfold.ai",
                   "domain": "paypal.com", "queries": ["devops", "platform"]}
        with patch.object(fetchers.requests, "Session") as client:
            transport = client.return_value.__enter__.return_value.get
            transport.return_value = http_response({"status": 403, "error": "Access denied"})
            jobs = fetchers.fetch_company(company)
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(jobs, [])
        self.assertTrue(fetchers.FETCH_ERRORS)

    def test_pcsx_paces_requests_across_queries_in_one_session(self):
        company = {"name": "Example", "ats": "pcsx", "host": "example.eightfold.ai",
                   "domain": "example.com", "queries": ["devops", "platform"], "requests_per_minute": 30}
        starts = []

        def get(url, **kwargs):
            starts.append(self.now)
            query = params(url)["query"][0]
            return http_response({"data": {"count": 1, "positions": [
                {"id": query, "name": query, "locations": ["India"]},
            ]}})

        with patch.object(fetchers.requests, "Session") as client:
            client.return_value.__enter__.return_value.get.side_effect = get
            jobs = fetchers.fetch_company(company)
        self.assertEqual({job["id"] for job in jobs}, {"devops", "platform"})
        self.assertEqual(starts, [0, 2])

    def test_pcsx_retries_share_the_same_request_pacing(self):
        company = {"name": "Example", "ats": "pcsx", "host": "example.eightfold.ai",
                   "domain": "example.com", "queries": ["devops"], "requests_per_minute": 1}
        limited = http_response({}, status=429)
        limited.headers["Retry-After"] = "1"
        replies = [limited, http_response({"data": {"count": 2, "positions": [{"id": "one"}]}}),
                   http_response({"data": {"count": 2, "positions": [{"id": "two"}]}})]
        starts = []

        def get(url, **kwargs):
            starts.append(self.now)
            return replies.pop(0)

        with patch.object(fetchers.requests, "Session") as client:
            client.return_value.__enter__.return_value.get.side_effect = get
            jobs = fetchers.fetch_company(company)
        self.assertEqual([job["id"] for job in jobs], ["one", "two"])
        self.assertEqual(starts, [0, 60, 120])

    def test_oracle_uses_finder_offset_and_preserves_all_secondary_locations(self):
        def api(url):
            finder = params(url)["finder"][0]
            fields = dict(field.split("=", 1) for field in finder.split(";", 1)[1].split(","))
            offset = int(fields["offset"])
            return {"items": [{"TotalJobsCount": 2, "requisitionList": [{"Id": str(offset + 1),
                    "Title": "Infrastructure Engineer", "PrimaryLocation": "New York",
                    "secondaryLocations": [{"Name": l} for l in ["Seattle", "Chicago", "Mumbai, India"]]}]}]}

        with patch.object(fetchers, "_get", side_effect=api):
            jobs = fetchers.oracle({"name": "Bank", "host": "bank.fa.oraclecloud.com", "site": "CX_1001"})
        self.assertEqual([j["id"] for j in jobs], ["1", "2"])
        self.assertEqual(jobs[1]["location"], "New York; Seattle; Chicago; Mumbai, India")
        self.assertTrue(jobs[1]["url"].endswith("/sites/CX_1001/job/2"))

    def test_oracle_uses_page_window_when_hidden_rows_reduce_returned_count(self):
        def api(url):
            finder = params(url)["finder"][0]
            fields = dict(field.split("=", 1) for field in finder.split(";", 1)[1].split(","))
            offset = int(fields["offset"])
            job_id = "visible-a" if offset < 2 else "visible-b"
            return {"items": [{"Offset": offset, "Limit": 2, "TotalJobsCount": 4,
                               "requisitionList": [{"Id": job_id, "Title": "DevOps Engineer",
                                                    "PrimaryLocation": "Mumbai, India"}]}]}

        with patch.object(fetchers, "_get", side_effect=api):
            jobs = fetchers.oracle({"name": "Bank", "host": "bank.fa.oraclecloud.com"})
        self.assertEqual([job["id"] for job in jobs], ["visible-a", "visible-b"])
        self.assertEqual(fetchers.FETCH_ERRORS, [])

    def test_source_rate_limit_recovers_without_losing_jobs(self):
        limited = fetchers.requests.Response()
        limited.status_code = 429
        limited.headers["Retry-After"] = "1"
        limited._content = b"{}"
        limited._content_consumed = True
        success = fetchers.requests.Response()
        success.status_code = 200
        success._content = b'{"jobs":[{"id":"recovered","title":"DevOps Engineer"}]}'
        with patch.object(fetchers.requests, "get", side_effect=[limited, success]), \
                patch.object(fetchers.time, "sleep"):
            jobs = fetchers.fetch_company({"name": "Example", "ats": "greenhouse", "token": "example"})
        self.assertEqual([job["id"] for job in jobs], ["recovered"])
        self.assertEqual(fetchers.FETCH_ERRORS, [])

    def test_lever_keeps_secondary_location(self):
        data = [{"id": "z1", "text": "SRE", "categories": {"location": "US",
                 "allLocations": ["US", "Bengaluru, India", "London"]},
                 "hostedUrl": "https://jobs.lever.co/zeta/z1"}]
        with patch.object(fetchers, "_get", return_value=data):
            job = fetchers.lever({"name": "Zeta", "token": "zeta"})[0]
        self.assertEqual(job["location"], "US; Bengaluru, India; London")
        self.assertEqual(job["company"], "Zeta")
        self.assertEqual(job["id"], "z1")

    def test_ashby_url_identity_and_secondary_locations(self):
        data = {"jobs": [{"title": "SRE", "location": "US", "isRemote": True,
                "secondaryLocations": [{"location": "Bengaluru, India"}, {"location": "London"}],
                "jobUrl": "https://jobs.ashbyhq.com/example/role-123"}]}
        with patch.object(fetchers, "_get", return_value=data):
            job = fetchers.ashby({"name": "Example", "token": "example"})[0]
        self.assertEqual(job["id"], "role-123")
        self.assertEqual(job["location"], "US; Bengaluru, India; London (Remote)")

    def test_failed_source_and_successful_empty_are_distinct(self):
        company = {"name": "Example", "ats": "greenhouse", "token": "example"}
        with patch.object(fetchers, "_get", side_effect=RuntimeError("HTTP 404")), patch.object(fetchers, "_log") as log:
            self.assertEqual(fetchers.fetch_company(company), [])
        self.assertIn("Example", " ".join(fetchers.FETCH_ERRORS))
        previous_errors = list(fetchers.FETCH_ERRORS)
        self.assertFalse(any("[empty]" in call.args[0] for call in log.call_args_list))
        with patch.object(fetchers, "_get", return_value={"jobs": []}), patch.object(fetchers, "_log") as log:
            self.assertEqual(fetchers.fetch_company(company), [])
        self.assertEqual(fetchers.FETCH_ERRORS, previous_errors)
        self.assertTrue(any("[empty]" in call.args[0] for call in log.call_args_list))

    def test_smartrecruiters_paginates_migrated_board(self):
        def api(url):
            offset = int(params(url)["offset"][0])
            return {"totalFound": 2, "content": [{"id": str(offset + 1), "name": "DevOps",
                    "location": {"fullLocation": "Chennai, India"}}]}

        with patch.object(fetchers, "_get", side_effect=api):
            jobs = fetchers.smartrecruiters({"name": "Freshworks", "token": "Freshworks"})
        self.assertEqual([j["id"] for j in jobs], ["1", "2"])
        self.assertEqual(jobs[1]["location"], "Chennai, India")

    def test_lever_qualification_lists_affect_experience_filtering(self):
        row = {"id": "requirements", "descriptionPlain": "About our company.",
               "lists": [{"text": "Qualifications",
                          "content": "<li>12+ years of overall engineering experience required.</li>"}]}
        with patch.object(fetchers, "_get", return_value=[row]):
            job = fetchers.lever({"name": "Example", "token": "example"})[0]
        self.assertTrue(experience.assess(enrich_jd(job), 6, 11)["drop"])

    def test_provider_country_disambiguates_city_and_india_group(self):
        company = {"name": "Example", "token": "example"}
        rows = [
            {"id": "canada", "name": "DevOps Engineer",
             "location": {"city": "London", "country": "CA"}},
            {"id": "india", "name": "DevOps Engineer",
             "location": {"city": "Vadodara", "country": "IN"}},
        ]
        config = {"role_keywords": ["devops"], "regions_enabled": ["india", "europe"],
                  "locations": {"india": ["India"], "europe": ["London"]}}
        with patch.object(fetchers, "_get", return_value={"content": rows, "totalFound": 2}):
            canada, india = fetchers.smartrecruiters(company)
        self.assertFalse(matching.passes(canada, config))
        self.assertTrue(matching.passes(india, config))
        self.assertTrue(matching.location_is_india(
            india["location"], config, india["country_code"]))

    def test_late_overall_requirement_survives_description_enrichment(self):
        job = {"id": "late", "_c": {"ats": "greenhouse", "token": "example"}}
        content = "<p>" + "Company background. " * 500 + "</p>"
        content += "<p>12+ years of overall engineering experience required.</p>"
        with patch("jobradar.describe._get", return_value={"content": content}):
            assessment = experience.assess(enrich_jd(job), 6, 11)
        self.assertTrue(assessment["drop"])

    def test_turbohire_fintech_identity_locations_and_full_detail(self):
        company = {"name": "Navi", "ats": "turbohire", "token": "navi"}
        row = {"JobId": "fintech-1", "JobIdObfuscated": "public-id", "JobTitle": "Platform Engineer",
               "Location": '[{"Address":"Bengaluru, India"},{"Address":"Mumbai, India"}]',
               "PublishedDate": "2026-09-10T09:41:41Z", "JobDescV2": "Truncated listing preview"}
        with patch.object(fetchers, "_get", side_effect=[{"access_token": "anonymous"}, {"OrgID": "fintech-org"}]), \
             patch.object(fetchers, "_post", return_value={"Total": 1, "Result": [row]}):
            job = fetchers.turbohire(company)[0]
        self.assertEqual(job["id"], "fintech-1")
        self.assertEqual(job["company"], "Navi")
        self.assertEqual(job["location"], "Bengaluru, India; Mumbai, India")
        self.assertEqual(job["url"], "https://navi.turbohire.co/job/publicjobs/public-id")
        job["_c"] = company
        with patch("jobradar.describe._turbohire_headers", return_value={}), \
             patch("jobradar.describe._get", return_value={
                 "JobDescriptionV2": "<p>Build cloud infrastructure.</p>",
                 "RolesAndResponsibilitiesV2": "<p>Automate releases.</p>",
                 "EligibilityV2": "<p>12+ years of overall engineering experience required.</p>"}):
            self.assertTrue(experience.assess(enrich_jd(job), 6, 11)["drop"])

    def test_turbohire_incomplete_full_board_is_not_reported_as_success(self):
        row = {"JobId": "1", "JobIdObfuscated": "one", "JobTitle": "SRE", "Location": "[]"}
        with patch.object(fetchers, "_get", side_effect=[{"access_token": "anonymous"}, {"OrgID": "org"}]), \
             patch.object(fetchers, "_post", return_value={"Total": 2, "Result": [row]}):
            jobs = fetchers.fetch_company({"name": "Navi", "ats": "turbohire", "token": "navi"})
        self.assertEqual([j["id"] for j in jobs], ["1"])
        self.assertIn("Navi", " ".join(fetchers.FETCH_ERRORS))

    def test_source_errors_do_not_expose_request_urls(self):
        company = {"name": "Example", "ats": "greenhouse", "token": "example"}
        with patch.object(fetchers, "_get", side_effect=RuntimeError(
                "403 for https://example.test/private?token=secret")):
            fetchers.fetch_company(company)
        self.assertNotIn("secret", str(fetchers.FETCH_ERRORS))
        self.assertIn("403", str(fetchers.FETCH_ERRORS))


if __name__ == "__main__":
    unittest.main()
