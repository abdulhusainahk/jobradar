"""Behavioral boundaries for deterministic job matching (standard-library unittest)."""
import unittest

from jobradar import experience, fit
from jobradar import filter as matching


def match_config(*regions):
    return {
        "role_keywords": ["devops", "platform engineer", "site reliability", "sre"],
        "seniority_keywords": ["senior", "staff", "lead", "sde-3"],
        "exclude_keywords": ["intern", "internship", "junior", "jr.", "entry-level", "director"],
        "exclude_unless_finance": ["vice president", "associate"],
        "exclude_companies": ["dream sports"],
        "locations": {
            "india": ["mumbai", "navi mumbai", "bengaluru", "india"],
            "uae": ["dubai", "abu dhabi", "uae", "united arab emirates"],
            "europe": ["dublin", "ireland", "london", "united kingdom", "berlin", "germany", "emea"],
        },
        "regions_enabled": list(regions),
        "remote_region_keywords": ["india", "apac", "asia", "europe", "emea", "global", "worldwide", "anywhere"],
    }


def job(title="Senior DevOps Engineer", location="Mumbai", **fields):
    return {"title": title, "location": location, "company": "Example", **fields}


class PhraseMatchingTests(unittest.TestCase):
    def test_internal_platform_is_not_an_intern(self):
        self.assertTrue(matching.passes(
            job("Senior DevOps Engineer - Internal Platform"), match_config("india")))
        self.assertTrue(matching.passes(
            job("Senior DevOps Engineer - Directory Services"), match_config("india")))

    def test_actual_excluded_levels_still_drop(self):
        for title in ("DevOps Intern", "DevOps Internship", "Junior DevOps Engineer",
                      "Jr. DevOps Engineer", "Entry Level DevOps Engineer", "Director of DevOps"):
            with self.subTest(title=title):
                self.assertFalse(matching.passes(job(title), match_config("india")))

    def test_role_phrases_normalize_separators_but_not_word_fragments(self):
        config = match_config("india")
        self.assertTrue(matching.role_matches("Senior PLATFORM—ENGINEER", config))
        self.assertTrue(matching.role_matches("Site\u00a0Reliability Engineer", config))
        self.assertFalse(matching.role_matches("DevOpsy Developer", config))
        self.assertFalse(matching.role_matches("Platform Engineering Manager", config))

    def test_finance_exception_does_not_override_director_exclusion(self):
        config = match_config("india")
        for title in ("Associate DevOps Engineer", "Vice-President, DevOps"):
            with self.subTest(title=title):
                self.assertFalse(matching.passes(job(title), config))
                self.assertTrue(matching.passes(job(title, _finance=True), config))
        self.assertFalse(matching.passes(job("Executive Director, DevOps", _finance=True), config))

    def test_company_and_seniority_phrases_have_boundaries(self):
        config = match_config("india")
        self.assertTrue(matching.is_excluded(job(company="Dream-Sports Ltd"), config))
        self.assertFalse(matching.is_excluded(job(company="Dream Sportswear"), config))
        self.assertEqual(matching.seniority_tag("Stafford DevOps Engineer", config), "")
        self.assertEqual(matching.seniority_tag("Staff DevOps Engineer", config), "Staff")
        self.assertEqual(matching.seniority_tag("DevOps SDE III", config), "")
        self.assertEqual(matching.seniority_tag("DevOps SDE 3", config), "Sde-3")


class GeographicMatchingTests(unittest.TestCase):
    def test_indiana_is_not_india(self):
        config = match_config("india")
        for location in ("Indianapolis, Indiana, US", "Indianapolis, IN, US",
                         "Indianapolis, IN", "Indiana", "Indianapolis"):
            with self.subTest(location=location):
                self.assertFalse(matching.location_matches(location, config))
                self.assertFalse(matching.location_is_india(location, config))
        self.assertTrue(matching.location_matches("Mumbai, Maharashtra, India", config))
        self.assertTrue(matching.location_is_india("Mumbai", config))

    def test_explicit_country_disambiguates_shared_city_names(self):
        config = match_config("europe")
        self.assertFalse(matching.location_matches("Dublin, California, US", config))
        self.assertFalse(matching.location_matches("Dublin California United States", config))
        self.assertFalse(matching.location_matches("London, Ontario, Canada", config))
        self.assertFalse(matching.location_matches("Dublin, DE, US", config))
        self.assertTrue(matching.location_matches("Dublin, Ireland", config))
        self.assertTrue(matching.location_matches("Dublin, IE", config))
        self.assertTrue(matching.location_matches("London, United Kingdom", config))

    def test_unknown_places_fail_without_inventing_a_country(self):
        config = match_config("india", "europe")
        self.assertFalse(matching.location_matches("Unknown City", config))
        self.assertFalse(matching.location_matches("Dublin, Unknown Country", config))
        self.assertFalse(matching.location_matches("Mumbai, Unknown Country", config))
        self.assertTrue(matching.location_matches("Unknown City, India", config))
        self.assertTrue(matching.location_matches("Mumbai (Hybrid)", config))

    def test_multi_location_roles_keep_an_allowed_alternative(self):
        india = match_config("india")
        europe = match_config("europe")
        self.assertTrue(matching.location_matches("Dublin, California, US; Mumbai, India", india))
        self.assertFalse(matching.location_matches("Dublin, California, US; Mumbai, India", europe))
        self.assertTrue(matching.location_matches("Mumbai, India; Dublin, Ireland", europe))
        self.assertTrue(matching.location_is_india("Dublin, Ireland / Mumbai", europe))
        self.assertTrue(matching.location_matches("Mumbai / Dublin", europe))

    def test_remote_scope_respects_enabled_regions(self):
        india = match_config("india")
        europe = match_config("europe")
        self.assertFalse(matching.location_matches("Remote - Europe", india))
        self.assertFalse(matching.location_matches("Remote - EMEA", india))
        self.assertTrue(matching.location_matches("Remote - Europe", europe))
        self.assertTrue(matching.location_matches("Remote - APAC", india))
        self.assertFalse(matching.location_matches("Remote - APAC", europe))
        self.assertFalse(matching.location_matches("Remote", india))
        self.assertFalse(matching.location_matches("Remote US", india))
        self.assertTrue(matching.location_matches("Remote Global", india))
        self.assertTrue(matching.location_matches("Remote Worldwide", europe))
        self.assertFalse(matching.location_matches("Remote Anywhere", match_config()))
        self.assertFalse(matching.location_matches("Remote Global (US only)", india))
        self.assertFalse(matching.location_is_india("Remote Global", india))
        self.assertTrue(matching.location_is_india("Remote India", india))

    def test_provider_country_fields_override_ambiguous_display_city(self):
        config = match_config("europe")
        self.assertFalse(matching.passes(job(location="Dublin", country_code="US"), config))
        self.assertTrue(matching.passes(job(location="Dublin", country_code="IE"), config))
        self.assertFalse(matching.passes(job(location="Dublin", country="Unknown"), config))
        self.assertFalse(matching.passes(job(location="Dublin", locations=[
            {"city": "Dublin", "country": "US"}]), config))
        self.assertTrue(matching.passes(job(location="Dublin", locations=[
            {"city": "Dublin", "country": "US"},
            {"city": "Mumbai", "countryCode": "IN"}]), match_config("india")))


class CandidateSelectionTests(unittest.TestCase):
    def test_broader_titles_are_candidates_without_relaxing_hard_exclusions(self):
        config = match_config("india")
        config["role_keywords"].extend(["production engineer", "developer productivity"])
        self.assertTrue(matching.is_candidate(job("Senior Production Engineer"), config))
        self.assertTrue(matching.is_candidate(job("Developer Productivity Engineer"), config))
        self.assertFalse(matching.is_candidate(job("Production Engineer Intern"), config))
        self.assertFalse(matching.is_candidate(job("Production Engineer", company="Dream Sports"), config))
        self.assertFalse(matching.is_candidate(job("Production Engineer", country_code="US"), config))

    def test_unknown_location_reaches_ai_but_not_strict_fallback(self):
        config = match_config("india")
        self.assertTrue(matching.is_candidate(job(location="Remote"), config))
        self.assertEqual(matching.location_status(job(location="Remote"), config), "unknown")
        self.assertFalse(matching.passes(job(location="Remote"), config))
        self.assertFalse(matching.is_candidate(job(location="Remote US"), config))
        self.assertFalse(matching.is_candidate(job(location="Remote Europe"), config))
        self.assertFalse(matching.is_candidate(job(location="Remote"), match_config()))

    def test_unknown_alternative_does_not_erase_explicit_allowed_location(self):
        config = match_config("india")
        self.assertEqual(matching.location_status(job(location="Office TBD; Mumbai, India"), config), "allowed")
        self.assertTrue(matching.passes(job(location="Office TBD; Mumbai, India"), config))
        self.assertTrue(matching.is_candidate(job(location="US; Office TBD"), config))
        self.assertFalse(matching.passes(job(location="US; Office TBD"), config))


class ExperienceMatchingTests(unittest.TestCase):
    def test_engineering_band_is_not_replaced_by_junior_tool_tenure(self):
        for description in (
            "6+ years engineering; 2-3 years Kubernetes",
            "2-3 years Kubernetes; 6+ years engineering",
            "6+ years of software engineering experience, including 2-3 years with Kubernetes",
            "6+ years of professional engineering experience and 2-3 years Kubernetes",
        ):
            with self.subTest(description=description):
                self.assertEqual(experience.required_years(description), (6, None))
                self.assertFalse(experience.assess(description, 6, 11)["drop"])

    def test_overall_senior_requirement_is_not_hidden_by_tool_range(self):
        for description in (
            "12+ overall; 3-5 Terraform",
            "3-5 years Terraform; 12+ years overall experience",
            "Overall experience: 12+ years; Terraform experience: 3-5 years",
            "6+ years engineering; 12+ years total experience",
        ):
            with self.subTest(description=description):
                self.assertEqual(experience.required_years(description), (12, None))
                self.assertTrue(experience.assess(description, 6, 11)["drop"])

    def test_tool_only_and_ambiguous_tenure_cannot_hard_drop(self):
        for description in (
            "Overall experience: 12+ years with Terraform",
            "12+ years of overall experience with Kubernetes",
            "2-3 years with Kubernetes", "12+ years of experience with Terraform",
            "Terraform experience: 12+ years", "12+ years of Kubernetes experience",
            "3-5 years", "Our product has existed for 12 years.",
        ):
            with self.subTest(description=description):
                self.assertIsNone(experience.required_years(description))
                self.assertFalse(experience.assess(description, 6, 11)["drop"])

    def test_real_overall_band_boundaries_remain_effective(self):
        self.assertTrue(experience.assess("2-4 years of experience", 6, 11)["drop"])
        self.assertFalse(experience.assess("3-5 years of experience", 6, 11)["drop"])
        self.assertFalse(experience.assess("At least 11 years of experience", 6, 11)["drop"])
        self.assertTrue(experience.assess("Minimum of 12 years of experience", 6, 11)["drop"])
        self.assertFalse(experience.assess("2+ years of experience", 6, 11)["drop"])

    def test_prose_and_html_preserve_scope_and_ranges(self):
        description = ("<ul><li>At least <strong>six</strong>&nbsp;years’ experience "
                       "in software engineering.</li><li>Two to three years with Kubernetes.</li></ul>")
        self.assertEqual(experience.required_years(description), (6, None))
        self.assertFalse(experience.assess(description, 6, 11)["drop"])
        self.assertTrue(experience.assess(
            "<p>Experience: two–four yrs</p><p>Build great systems.</p>", 6, 11)["drop"])
        self.assertTrue(experience.assess("Twelve years of professional experience required.", 6, 11)["drop"])

    def test_conflicting_or_optional_bands_do_not_force_a_drop(self):
        self.assertFalse(experience.assess(
            "12+ years overall with a bachelor's or 8+ years overall with a master's", 6, 11)["drop"])
        self.assertFalse(experience.assess("12+ years of experience preferred", 6, 11)["drop"])
        self.assertFalse(experience.assess(
            "2-3 years of relevant experience; 6+ years of relevant experience", 6, 11)["drop"])


class SkillMatchingTests(unittest.TestCase):
    def test_short_skills_do_not_match_inside_ordinary_words(self):
        result = fit.devops_fit(job(title="Engineer"),
                               "Monitoring flaws for weeks while the team speaks to a maniac.")
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["score"], 0)
        self.assertTrue(result["monitoring_only"])
        actual = fit.devops_fit(job(title="Engineer"), "AWS, EKS, AKS and IaC")
        self.assertEqual(set(actual["matched"]), {"AWS", "EKS", "AKS", "IaC"})
        self.assertFalse(actual["monitoring_only"])

    def test_skill_phrases_normalize_without_double_counting_separator_aliases(self):
        plain = fit.devops_fit(job(title="Engineer"), "GitHub Actions; infrastructure as code; CI/CD")
        separated = fit.devops_fit(job(title="Engineer"), "GITHUB—ACTIONS; infrastructure-as-code; CI / CD")
        self.assertEqual(set(separated["matched"]), {"Github Actions", "Infrastructure As Code", "CI/CD"})
        self.assertEqual(separated["score"], plain["score"])
        self.assertEqual(fit.devops_fit(job(title="Engineer"), "CI/CD")["score"], 15)


if __name__ == "__main__":
    unittest.main()
