"""Tech taxonomy: skills, role detection, categories, seniority, experience.

Every lookup here is word-boundary aware. The original implementation used
naive `substring in text` matching, which meant "ai" matched *Retail* Manager,
"ml" matched *HTML*, and "c" matched essentially every job ever posted. That
both polluted the skill lists and let non-tech roles through the filter.

The boundaries deliberately treat `+`, `#` and `.` as word characters so `c`
does not match inside `c++` and `node` does not match inside `node.js`. A dot is
only a boundary blocker when it joins two tokens (`node.js`), not when it ends a
sentence - otherwise "we use PostgreSQL." would fail to match PostgreSQL.
"""

from __future__ import annotations

import re

_BOUNDARY_L = r"(?<![A-Za-z0-9+#])(?<![A-Za-z0-9]\.)"
_BOUNDARY_R = r"(?![A-Za-z0-9+#])(?!\.[A-Za-z0-9])"


def _compile(terms: object) -> re.Pattern[str]:
    """Compile an alternation of terms, longest-first so the longest wins."""
    unique = sorted({str(t).lower() for t in terms}, key=len, reverse=True)
    if not unique:
        return re.compile(r"(?!x)x")  # never matches
    body = "|".join(re.escape(term) for term in unique)
    return re.compile(f"{_BOUNDARY_L}(?:{body}){_BOUNDARY_R}", re.IGNORECASE)


# =============================================================================
# SKILLS  -  canonical name -> aliases that should map onto it
# =============================================================================

SKILLS: dict[str, list[str]] = {
    # languages
    "Python": ["python", "python3"],
    "Java": ["java", "core java"],
    "JavaScript": ["javascript", "java script", "es6", "ecmascript"],
    "TypeScript": ["typescript", "ts"],
    "C": ["c"],
    "C++": ["c++", "cpp"],
    "C#": ["c#", "csharp", "c sharp"],
    "Go": ["golang", "go lang"],
    "Rust": ["rust"],
    "Ruby": ["ruby"],
    "PHP": ["php"],
    "Swift": ["swift"],
    "Kotlin": ["kotlin"],
    "Scala": ["scala"],
    "R": ["r programming", "r language"],
    "MATLAB": ["matlab"],
    "Perl": ["perl"],
    "Dart": ["dart"],
    "Shell Scripting": ["bash", "shell scripting", "shell script", "powershell", "zsh"],
    "VBA": ["vba", "visual basic"],
    "COBOL": ["cobol"],
    # frontend
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3", "sass", "scss", "less"],
    "React": ["react", "react.js", "reactjs"],
    "Next.js": ["next.js", "nextjs"],
    "Angular": ["angular", "angularjs", "angular.js"],
    "Vue.js": ["vue", "vue.js", "vuejs", "nuxt", "nuxt.js"],
    "Svelte": ["svelte", "sveltekit"],
    "jQuery": ["jquery"],
    "Bootstrap": ["bootstrap"],
    "Tailwind CSS": ["tailwind", "tailwindcss", "tailwind css"],
    "Redux": ["redux"],
    "Webpack": ["webpack", "vite", "rollup"],
    # backend / frameworks
    "Node.js": ["node.js", "nodejs", "node js"],
    "Express.js": ["express", "express.js", "expressjs"],
    "NestJS": ["nestjs", "nest.js"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi", "fast api"],
    "Spring Boot": ["spring boot", "springboot", "spring", "spring mvc"],
    "Hibernate": ["hibernate", "jpa"],
    ".NET": [".net", "dotnet", "asp.net", "asp.net core", ".net core"],
    "Laravel": ["laravel"],
    "Ruby on Rails": ["ruby on rails", "rails"],
    "GraphQL": ["graphql", "apollo"],
    "REST API": ["rest api", "restful", "rest apis", "restful api"],
    "gRPC": ["grpc"],
    "Microservices": ["microservices", "micro services", "micro-services"],
    # data stores
    "SQL": ["sql", "t-sql", "pl/sql", "plsql"],
    "MySQL": ["mysql", "mariadb"],
    "PostgreSQL": ["postgresql", "postgres", "psql"],
    "Oracle DB": ["oracle db", "oracle database", "oracle 11g", "oracle 12c"],
    "SQL Server": ["sql server", "mssql", "ms sql"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Cassandra": ["cassandra"],
    "DynamoDB": ["dynamodb"],
    "Elasticsearch": ["elasticsearch", "opensearch", "elastic search"],
    "Snowflake": ["snowflake"],
    "BigQuery": ["bigquery", "big query"],
    "Redshift": ["redshift"],
    "Neo4j": ["neo4j"],
    "SQLite": ["sqlite"],
    # cloud / infra
    "AWS": ["aws", "amazon web services", "ec2", "s3", "lambda"],
    "Azure": ["azure", "microsoft azure"],
    "GCP": ["gcp", "google cloud", "google cloud platform"],
    "Docker": ["docker", "containerization"],
    "Kubernetes": ["kubernetes", "k8s", "eks", "aks", "gke"],
    "Terraform": ["terraform"],
    "Ansible": ["ansible"],
    "Jenkins": ["jenkins"],
    "GitHub Actions": ["github actions"],
    "GitLab CI": ["gitlab ci", "gitlab-ci"],
    "CI/CD": ["ci/cd", "cicd", "ci cd", "continuous integration"],
    "Linux": ["linux", "ubuntu", "centos", "rhel", "unix"],
    "Nginx": ["nginx", "apache http"],
    "Prometheus": ["prometheus", "grafana"],
    "Kafka": ["kafka", "apache kafka"],
    "RabbitMQ": ["rabbitmq"],
    "Airflow": ["airflow", "apache airflow"],
    "Spark": ["spark", "apache spark", "pyspark"],
    "Hadoop": ["hadoop", "hdfs", "hive"],
    "dbt": ["dbt"],
    "ETL": ["etl", "elt", "data pipeline", "data pipelines"],
    # ai / ml
    "Machine Learning": ["machine learning", "ml"],
    "Deep Learning": ["deep learning", "neural network", "neural networks"],
    "TensorFlow": ["tensorflow", "keras"],
    "PyTorch": ["pytorch", "torch"],
    "Scikit-Learn": ["scikit-learn", "sklearn", "scikit learn"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision", "opencv", "image processing"],
    "LLM": ["llm", "llms", "large language model", "large language models"],
    "Generative AI": ["generative ai", "genai", "gen ai"],
    "LangChain": ["langchain", "langgraph"],
    "LlamaIndex": ["llamaindex", "llama index"],
    "Hugging Face": ["hugging face", "huggingface", "transformers"],
    "RAG": ["rag", "retrieval augmented generation"],
    "OpenAI API": ["openai", "gpt-4", "chatgpt api"],
    "Vector Databases": ["vector database", "vector databases", "pinecone", "chromadb", "weaviate", "faiss"],
    "MLOps": ["mlops", "mlflow", "kubeflow"],
    # data analysis
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    "Power BI": ["power bi", "powerbi"],
    "Tableau": ["tableau"],
    "Looker": ["looker"],
    "Excel": ["advanced excel", "ms excel", "microsoft excel"],
    "Statistics": ["statistics", "statistical analysis", "statistical modeling"],
    # mobile
    "Android": ["android", "android sdk", "jetpack compose"],
    "iOS": ["ios", "swiftui", "objective-c", "xcode"],
    "Flutter": ["flutter"],
    "React Native": ["react native", "react-native"],
    # qa / testing
    "Selenium": ["selenium", "webdriver"],
    "Cypress": ["cypress"],
    "Playwright": ["playwright"],
    "Appium": ["appium"],
    "JUnit": ["junit", "testng"],
    "PyTest": ["pytest"],
    "Jest": ["jest", "vitest"],
    "Manual Testing": ["manual testing", "functional testing", "regression testing"],
    "Automation Testing": ["automation testing", "test automation", "automated testing"],
    "API Testing": ["api testing", "postman", "soapui"],
    "Performance Testing": ["performance testing", "jmeter", "load testing", "loadrunner"],
    # security
    "Cybersecurity": ["cybersecurity", "cyber security", "information security", "infosec"],
    "Penetration Testing": ["penetration testing", "pentesting", "vapt", "ethical hacking"],
    "SIEM": ["siem", "splunk", "qradar"],
    "IAM": ["iam", "identity and access management", "okta"],
    "Network Security": ["network security", "firewall", "ids/ips"],
    # networking / support
    "Networking": ["networking", "tcp/ip", "ccna", "routing and switching", "lan/wan"],
    "Active Directory": ["active directory", "ldap"],
    "VMware": ["vmware", "virtualization", "hyper-v"],
    "ServiceNow": ["servicenow", "service now"],
    "ITIL": ["itil"],
    # enterprise
    "SAP": ["sap", "sap abap", "sap hana", "sap mm", "sap fico"],
    "Salesforce": ["salesforce", "apex", "sfdc"],
    "ServiceMax": ["servicemax"],
    "Dynamics 365": ["dynamics 365", "msd365"],
    "SharePoint": ["sharepoint"],
    # tooling / practice
    "Git": ["git", "github", "gitlab", "bitbucket", "version control"],
    "Agile": ["agile", "scrum", "kanban", "safe agile"],
    "JIRA": ["jira", "confluence"],
    "Data Structures": ["data structures", "dsa", "algorithms"],
    "OOP": ["oop", "object oriented programming", "object-oriented"],
    "System Design": ["system design", "low level design", "high level design"],
    "Blockchain": ["blockchain", "solidity", "web3", "smart contract", "smart contracts"],
    "Unity": ["unity", "unity3d", "unreal engine"],
    "Embedded C": ["embedded c", "embedded systems", "rtos", "firmware"],
    "VLSI": ["vlsi", "verilog", "vhdl", "fpga"],
    "IoT": ["iot", "internet of things", "arduino", "raspberry pi"],
    "AutoCAD": ["autocad"],
    "Figma": ["figma", "adobe xd", "sketch"],
    "UI/UX": ["ui/ux", "user experience", "user interface design", "wireframing", "prototyping"],
    "SEO": ["seo", "search engine optimization"],
}

_ALIAS_TO_SKILL: dict[str, str] = {
    alias.lower(): canonical
    for canonical, aliases in SKILLS.items()
    for alias in aliases
}
_SKILL_RE = _compile(_ALIAS_TO_SKILL)


def extract_skills(*texts: str, limit: int = 40) -> list[str]:
    """Return canonical skill names found in any of `texts`."""
    blob = " \n ".join(t for t in texts if t)
    if not blob:
        return []
    found = {
        _ALIAS_TO_SKILL[match.group(0).lower()]
        for match in _SKILL_RE.finditer(blob)
    }
    return sorted(found)[:limit]


# =============================================================================
# CATEGORIES
# =============================================================================

CATEGORIES: dict[str, list[str]] = {
    "Full Stack": [
        "full stack", "fullstack", "full-stack", "mern", "mean stack", "mern stack",
    ],
    "Backend": [
        "backend", "back end", "back-end", "server side", "server-side",
        "api developer", "django", "flask", "fastapi", "node.js", "nodejs",
        "spring boot", "express.js", "microservices", "rest api", "graphql",
        "php developer", "laravel", ".net developer", "golang developer",
    ],
    "Frontend": [
        "frontend", "front end", "front-end", "ui developer", "web developer",
        "react", "reactjs", "angular", "vue", "javascript developer",
        "typescript developer", "html", "css", "tailwind", "next.js",
    ],
    "Mobile Development": [
        "mobile developer", "android developer", "ios developer", "flutter",
        "react native", "kotlin", "swift", "swiftui", "mobile application",
        "android", "ios", "app developer",
    ],
    "Data Science / AI-ML": [
        "data scientist", "data science", "machine learning", "deep learning",
        "artificial intelligence", "ai engineer", "ml engineer", "nlp",
        "computer vision", "tensorflow", "pytorch", "generative ai", "genai",
        "llm", "large language model", "langchain", "rag", "hugging face",
        "mlops", "research scientist", "applied scientist", "scikit-learn",
    ],
    "Data Engineering / Analytics": [
        "data engineer", "data analyst", "analytics engineer", "bi developer",
        "business intelligence", "etl", "elt", "data warehouse", "data pipeline",
        "power bi", "tableau", "looker", "snowflake", "bigquery", "redshift",
        "airflow", "spark", "pyspark", "hadoop", "dbt", "reporting analyst",
    ],
    "DevOps / Cloud / SRE": [
        "devops", "site reliability", "sre", "cloud engineer", "platform engineer",
        "infrastructure engineer", "kubernetes", "docker", "terraform", "ansible",
        "jenkins", "ci/cd", "aws", "azure", "gcp", "cloud architect",
        "build and release", "observability",
    ],
    "QA / Testing": [
        "qa engineer", "quality assurance", "test engineer", "sdet", "tester",
        "automation testing", "manual testing", "selenium", "cypress", "appium",
        "test automation", "qa analyst", "quality engineer", "performance testing",
    ],
    "Cybersecurity": [
        "security engineer", "cybersecurity", "cyber security", "information security",
        "soc analyst", "penetration testing", "pentester", "vapt", "ethical hacking",
        "security analyst", "appsec", "grc analyst", "siem", "threat",
    ],
    "IT Support / Infrastructure": [
        "it support", "technical support", "service desk", "help desk", "helpdesk",
        "desktop support", "system administrator", "systems administrator", "sysadmin",
        "it administrator", "noc engineer", "application support", "production support",
        "it operations", "vmware", "active directory", "itil",
    ],
    "Networking": [
        "network engineer", "network administrator", "ccna", "ccnp", "routing and switching",
        "network operations", "telecom engineer", "wireless engineer",
    ],
    "Database / DBA": [
        "database administrator", "dba", "database developer", "sql developer",
        "pl/sql", "oracle dba", "database engineer",
    ],
    "Embedded / Hardware": [
        "embedded", "firmware", "rtos", "vlsi", "verilog", "vhdl", "fpga",
        "hardware engineer", "iot", "device driver", "embedded c",
    ],
    "Game Development": [
        "game developer", "game programmer", "unity developer", "unreal",
        "game design", "unity3d",
    ],
    "Blockchain / Web3": [
        "blockchain", "web3", "solidity", "smart contract", "defi", "dapp",
    ],
    "UI/UX Design": [
        "ux designer", "ui designer", "ui/ux", "product designer", "interaction designer",
        "user experience", "figma", "wireframing",
    ],
    "ERP / CRM": [
        "sap", "salesforce", "abap", "sap hana", "dynamics 365", "oracle apps",
        "peoplesoft", "workday", "servicenow developer", "sfdc",
    ],
    "Product / Program (Tech)": [
        "product manager", "technical product", "product analyst", "program manager",
        "scrum master", "project manager", "technical program",
    ],
    "Business / Systems Analyst": [
        "business analyst", "systems analyst", "functional consultant",
        "requirements analyst", "process analyst",
    ],
    "Software Engineering": [
        "software engineer", "software developer", "programmer analyst",
        "application developer", "software development engineer", "sde",
        "member of technical staff", "systems engineer",
        "programmer", "technical consultant", "software architect",
    ],
}

_KEYWORD_TO_CATEGORY: dict[str, list[str]] = {}
for _cat, _kws in CATEGORIES.items():
    for _kw in _kws:
        _KEYWORD_TO_CATEGORY.setdefault(_kw.lower(), []).append(_cat)
_CATEGORY_RE = _compile(_KEYWORD_TO_CATEGORY)

# Specific beats generic when scores tie.
_CATEGORY_PRIORITY = {
    name: index
    for index, name in enumerate(
        [
            "Full Stack", "Data Science / AI-ML", "Data Engineering / Analytics",
            "DevOps / Cloud / SRE", "Cybersecurity", "Blockchain / Web3",
            "Game Development", "Embedded / Hardware", "Mobile Development",
            "QA / Testing", "ERP / CRM", "Database / DBA", "Networking",
            "UI/UX Design", "Business / Systems Analyst", "Product / Program (Tech)",
            "IT Support / Infrastructure", "Backend", "Frontend",
            "Software Engineering",
        ]
    )
}

TITLE_WEIGHT = 5
SKILL_WEIGHT = 2
DESC_WEIGHT = 1


def _score_categories(text: str, weight: int, scores: dict[str, float]) -> None:
    if not text:
        return
    for match in _CATEGORY_RE.finditer(text):
        for category in _KEYWORD_TO_CATEGORY[match.group(0).lower()]:
            scores[category] = scores.get(category, 0) + weight


def categorize(title: str, description: str = "", skills: list[str] | None = None) -> str:
    """Pick the single best category. Title signal dominates description noise."""
    scores: dict[str, float] = {}
    _score_categories(title, TITLE_WEIGHT, scores)
    _score_categories(" ".join(skills or []), SKILL_WEIGHT, scores)
    # Descriptions list every buzzword under the sun; cap their influence.
    _score_categories(description[:4000], DESC_WEIGHT, scores)

    if not scores:
        return "Other"

    # A role that clearly touches both ends of the stack is Full Stack.
    if scores.get("Backend", 0) >= TITLE_WEIGHT and scores.get("Frontend", 0) >= TITLE_WEIGHT:
        return "Full Stack"

    best = max(scores.items(), key=lambda kv: (kv[1], -_CATEGORY_PRIORITY.get(kv[0], 99)))
    return best[0] if best[1] > 0 else "Other"


def secondary_categories(title: str, description: str = "", skills: list[str] | None = None,
                         limit: int = 3) -> list[str]:
    """Other categories the posting also touches - useful for facet filtering."""
    scores: dict[str, float] = {}
    _score_categories(title, TITLE_WEIGHT, scores)
    _score_categories(" ".join(skills or []), SKILL_WEIGHT, scores)
    _score_categories(description[:4000], DESC_WEIGHT, scores)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [name for name, score in ranked[:limit] if score >= SKILL_WEIGHT]


# =============================================================================
# TECH / NON-TECH CLASSIFICATION
# =============================================================================

# Terms that on their own prove the role is technical.
STRONG_TECH_TERMS = [
    "software engineer", "software developer", "software development",
    "web developer", "app developer", "application developer", "mobile developer",
    "backend", "back end", "back-end", "frontend", "front end", "front-end",
    "full stack", "fullstack", "full-stack", "sde", "sde-1", "sde 1",
    "programmer", "coder", "devops", "sre", "site reliability", "platform engineer",
    "cloud engineer", "cloud architect", "solution architect", "software architect",
    "data scientist", "data engineer", "data analyst", "analytics engineer",
    "machine learning", "deep learning", "artificial intelligence", "ai engineer",
    "ml engineer", "mlops", "nlp engineer", "computer vision", "research engineer",
    "generative ai", "genai", "llm engineer", "prompt engineer",
    "qa engineer", "quality assurance", "test engineer", "sdet", "automation engineer",
    "automation tester", "test analyst", "quality engineer",
    "database administrator", "dba", "database developer", "sql developer",
    "network engineer", "network administrator", "system administrator",
    "systems administrator", "sysadmin", "it support", "technical support",
    "service desk", "help desk", "helpdesk", "desktop support",
    "application support", "production support", "it operations", "noc engineer",
    "security engineer", "security analyst", "cyber security", "cybersecurity",
    "information security", "soc analyst", "penetration tester", "ethical hacker",
    "embedded engineer", "firmware engineer", "vlsi", "verilog", "fpga",
    "game developer", "game programmer", "unity developer",
    "blockchain developer", "web3 developer", "solidity developer",
    "ux designer", "ui designer", "ui/ux designer", "product designer",
    "scrum master", "technical writer", "technical consultant",
    "salesforce developer", "sap consultant", "abap", "servicenow developer",
    # "graduate engineer trainee" (GET) is deliberately absent: in India it is a
    # discipline-agnostic fresher title used by mechanical, civil and electrical
    # employers too. As a strong term it bypassed the non-tech veto, admitting
    # AutoCAD draughting roles and labelling them Software Engineering. It now
    # falls through to the generic-role path, which requires a tech qualifier or
    # a genuinely technical skill list.
    "member of technical staff", "technology analyst",
    "systems engineer", "support engineer", "implementation engineer",
    "integration engineer", "middleware", "mainframe", "etl developer",
    "bi developer", "business intelligence", "power bi developer", "tableau developer",
    "android developer", "ios developer", "flutter developer", "react native developer",
    "python developer", "java developer", "javascript developer", "react developer",
    "angular developer", "node.js developer", "golang developer", ".net developer",
    "php developer", "ruby developer", "c++ developer", "c# developer",
    "computer science", "information technology",
]

# Roles that are technical only when paired with a tech qualifier.
GENERIC_ROLE_TERMS = [
    "engineer", "developer", "analyst", "consultant", "architect", "administrator",
    "specialist", "associate", "trainee", "intern", "internship", "technician",
    "executive", "officer", "manager", "lead", "scientist", "researcher",
]

# Qualifiers that make a generic role technical.
TECH_QUALIFIER_TERMS = [
    "software", "technology", "technical", "tech", "it", "computer", "web",
    "application", "applications", "systems", "system", "data", "cloud", "digital",
    "platform", "product", "api", "automation", "infrastructure", "network",
    "security", "database", "server", "devops", "mobile", "ai", "ml",
    "python", "java", "javascript", "typescript", "react", "angular", "vue",
    "node.js", "nodejs", ".net", "c++", "c#", "golang", "php", "ruby", "kotlin",
    "swift", "flutter", "django", "flask", "spring", "sql", "aws", "azure", "gcp",
    "kubernetes", "docker", "linux", "sap", "salesforce", "oracle", "erp", "crm",
]

# Non-tech roles. These veto a match unless a strong tech term also appears.
NON_TECH_TERMS = [
    "sales", "presales", "business development", "bdm", "bde", "telecaller",
    "telesales", "telemarketing", "marketing", "digital marketing", "seo executive",
    "content writer", "copywriter", "social media",
    "hr", "human resource", "human resources", "recruiter", "recruitment",
    "talent acquisition", "payroll",
    "accountant", "accounts", "accounting", "finance", "financial", "audit",
    "auditor", "taxation", "chartered accountant", "bookkeeping", "billing",
    "insurance", "loan", "banking operations", "relationship manager",
    "customer service", "customer care", "customer support executive",
    "bpo", "kpo", "voice process", "non voice process", "call center", "call centre",
    "back office", "data entry", "typist",
    "nurse", "nursing", "doctor", "physician", "pharmacist", "medical",
    "teacher", "tutor", "faculty", "lecturer", "professor",
    "civil engineer", "mechanical engineer", "electrical engineer",
    "chemical engineer", "production engineer", "quality inspector",
    "site engineer", "maintenance engineer", "design engineer autocad",
    "supply chain", "logistics", "warehouse", "procurement", "purchase",
    "driver", "security guard", "housekeeping", "chef", "receptionist",
    "legal", "advocate", "paralegal", "real estate", "interior designer",
    "fashion designer", "graphic designer", "video editor", "photographer",
]

_STRONG_TECH_RE = _compile(STRONG_TECH_TERMS)
_GENERIC_ROLE_RE = _compile(GENERIC_ROLE_TERMS)
_TECH_QUALIFIER_RE = _compile(TECH_QUALIFIER_TERMS)
_NON_TECH_RE = _compile(NON_TECH_TERMS)


def has_strong_tech_title(title: str) -> bool:
    """True when the title alone proves the role is technical.

    Lets callers keep a posting that arrived without a description - common for
    LinkedIn search cards - instead of discarding it for having no body text.
    """
    return bool(_STRONG_TECH_RE.search(title or ""))


def is_tech_job(title: str, description: str = "", skills: list[str] | None = None) -> bool:
    """Decide whether a posting is an IT/tech role.

    Layered so that broad IT roles (support, DBA, network, security, ERP) are
    captured too - not just the handful of dev titles the old keyword list had.
    """
    title = (title or "").strip()
    if not title:
        return False

    skills = skills or []
    strong_in_title = bool(_STRONG_TECH_RE.search(title))

    # A non-tech title wins unless the title itself is explicitly technical.
    if _NON_TECH_RE.search(title) and not strong_in_title:
        return False

    if strong_in_title:
        return True

    # Generic role + technical qualifier, e.g. "Associate - Cloud Operations".
    if _GENERIC_ROLE_RE.search(title) and _TECH_QUALIFIER_RE.search(title):
        return True

    # Generic role whose skill list is unmistakably technical.
    if _GENERIC_ROLE_RE.search(title) and len(skills) >= 2:
        return True

    # Last resort: the description is dense with technology.
    if len(skills) >= 4 and _STRONG_TECH_RE.search(description or ""):
        return True

    return False


# =============================================================================
# SENIORITY + EXPERIENCE
# =============================================================================

SENIORITY_LEVELS = ["intern", "fresher", "junior", "mid", "senior", "lead"]

_INTERN_RE = _compile(["intern", "internship", "summer intern", "apprentice", "apprenticeship"])
_FRESHER_RE = _compile([
    "fresher", "freshers", "entry level", "entry-level", "graduate", "new graduate",
    "campus hire", "trainee", "graduate engineer trainee", "no experience",
    "0 experience", "zero experience", "college graduate", "recent graduate",
])
# "get" (the GET acronym) is intentionally not above: it is an ordinary English
# verb, so it matched titles like "Get Started Program" and forced them to
# fresher. "trainee" and "graduate engineer trainee" already cover the real case.
_JUNIOR_RE = _compile(["junior", "jr", "associate", "sde 1", "sde-1", "sde i", "level 1"])
_SENIOR_RE = _compile(["senior", "sr", "sde 3", "sde-3", "staff", "principal", "specialist iv"])
_LEAD_RE = _compile([
    "lead", "tech lead", "team lead", "manager", "head of", "director",
    "architect", "vp", "chief",
])

# "0-3 Yrs", "2 to 5 years", "1 - 4 year"
_EXP_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:yr|yrs|year|years)?",
    re.IGNORECASE,
)
# "3+ years", "minimum 2 years"
_EXP_SINGLE_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:yr|yrs|year|years)",
    re.IGNORECASE,
)


def parse_experience(text: str) -> tuple[int | None, int | None]:
    """Parse an experience string into (min_years, max_years)."""
    if not text:
        return (None, None)
    lowered = text.lower()

    if _INTERN_RE.search(lowered) or _FRESHER_RE.search(lowered):
        # Still try for an explicit range, e.g. "Fresher (0-1 yrs)".
        match = _EXP_RANGE_RE.search(lowered)
        if match:
            return (int(match.group(1)), int(match.group(2)))
        return (0, 1)

    match = _EXP_RANGE_RE.search(lowered)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return (min(low, high), max(low, high))

    match = _EXP_SINGLE_RE.search(lowered)
    if match:
        years = int(match.group(1))
        return (years, years + 2 if "+" in lowered else years)

    return (None, None)


def detect_seniority(title: str, experience_text: str = "",
                     min_years: int | None = None) -> str:
    """Classify seniority from title, then experience text, then years."""
    blob = f"{title} {experience_text}"

    if _INTERN_RE.search(title):
        return "intern"
    if _LEAD_RE.search(title):
        return "lead"
    if _SENIOR_RE.search(title):
        return "senior"
    if _FRESHER_RE.search(blob):
        return "fresher"
    if _JUNIOR_RE.search(title):
        return "junior"

    if min_years is not None:
        if min_years == 0:
            return "fresher"
        if min_years <= 2:
            return "junior"
        if min_years <= 5:
            return "mid"
        if min_years <= 8:
            return "senior"
        return "lead"

    return "mid"


_REMOTE_RE = _compile(["remote", "work from home", "wfh", "anywhere", "fully remote"])
_HYBRID_RE = _compile(["hybrid"])


def detect_work_mode(location: str, title: str = "", description: str = "") -> str:
    """Return one of: remote | hybrid | onsite."""
    blob = f"{location} {title} {description[:600]}"
    if _HYBRID_RE.search(blob):
        return "hybrid"
    if _REMOTE_RE.search(blob):
        return "remote"
    return "onsite"


# =============================================================================
# SEARCH QUERY CATALOGUE  -  drives coverage
# =============================================================================

# Grouped so a partial run still spans the whole tech space rather than
# exhausting one niche. Roughly 90 queries; each returns up to ~300 postings.
SEARCH_QUERIES: list[str] = [
    # core software
    "software engineer", "software developer", "software development engineer",
    "associate software engineer", "graduate engineer trainee", "programmer analyst",
    "application developer", "systems engineer", "technology analyst",
    # languages
    "python developer", "java developer", "javascript developer",
    "typescript developer", "golang developer", "c++ developer", "c# developer",
    "dot net developer", "php developer", "ruby on rails developer",
    "kotlin developer", "scala developer",
    # web
    "backend developer", "frontend developer", "full stack developer",
    "react developer", "angular developer", "vue developer", "node js developer",
    "django developer", "spring boot developer", "web developer",
    "mern stack developer", "api developer",
    # mobile
    "android developer", "ios developer", "flutter developer",
    "react native developer", "mobile application developer",
    # data / ai
    "data analyst", "data scientist", "data engineer", "analytics engineer",
    "business intelligence developer", "power bi developer", "tableau developer",
    "machine learning engineer", "ai engineer", "deep learning engineer",
    "nlp engineer", "computer vision engineer", "mlops engineer",
    "generative ai engineer", "llm engineer", "research engineer",
    "etl developer", "big data engineer", "data warehouse engineer",
    # cloud / devops
    "devops engineer", "cloud engineer", "site reliability engineer",
    "platform engineer", "aws engineer", "azure engineer", "kubernetes engineer",
    "infrastructure engineer", "build and release engineer",
    # qa
    "qa engineer", "quality assurance engineer", "test engineer", "sdet",
    "automation test engineer", "manual testing engineer", "performance test engineer",
    # security
    "security engineer", "cyber security analyst", "soc analyst",
    "information security analyst", "penetration tester", "application security engineer",
    # infra / support / network
    "system administrator", "network engineer", "it support engineer",
    "technical support engineer", "application support engineer",
    "production support engineer", "service desk analyst", "desktop support engineer",
    "noc engineer", "linux administrator", "vmware administrator",
    # database
    "database administrator", "sql developer", "oracle dba", "database engineer",
    # enterprise
    "sap consultant", "sap abap developer", "salesforce developer",
    "servicenow developer", "oracle apps technical consultant", "workday consultant",
    # specialised
    "embedded software engineer", "firmware engineer", "vlsi design engineer",
    "iot engineer", "game developer", "unity developer", "blockchain developer",
    "solidity developer",
    # adjacent tech roles
    "business analyst", "systems analyst", "technical product manager",
    "scrum master", "ui ux designer", "product designer", "technical writer",
    # internships
    "software engineer intern", "data science intern", "web development intern",
    "machine learning intern", "developer intern",
]
