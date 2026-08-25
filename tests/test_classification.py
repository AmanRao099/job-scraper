"""Qualification, country, visa and relocation classifier regressions."""

import pytest

from app.classification import classify_international, parse_qualifications


@pytest.mark.parametrize(
    ("description", "degree", "requirement"),
    [
        ("A Masters degree is required for this role.", "Masters degree", "required"),
        ("MS or MSc preferred in Computer Science.", "MS", "preferred"),
        ("A Bachelor's or Masters degree in engineering.", "Masters degree", "accepted"),
        ("An Advanced degree is a plus.", "Advanced degree", "preferred"),
        ("M.Tech in Computer Science.", "M.Tech", "mentioned"),
        ("Minimum qualification: M.E./M.Tech.", "M.E.", "required"),
        ("Masters preferred but not mandatory.", "Masters degree", "preferred"),
        ("Master of Science in AI accepted.", "Master of Science", "accepted"),
        ("Master of Engineering in embedded systems.", "Master of Engineering", "mentioned"),
        ("MEng preferred for this position.", "MEng", "preferred"),
        ("MS required.", "MS", "required"),
        ("Postgraduate degree in a related field.", "Postgraduate degree", "mentioned"),
        ("MTech candidates may apply.", "M.Tech", "mentioned"),
    ],
)
def test_masters_equivalents(description, degree, requirement):
    result = parse_qualifications(description)
    assert result.masters_match is True
    assert degree in result.degree_requirements
    assert result.education_requirement == requirement


@pytest.mark.parametrize(
    "description",
    [
        "Certified Scrum Master required.",
        "Own the master data platform and master records.",
        "Merge changes into the master branch.",
        "Review the master services agreement.",
        "For questions, contact me.",
        "Bachelor's degree in Computer Science required.",
        "Experience with MS SQL and Microsoft Office.",
    ],
)
def test_false_positives_are_not_degrees(description):
    result = parse_qualifications(description)
    assert result.masters_match is False
    assert result.degree_requirements == []
    assert result.education_requirement == "not_stated"


def test_unrelated_required_does_not_change_degree_context():
    result = parse_qualifications(
        "On-call work is required. You will build Python APIs. A Masters degree is a plus."
    )
    assert result.education_requirement == "preferred"


def test_experience_numbers_are_not_degrees():
    assert parse_qualifications("Requires 4-6 years of Python experience.").masters_match is False


@pytest.mark.parametrize("location", ["Bengaluru", "Mumbai", "Kochi", "Pune, Maharashtra"])
def test_indian_locations(location):
    result = classify_international(location, "")
    assert result.country == "India"
    assert result.is_abroad is False


@pytest.mark.parametrize(
    ("location", "country"),
    [("Berlin", "Germany"), ("Toronto, Canada", "Canada"), ("Sydney", "Australia")],
)
def test_international_locations(location, country):
    result = classify_international(location, "")
    assert result.country == country
    assert result.is_abroad is True


def test_worldwide_remote_is_international():
    result = classify_international("Remote", "Open to candidates worldwide; work from anywhere.")
    assert result.country == "Worldwide"
    assert result.is_abroad is True


def test_plain_remote_stays_unknown():
    result = classify_international("Remote", "Join our distributed Python team.")
    assert result.country is None
    assert result.is_abroad is False
    assert result.visa_sponsorship == "unknown"


def test_visa_offered():
    assert classify_international("London", "Visa sponsorship available.").visa_sponsorship == "offered"


def test_visa_unavailable_takes_precedence():
    result = classify_international(
        "London", "We discuss sponsorship, but no visa sponsorship is available."
    )
    assert result.visa_sponsorship == "not_offered"


def test_work_authorization_required():
    result = classify_international("New York", "Must already be authorized to work in the US.")
    assert result.work_authorization_required is True


def test_relocation_support():
    offered = classify_international("Berlin", "Relocation assistance provided.")
    unavailable = classify_international("Berlin", "No relocation assistance is available.")
    assert offered.relocation_support == "offered"
    assert unavailable.relocation_support == "not_offered"
