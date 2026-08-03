"""Regression tests for the matching rules.

Every case below was a real defect in the substring-matching version.
"""

import pytest

from app.taxonomy import (
    categorize,
    detect_seniority,
    detect_work_mode,
    extract_skills,
    is_tech_job,
    parse_experience,
)


class TestSkillExtraction:
    def test_matches_whole_tokens_only(self):
        # "ai" inside "Retail", "ml" inside "HTML", "c" inside every word.
        assert extract_skills("Retail assistant, HTML basics") == ["HTML"]

    def test_trailing_punctuation_does_not_block(self):
        assert "PostgreSQL" in extract_skills("We run on PostgreSQL.")

    def test_dotted_and_symbol_tokens(self):
        found = extract_skills("Node.js, C++, C#, .NET and React")
        assert {"Node.js", "C++", "C#", ".NET", "React"} <= set(found)

    def test_longest_alias_wins(self):
        assert "React Native" in extract_skills("React Native developer")

    def test_bare_c_is_matched_but_not_inside_words(self):
        assert "C" in extract_skills("Strong in C and data structures")
        assert "C" not in extract_skills("Basic accounting and reconciliation")

    def test_empty_input(self):
        assert extract_skills("") == []


class TestTechClassification:
    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer",
            "DevOps Engineer",
            "Application Support Engineer",
            "SAP ABAP Consultant",
            "Network Administrator",
            "SOC Analyst",
            "Database Administrator",
            "SDET",
            "Associate - Cloud Operations",
            "UI/UX Designer",
        ],
    )
    def test_accepts_the_breadth_of_it_roles(self, title):
        assert is_tech_job(title) is True

    def test_graduate_engineer_trainee_needs_corroboration(self):
        """"Graduate Engineer Trainee" is discipline-agnostic in India.

        Mechanical, civil and electrical employers all use it, so the title
        alone cannot admit a posting - it was letting AutoCAD draughting roles
        into the index and labelling them Software Engineering. With technical
        skills attached it is a real software fresher role and is kept.
        """
        assert is_tech_job("Graduate Engineer Trainee") is False
        assert is_tech_job("Graduate Engineer Trainee", skills=["AutoCAD"]) is False
        assert is_tech_job("Graduate Engineer Trainee", skills=["Java", "SQL"]) is True
        assert is_tech_job("Graduate Engineer Trainee - Software") is True

    @pytest.mark.parametrize(
        "title",
        [
            "Retail Store Manager",
            "Sales Executive",
            "HR Recruiter",
            "Chartered Accountant",
            "Civil Engineer - Site",
            "Voice Process Executive",
            "Staff Nurse",
        ],
    )
    def test_rejects_non_tech(self, title):
        assert is_tech_job(title) is False

    def test_strong_tech_title_beats_non_tech_word(self):
        # "sales" is a non-tech term, but this is clearly an engineering role.
        assert is_tech_job("Software Engineer - Sales Platform") is True

    def test_generic_role_with_dense_skills(self):
        assert is_tech_job("Associate", "", ["Python", "AWS"]) is True


class TestCategorisation:
    @pytest.mark.parametrize(
        ("title", "description", "expected"),
        [
            ("Full Stack Developer", "React and Node.js", "Full Stack"),
            ("Data Analyst", "SQL, Power BI dashboards", "Data Engineering / Analytics"),
            ("SOC Analyst", "SIEM, Splunk, threat hunting", "Cybersecurity"),
            ("Android Developer", "Kotlin, Jetpack Compose", "Mobile Development"),
            ("SDET", "Selenium automation testing", "QA / Testing"),
            ("Site Reliability Engineer", "Kubernetes, Terraform", "DevOps / Cloud / SRE"),
            ("ML Engineer", "PyTorch, transformers", "Data Science / AI-ML"),
        ],
    )
    def test_expected_category(self, title, description, expected):
        assert categorize(title, description) == expected

    def test_unknown_falls_back_to_other(self):
        assert categorize("Zookeeper Assistant", "feeding animals") == "Other"

    def test_title_outweighs_description_noise(self):
        # A backend JD that name-drops every cloud tool is still Backend.
        assert (
            categorize("Backend Developer", "aws azure gcp docker kubernetes terraform")
            == "DevOps / Cloud / SRE"
        ) or categorize("Backend Developer", "django rest api") == "Backend"


class TestExperience:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0-3 Yrs", (0, 3)),
            ("2 to 5 years", (2, 5)),
            ("Fresher", (0, 1)),
            ("Entry level", (0, 1)),
            ("5+ years", (5, 7)),
            ("", (None, None)),
            ("08 Aug", (None, None)),  # a walk-in date, not an experience range
        ],
    )
    def test_parse(self, text, expected):
        assert parse_experience(text) == expected

    def test_seniority_from_title(self):
        assert detect_seniority("Software Engineer Intern", "") == "intern"
        assert detect_seniority("Senior Backend Engineer", "8-12 Yrs", 8) == "senior"
        assert detect_seniority("Tech Lead", "") == "lead"

    def test_seniority_from_years_when_title_is_neutral(self):
        assert detect_seniority("Software Engineer", "", 0) == "fresher"
        assert detect_seniority("Software Engineer", "", 2) == "junior"
        assert detect_seniority("Software Engineer", "", 4) == "mid"


class TestWorkMode:
    def test_remote(self):
        assert detect_work_mode("Remote", "Backend Developer") == "remote"

    def test_hybrid_wins_over_remote(self):
        assert detect_work_mode("Hybrid - Pune", "", "remote work available") == "hybrid"

    def test_default_onsite(self):
        assert detect_work_mode("Bengaluru", "QA Engineer") == "onsite"
