"""Targeted scrape profiles.

A normal run sweeps ~95 search terms across all of India and keeps anything up
to `MAX_EXPERIENCE_YEARS`. A *profile* is a saved narrowing of that on three
axes at once - which terms to search, which city to search them in, and which
postings survive afterwards - so one button produces a listing you would
otherwise assemble by hand from several filtered searches.

The post-scrape filter runs on top of the ordinary one in `app/enrich.py`, not
instead of it: a posting must first be a real, current, entry-level tech job,
and only then is it tested against the profile. Its rejections are counted
under their own reasons so the run log shows exactly where the funnel narrowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.enrich import NormalizedJob
from app.sources.base import SearchScope
from app.startups import is_probable_startup
from app.taxonomy import _compile as compile_terms

# Profile-specific reject reasons, kept distinct from the enrichment ones.
REJECT_OFF_LOCATION = "outside_target_city"
REJECT_NOT_FRESHER = "not_fresher"
REJECT_NOT_STARTUP = "not_startup"


@dataclass(frozen=True, slots=True)
class ScrapeProfile:
    key: str
    label: str
    description: str
    queries: tuple[str, ...]
    scope: SearchScope
    # Substrings that must appear in a posting's location. The boards spell one
    # city several ways ("Bengaluru", "Bangalore/Bengaluru", "Whitefield"), and
    # a nationwide query leaks other cities in regardless of the geo filter.
    location_terms: tuple[str, ...] = ()
    # Seniority labels that count as "fresher" outright.
    fresher_seniorities: frozenset[str] = frozenset({"fresher", "intern"})
    # A posting demanding more than this many years is not a fresher role,
    # whatever its title says.
    max_experience_years: int = 1
    require_startup: bool = False

    _location_re: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_location_re", compile_terms(self.location_terms))

    # ------------------------------------------------------------------ rules
    def location_ok(self, location: str) -> bool:
        if not self.location_terms:
            return True
        # An empty location cannot be verified. Dropping it is the point of a
        # city profile: a remote-or-unstated posting may be anywhere.
        return bool(location) and bool(self._location_re.search(location))

    def fresher_ok(self, job: NormalizedJob) -> bool:
        if job.experience_min is not None:
            if job.experience_min > self.max_experience_years:
                return False
            if job.experience_min == 0:
                return True
        return job.seniority in self.fresher_seniorities

    def startup_ok(self, job: NormalizedJob) -> bool:
        if not self.require_startup:
            return True
        return is_probable_startup(job.company, job.description, job.title)

    def reject_reason(self, job: NormalizedJob) -> str | None:
        """Why this posting does not belong in the profile, or None if it does."""
        if not self.location_ok(job.location):
            return REJECT_OFF_LOCATION
        if not self.fresher_ok(job):
            return REJECT_NOT_FRESHER
        if not self.startup_ok(job):
            return REJECT_NOT_STARTUP
        return None

    def apply(
        self, jobs: list[NormalizedJob]
    ) -> tuple[list[NormalizedJob], dict[str, int]]:
        kept: list[NormalizedJob] = []
        rejected: dict[str, int] = {}
        for job in jobs:
            reason = self.reject_reason(job)
            if reason is None:
                kept.append(job)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1
        return kept, rejected

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "location": self.scope.location,
            "queries": list(self.queries),
            "query_count": len(self.queries),
            "max_experience_years": self.max_experience_years,
            "require_startup": self.require_startup,
        }


# ---------------------------------------------------------------------------
# Bengaluru, freshers, startups
# ---------------------------------------------------------------------------

# Both spellings plus the tech corridors, because a card often names the
# neighbourhood rather than the city.
BENGALURU_TERMS: tuple[str, ...] = (
    "bengaluru", "bangalore", "banglore", "bangalore urban", "bangalore rural",
    "whitefield", "koramangala", "indiranagar", "electronic city", "electronics city",
    "hsr layout", "btm layout", "marathahalli", "bellandur", "sarjapur",
    "jayanagar", "jp nagar", "hebbal", "yelahanka", "banashankari",
    "rajajinagar", "malleshwaram", "domlur", "yeshwanthpur", "bommanahalli",
    "kadugodi", "mahadevapura", "nagawara", "hoodi", "brookefield",
)

# Fresher-slanted terms. Naukri turns each into
# `/<slug>-jobs-in-bangalore`, so the city is in the search itself rather than
# only in the post-filter.
FRESHER_QUERIES: tuple[str, ...] = (
    # generalist entry titles
    "fresher software engineer", "software engineer fresher",
    "graduate engineer trainee", "associate software engineer",
    "junior software engineer", "software developer fresher",
    "entry level software engineer", "product engineer",
    "founding engineer", "startup software engineer",
    # by stack
    "python developer fresher", "java developer fresher",
    "backend developer fresher", "frontend developer fresher",
    "full stack developer fresher", "react developer fresher",
    "node js developer fresher", "mern stack developer fresher",
    "android developer fresher", "flutter developer fresher",
    # data / ai
    "data analyst fresher", "data engineer fresher",
    "machine learning engineer fresher", "ai engineer fresher",
    # quality / platform / design
    "qa engineer fresher", "sdet fresher", "devops engineer fresher",
    "cloud engineer fresher", "ui ux designer fresher",
    # internships
    "software engineer intern", "data science intern", "product design intern",
)

BANGALORE_FRESHER_STARTUPS = ScrapeProfile(
    key="bangalore-fresher-startups",
    label="Bengaluru · freshers · startups",
    description=(
        "Entry-level tech roles in Bengaluru at companies that read as startups. "
        "Searches fresher-slanted terms scoped to the city, then keeps only "
        "postings located in Bengaluru, requiring at most "
        "1 year of experience, from a company that is neither an IT-services "
        "major, a global captive, a bank, a Big Four firm nor a staffing agency, "
        "and whose ad describes itself in startup terms (early-stage, Series A, "
        "founding team, YC-backed, …)."
    ),
    queries=FRESHER_QUERIES,
    scope=SearchScope(
        location="Bengaluru, Karnataka, India",
        linkedin_geo_id="105214831",  # Bengaluru, Karnataka, India
        naukri_location_slug="bangalore",
    ),
    location_terms=BENGALURU_TERMS,
    max_experience_years=1,
    require_startup=True,
)


PROFILES: dict[str, ScrapeProfile] = {
    BANGALORE_FRESHER_STARTUPS.key: BANGALORE_FRESHER_STARTUPS,
}

AVAILABLE_PROFILES = sorted(PROFILES)


def get_profile(key: str) -> ScrapeProfile:
    """Look a profile up by key, case- and separator-insensitively."""
    normalised = (key or "").strip().lower().replace("_", "-")
    profile = PROFILES.get(normalised)
    if profile is None:
        raise ValueError(
            f"Unknown scrape profile {key!r}. Available: {AVAILABLE_PROFILES}"
        )
    return profile


def resolve_profile(profile: "str | ScrapeProfile | None") -> ScrapeProfile | None:
    if profile is None or isinstance(profile, ScrapeProfile):
        return profile
    return get_profile(profile)
