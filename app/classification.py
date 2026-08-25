"""Deterministic education and international-posting classification.

The classifiers deliberately consume only text already present in a posting.
They are kept outside the scraper adapters so source query wording can never be
mistaken for evidence from the actual job description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


EDUCATION_REQUIREMENTS = ("required", "preferred", "accepted", "mentioned", "not_stated")
VISA_SPONSORSHIP_VALUES = ("offered", "not_offered", "unknown")
RELOCATION_SUPPORT_VALUES = ("offered", "not_offered", "unknown")


@dataclass(frozen=True, slots=True)
class QualificationResult:
    degree_requirements: list[str] = field(default_factory=list)
    masters_match: bool = False
    education_requirement: str = "not_stated"


@dataclass(frozen=True, slots=True)
class InternationalResult:
    country: str | None = None
    is_abroad: bool = False
    visa_sponsorship: str = "unknown"
    work_authorization_required: bool = False
    relocation_support: str = "unknown"


# Long forms come first so the stored degree list is as informative as possible.
# Short forms use strict boundaries and, for MS/ME, qualification context checks
# below; this prevents "MS SQL", "contact me", and ordinary prose from matching.
_DEGREE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Master of Engineering", re.compile(r"\bmaster(?:'s)?\s+of\s+engineering\b", re.I)),
    ("Master of Science", re.compile(r"\bmaster(?:'s)?\s+of\s+science\b", re.I)),
    ("Masters degree", re.compile(r"\bmaster(?:'s|s)?\s+degree\b", re.I)),
    ("Masters degree", re.compile(r"\b(?:master's|masters)\b(?!\s+(?:data|branch|record|services))", re.I)),
    ("Postgraduate degree", re.compile(r"\bpost[ -]?graduate\s+degree\b", re.I)),
    ("Advanced degree", re.compile(r"\badvanced\s+degree\b", re.I)),
    ("M.Tech", re.compile(r"(?<![A-Za-z0-9])m\s*\.?\s*tech\.?\b", re.I)),
    ("MEng", re.compile(r"(?<![A-Za-z0-9])m\s*\.?\s*eng\.?\b", re.I)),
    ("MSc", re.compile(r"(?<![A-Za-z0-9])m\s*\.?\s*sc\.?\b", re.I)),
    ("M.E.", re.compile(r"(?<![A-Za-z0-9])m\s*\.\s*e\s*\.?\b", re.I)),
    ("MS", re.compile(r"(?<![A-Za-z0-9])m\.?s\.?(?![A-Za-z0-9])", re.I)),
)

_DEGREE_CONTEXT_RE = re.compile(
    r"\b(?:degree|education|qualification|academic|graduate|postgraduate|"
    r"bachelor|bsc|b\.s\.?|computer science|engineering|related field|discipline)\b",
    re.I,
)
_SHORT_DEGREE_NOISE_RE = re.compile(
    r"\b(?:ms\s+(?:sql|office|excel|word|teams|azure|dynamics|access)|"
    r"master\s+(?:data|branch|record|services agreement)|scrum\s+master)\b",
    re.I,
)

_PREFERRED_RE = re.compile(
    r"\b(?:preferred|preference|a plus|plus|nice to have|desirable|advantage|"
    r"beneficial|not mandatory)\b",
    re.I,
)
_REQUIRED_RE = re.compile(
    r"\b(?:required|requirement|must have|must possess|minimum (?:education|qualification)|"
    r"minimum qualification|mandatory|essential)\b",
    re.I,
)
_ACCEPTED_RE = re.compile(
    r"\b(?:accepted|acceptable|or (?:a )?master(?:'s|s)?|"
    r"bachelor(?:'s|s)?(?:\s+degree)?\s+(?:and\s*/?\s*or|or)\s+master|"
    r"bs\s*/\s*ms|b\.s\.?\s*/\s*m\.s\.?)\b",
    re.I,
)


def _context(text: str, start: int, end: int, radius: int = 110) -> str:
    """Return nearby clause-sized context without consulting unrelated prose."""
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    window = text[left:right]
    # Keep the containing sentence/clause; requirements elsewhere in a long JD
    # must not influence this degree mention.
    pieces = re.split(r"[\n\r]|(?<=[.!?;])\s+", window)
    relative = start - left
    cursor = 0
    selected: list[str] = []
    for piece in pieces:
        piece_end = cursor + len(piece)
        if piece_end >= relative - 1 and cursor <= (end - left) + 1:
            selected.append(piece)
        cursor = piece_end + 1
    return " ".join(selected) if selected else window


def parse_qualifications(description: str | None) -> QualificationResult:
    """Extract Masters-equivalent degrees and requirement strength.

    Only the posting description is intended as input. Callers should not append
    the search query or title: a query hit is not evidence of a qualification.
    """
    text = description or ""
    if not text:
        return QualificationResult()

    matches: list[tuple[int, int, str, str]] = []
    for canonical, pattern in _DEGREE_PATTERNS:
        for match in pattern.finditer(text):
            nearby = _context(text, match.start(), match.end())
            if _SHORT_DEGREE_NOISE_RE.search(nearby):
                continue
            if canonical in {"MS", "M.E."} and not (
                _DEGREE_CONTEXT_RE.search(nearby)
                or re.search(r"\b(?:msc|meng|m\.?tech|masters?)\b", nearby, re.I)
                or _REQUIRED_RE.search(nearby)
                or _PREFERRED_RE.search(nearby)
                or _ACCEPTED_RE.search(nearby)
            ):
                continue
            matches.append((match.start(), match.end(), canonical, nearby))

    if not matches:
        return QualificationResult()

    matches.sort(key=lambda item: item[0])
    degrees: list[str] = []
    classifications: list[str] = []
    for _, _, canonical, nearby in matches:
        if canonical not in degrees:
            degrees.append(canonical)
        # "preferred but not mandatory" is preferred even though mandatory is
        # present. Preference signals therefore outrank required signals.
        if _PREFERRED_RE.search(nearby):
            classifications.append("preferred")
        elif _ACCEPTED_RE.search(nearby):
            classifications.append("accepted")
        elif _REQUIRED_RE.search(nearby):
            classifications.append("required")
        else:
            classifications.append("mentioned")

    # Preserve the strongest explicit relationship found for any Masters degree.
    priority = {"required": 4, "preferred": 3, "accepted": 2, "mentioned": 1}
    requirement = max(classifications, key=priority.__getitem__)
    return QualificationResult(degrees, True, requirement)


# India detection is intentionally broader than a handful of cities: absence of
# the word "India" must not turn a domestic posting into an international one.
_INDIA_RE = re.compile(
    r"\b(?:india|bharat|bengaluru|bangalore|mumbai|bombay|delhi|new delhi|"
    r"hyderabad|pune|chennai|madras|kolkata|calcutta|kochi|cochin|gurugram|"
    r"gurgaon|noida|ahmedabad|jaipur|chandigarh|lucknow|indore|bhopal|surat|"
    r"vadodara|nagpur|mysuru|mysore|mangaluru|mangalore|coimbatore|trivandrum|"
    r"thiruvananthapuram|visakhapatnam|vijayawada|bhubaneswar|patna|ranchi|"
    r"goa|karnataka|maharashtra|telangana|tamil nadu|kerala|west bengal|"
    r"uttar pradesh|madhya pradesh|rajasthan|gujarat|odisha|punjab|haryana)\b",
    re.I,
)

# Country names are normalization evidence, not an acceptance allowlist. Any
# confidently non-India country is accepted; unknown locations remain unknown.
_COUNTRY_ALIASES: dict[str, str] = {
    "united states": "United States", "united states of america": "United States",
    "usa": "United States", "u.s.a": "United States", "us": "United States",
    "canada": "Canada", "mexico": "Mexico", "brazil": "Brazil", "argentina": "Argentina",
    "chile": "Chile", "colombia": "Colombia", "peru": "Peru",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "u.k": "United Kingdom",
    "ireland": "Ireland", "france": "France", "germany": "Germany", "spain": "Spain",
    "portugal": "Portugal", "italy": "Italy", "netherlands": "Netherlands",
    "belgium": "Belgium", "luxembourg": "Luxembourg", "switzerland": "Switzerland",
    "austria": "Austria", "poland": "Poland", "czechia": "Czechia",
    "czech republic": "Czechia", "slovakia": "Slovakia", "hungary": "Hungary",
    "romania": "Romania", "bulgaria": "Bulgaria", "greece": "Greece",
    "denmark": "Denmark", "sweden": "Sweden", "norway": "Norway", "finland": "Finland",
    "iceland": "Iceland", "estonia": "Estonia", "latvia": "Latvia", "lithuania": "Lithuania",
    "ukraine": "Ukraine", "croatia": "Croatia", "serbia": "Serbia", "slovenia": "Slovenia",
    "turkey": "Turkey", "türkiye": "Turkey", "israel": "Israel", "united arab emirates": "United Arab Emirates",
    "uae": "United Arab Emirates", "saudi arabia": "Saudi Arabia", "qatar": "Qatar",
    "egypt": "Egypt", "south africa": "South Africa", "kenya": "Kenya", "nigeria": "Nigeria",
    "ghana": "Ghana", "morocco": "Morocco", "tunisia": "Tunisia",
    "australia": "Australia", "new zealand": "New Zealand",
    "singapore": "Singapore", "malaysia": "Malaysia", "indonesia": "Indonesia",
    "philippines": "Philippines", "thailand": "Thailand", "vietnam": "Vietnam",
    "japan": "Japan", "south korea": "South Korea", "korea": "South Korea",
    "china": "China", "hong kong": "Hong Kong", "taiwan": "Taiwan",
    "pakistan": "Pakistan", "bangladesh": "Bangladesh", "sri lanka": "Sri Lanka",
    "nepal": "Nepal",
}
_ADDITIONAL_COUNTRIES = """
afghanistan albania algeria andorra angola antigua-and-barbuda armenia azerbaijan
bahamas bahrain barbados belarus belize benin bhutan bolivia bosnia-and-herzegovina
botswana brunei burkina-faso burundi cabo-verde cambodia cameroon
central-african-republic chad comoros costa-rica cuba cyprus djibouti dominica
dominican-republic ecuador el-salvador equatorial-guinea eritrea eswatini ethiopia
fiji gabon gambia georgia grenada guatemala guinea guinea-bissau guyana haiti
honduras iran iraq jamaica jordan kazakhstan kiribati kuwait kyrgyzstan laos
lebanon lesotho liberia libya liechtenstein madagascar malawi maldives mali malta
marshall-islands mauritania mauritius micronesia moldova monaco mongolia montenegro
mozambique myanmar namibia nauru nicaragua niger north-macedonia oman palau panama
papua-new-guinea paraguay russia rwanda saint-kitts-and-nevis saint-lucia
saint-vincent-and-the-grenadines samoa san-marino sao-tome-and-principe senegal
seychelles sierra-leone solomon-islands somalia south-sudan sudan suriname syria
tajikistan tanzania timor-leste togo tonga trinidad-and-tobago turkmenistan tuvalu
uganda uruguay uzbekistan vanuatu vatican-city venezuela yemen zambia zimbabwe
""".split()
for _country in _ADDITIONAL_COUNTRIES:
    _name = _country.replace("-", " ")
    _COUNTRY_ALIASES.setdefault(_name, _name.title())
_COUNTRY_ALIASES.update(
    {
        "democratic republic of the congo": "Democratic Republic of the Congo",
        "dr congo": "Democratic Republic of the Congo",
        "republic of the congo": "Republic of the Congo",
        "congo": "Republic of the Congo",
        "ivory coast": "Côte d'Ivoire",
        "côte d'ivoire": "Côte d'Ivoire",
        "north korea": "North Korea",
        "palestine": "Palestine",
        "kosovo": "Kosovo",
    }
)

_CITY_COUNTRIES: dict[str, str] = {
    "new york": "United States", "san francisco": "United States", "seattle": "United States",
    "austin": "United States", "boston": "United States", "chicago": "United States",
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada",
    "london": "United Kingdom", "manchester": "United Kingdom", "dublin": "Ireland",
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany",
    "paris": "France", "amsterdam": "Netherlands", "zurich": "Switzerland",
    "stockholm": "Sweden", "oslo": "Norway", "helsinki": "Finland", "copenhagen": "Denmark",
    "warsaw": "Poland", "prague": "Czechia", "lisbon": "Portugal", "madrid": "Spain",
    "barcelona": "Spain", "rome": "Italy", "milan": "Italy", "vienna": "Austria",
    "tel aviv": "Israel", "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates",
    "sydney": "Australia", "melbourne": "Australia", "auckland": "New Zealand",
    "tokyo": "Japan", "seoul": "South Korea", "beijing": "China", "shanghai": "China",
    "singapore": "Singapore", "hong kong": "Hong Kong",
}


def _term_pattern(terms: object) -> re.Pattern[str]:
    body = "|".join(re.escape(str(term)) for term in sorted(terms, key=lambda x: len(str(x)), reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])", re.I)


_COUNTRY_RE = _term_pattern(_COUNTRY_ALIASES)
_CITY_RE = _term_pattern(_CITY_COUNTRIES)
_FOREIGN_REGION_RE = re.compile(
    r"\b(?:europe|european union|north america|latin america|south america|"
    r"middle east|africa|australia and new zealand)\b",
    re.I,
)
_WORLDWIDE_RE = re.compile(
    r"\b(?:remote\s+(?:worldwide|globally)|(?:worldwide|global(?:ly)?)\s+remote|work\s+from\s+anywhere|"
    r"candidates?\s+(?:from|located\s+in)\s+anywhere|open\s+to\s+(?:candidates\s+)?worldwide|"
    r"anywhere\s+in\s+the\s+world|global(?:ly)?\s+distributed\s+team)\b",
    re.I,
)

_VISA_NOT_OFFERED_RE = re.compile(
    r"\b(?:no|not|without|unable to provide|cannot provide|do not provide|won't provide|"
    r"will not provide)\s+(?:visa\s+)?sponsorship\b|\bsponsorship\s+(?:(?:is\s+)?not available|unavailable)\b",
    re.I,
)
_VISA_OFFERED_RE = re.compile(
    r"\b(?:visa sponsorship (?:is )?available|h-?1b sponsorship|we will sponsor|"
    r"sponsorship provided|eligible for sponsorship|can sponsor)\b",
    re.I,
)
_WORK_AUTH_RE = re.compile(
    r"\b(?:must (?:already )?be (?:legally )?authorized to work|right to work (?:is )?required|"
    r"valid work authorization (?:is )?required|must have (?:the )?right to work|"
    r"eligible to work without sponsorship)\b",
    re.I,
)
_RELOCATION_NOT_OFFERED_RE = re.compile(
    r"\b(?:no|not|without|cannot provide|do not provide|will not provide)\s+relocation\s+(?:assistance|support)\b",
    re.I,
)
_RELOCATION_OFFERED_RE = re.compile(
    r"\b(?:relocation (?:assistance|support|package) (?:is )?(?:provided|available|offered)|"
    r"we (?:offer|provide) relocation|relocation provided)\b",
    re.I,
)


def classify_international(location: str | None, description: str | None) -> InternationalResult:
    """Best-effort geography and mobility classification.

    A bare ``Remote`` location remains geographically unknown. Worldwide remote
    eligibility must be stated in the description to count as international.
    """
    loc = (location or "").strip()
    desc = description or ""

    country: str | None = None
    is_abroad = False
    if _INDIA_RE.search(loc):
        country = "India"
    else:
        country_match = _COUNTRY_RE.search(loc)
        city_match = _CITY_RE.search(loc)
        if country_match:
            country = _COUNTRY_ALIASES[country_match.group(0).lower()]
            is_abroad = country != "India"
        elif city_match:
            country = _CITY_COUNTRIES[city_match.group(0).lower()]
            is_abroad = True
        elif _FOREIGN_REGION_RE.search(loc):
            # Region-level evidence can establish "abroad" even when there is
            # no single country to normalize. Country remains best-effort.
            is_abroad = True
        elif _WORLDWIDE_RE.search(desc):
            country = "Worldwide"
            is_abroad = True

    # Negative phrases take precedence over all generic/positive mentions.
    if _VISA_NOT_OFFERED_RE.search(desc):
        visa = "not_offered"
    elif _VISA_OFFERED_RE.search(desc):
        visa = "offered"
    else:
        visa = "unknown"

    if _RELOCATION_NOT_OFFERED_RE.search(desc):
        relocation = "not_offered"
    elif _RELOCATION_OFFERED_RE.search(desc):
        relocation = "offered"
    else:
        relocation = "unknown"

    return InternationalResult(
        country=country,
        is_abroad=is_abroad,
        visa_sponsorship=visa,
        work_authorization_required=bool(_WORK_AUTH_RE.search(desc)),
        relocation_support=relocation,
    )
