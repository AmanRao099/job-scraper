"""Is this posting from a startup?

Neither Naukri nor LinkedIn's guest surfaces expose company size, funding stage
or founding year, so "startup" has to be inferred from the two strings we do
get: the company name and the job description. Two rules, in order:

1. **Hard block.** The IT-services majors, global captives, banks, Big Four and
   staffing agencies post the overwhelming majority of Bengaluru fresher ads.
   None of them is a startup, and their names are stable enough to list.
2. **Require a positive signal.** Whatever survives must say so itself -
   "early-stage", "Series A", "founding engineer", "YC-backed", and friends.

Rule 2 is deliberately precision-first: a genuine startup that never uses the
word in its ad is dropped. The alternative - keeping everything not on the
blocklist - readmits every mid-size product company and consultancy that the
list happens not to name, which is most of them.
"""

from __future__ import annotations

import re

# Same word-boundary rules as the skill matcher: "ai" must not match "retail",
# and "startup" must not match "startups"-free prose like "restartup".
from app.taxonomy import _compile as compile_terms

# ---------------------------------------------------------------------------
# 1. Companies that are definitionally not startups
# ---------------------------------------------------------------------------

# Matched against the company name only. Written as the shortest distinctive
# fragment so "Infosys BPM" and "Infosys Ltd" both hit "infosys".
ENTERPRISE_COMPANIES: tuple[str, ...] = (
    # Indian IT services
    "tcs", "tata consultancy", "infosys", "wipro", "hcl", "hcltech",
    "tech mahindra", "cognizant", "capgemini", "accenture", "ltimindtree",
    "l&t infotech", "larsen & toubro", "mindtree", "mphasis", "hexaware",
    "zensar", "birlasoft", "cyient", "persistent systems", "coforge", "niit",
    "virtusa", "ust global", "quest global", "sonata software", "happiest minds",
    "nttdata", "ntt data", "atos", "dxc", "syntel", "iGate", "kpit",
    # BPO / GCC
    "genpact", "concentrix", "wns", "firstsource", "sutherland",
    "teleperformance", "conduent", "exlservice", "hinduja global",
    # Global product / hardware majors
    "ibm", "oracle", "microsoft", "amazon", "google", "meta platforms",
    "apple", "intel", "cisco", "dell", "hewlett", "sap", "salesforce",
    "adobe", "qualcomm", "nvidia", "samsung", "lg electronics", "sony",
    "bosch", "siemens", "philips", "honeywell", "schneider electric", "abb",
    "general electric", "texas instruments", "broadcom", "vmware", "sap labs",
    "paypal", "uber", "netflix", "linkedin", "yahoo", "ericsson", "nokia",
    "juniper networks", "arista networks", "western digital", "micron",
    # Banks / financial services
    "jpmorgan", "jp morgan", "goldman sachs", "morgan stanley", "wells fargo",
    "citibank", "citigroup", "hsbc", "barclays", "deutsche bank",
    "standard chartered", "bank of america", "icici", "hdfc", "axis bank",
    "kotak", "state bank of india", "american express", "visa inc",
    "mastercard", "fidelity investments", "nomura", "ubs",
    # Consulting / audit
    "deloitte", "pwc", "pricewaterhouse", "kpmg", "ernst & young", "ey india",
    "mckinsey", "bain & company", "boston consulting", "zs associates",
    # Large Indian corporates and grown-up unicorns
    "reliance", "adani", "jio", "airtel", "vodafone", "tata elxsi",
    "tata technologies", "mahindra", "l&t technology", "flipkart", "walmart",
    "myntra", "paytm", "ola cabs", "ola electric", "swiggy", "zomato", "byju", "unacademy",
    "phonepe", "freshworks", "zoho", "optum", "unitedhealth", "cvs health",
    "target corporation", "lowe's", "shell india", "thoughtworks",
)

# Matched against the company name: staffing shops and body shops list other
# people's vacancies, so the posting's real employer is unknown.
STAFFING_MARKERS: tuple[str, ...] = (
    "staffing", "manpower", "recruitment", "recruiters", "recruiting",
    "placement", "placements", "hr solutions", "hr services", "hr consulting",
    "hiring solutions", "talent solutions", "talent acquisition", "outsourcing",
    "consultancy", "consultants", "job hub", "career solutions", "workforce",
    "randstad", "adecco", "team lease", "teamlease", "quess corp", "kelly services",
)

# ---------------------------------------------------------------------------
# 2. Phrases a startup uses about itself
# ---------------------------------------------------------------------------

STARTUP_SIGNALS: tuple[str, ...] = (
    "startup", "start-up", "start up", "startups",
    "early stage", "early-stage", "growth stage", "growth-stage",
    "seed funded", "seed-funded", "seed stage", "pre-seed", "pre seed",
    "series a", "series b", "series c", "seed round", "funding round",
    "venture backed", "venture-backed", "vc backed", "vc-backed", "vc funded",
    "angel funded", "angel-funded", "y combinator", "ycombinator",
    "yc backed", "yc-backed", "accel backed", "sequoia backed", "backed by",
    "founding team", "founding engineer", "founding member",
    "co-founders", "cofounders", "our founders", "the founders",
    # "0 to 1" is left out on purpose - it collides with experience ranges
    # ("0 to 1 years"), which appear in exactly the ads this profile targets.
    "zero to one", "first engineering hire",
    "bootstrapped", "stealth mode", "stealth startup",
)

_ENTERPRISE_RE = compile_terms(ENTERPRISE_COMPANIES)
_STAFFING_RE = compile_terms(STAFFING_MARKERS)
_SIGNAL_RE = compile_terms(STARTUP_SIGNALS)

# "startup" is also ordinary sysadmin vocabulary. Scrub those uses before
# looking for the funding-stage sense, or every support engineer ad that says
# "troubleshoot application startup issues" reads as a startup.
_NOISE_RE = re.compile(
    r"(?:system|server|application|app|service|machine|device|boot|cluster|node|"
    r"container|process|program|jvm|os)\s+start[-\s]?up"
    r"|start[-\s]?up\s+(?:script|scripts|time|times|sequence|configuration|config|"
    r"logs?|failures?|issues?|errors?|and\s+shutdown|/\s*shutdown)",
    re.IGNORECASE,
)


def is_enterprise(company: str) -> bool:
    """True for a company that cannot be a startup (major, captive, staffing)."""
    name = (company or "").strip()
    if not name:
        return False
    return bool(_ENTERPRISE_RE.search(name) or _STAFFING_RE.search(name))


def startup_signal(*texts: str) -> str | None:
    """Return the first self-declared startup phrase found, if any."""
    for text in texts:
        if not text:
            continue
        # Descriptions run long; the pitch is always near the top.
        cleaned = _NOISE_RE.sub(" ", text[:6000])
        match = _SIGNAL_RE.search(cleaned)
        if match:
            return match.group(0).lower()
    return None


def is_probable_startup(company: str, description: str = "", title: str = "") -> bool:
    """Best-effort verdict from the company name plus the ad's own words."""
    if is_enterprise(company):
        return False
    return startup_signal(company, title, description) is not None
