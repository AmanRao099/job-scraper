# parser.py

import re


# =========================================
# SKILL DATABASE
# =========================================

SKILLS = [

    "Python",
    "Java",
    "C++",
    "C",
    "JavaScript",
    "TypeScript",

    "React",
    "Angular",
    "Vue",

    "Node.js",
    "Express",

    "Django",
    "Flask",
    "FastAPI",

    "Spring Boot",

    "SQL",
    "MySQL",
    "PostgreSQL",
    "MongoDB",
    "Redis",

    "AWS",
    "Azure",
    "GCP",

    "Docker",
    "Kubernetes",

    "TensorFlow",
    "PyTorch",

    "Machine Learning",
    "Data Science",

    "Git",
    "Linux",
    "CI/CD"
]


# =========================================
# TECH FILTER
# =========================================

TECH_KEYWORDS = [

    "developer",
    "engineer",
    "software",
    "frontend",
    "backend",
    "full stack",
    "data",
    "ai",
    "ml",
    "cloud",
    "devops",
    "analyst",
    "programmer",
    "intern",
    "trainee"
]


# =========================================
# FRESHER FILTER
# =========================================

FRESHER_KEYWORDS = [

    "fresher",
    "freshers",
    "0-1",
    "0 - 1",
    "entry level",
    "graduate",
    "intern",
    "trainee",
    "junior"
]


# =========================================
# EXTRACT SKILLS
# =========================================

def extract_skills(description):

    found_skills = []

    description_lower = description.lower()

    for skill in SKILLS:

        if skill.lower() in description_lower:

            found_skills.append(skill)

    return list(set(found_skills))


# =========================================
# CHECK TECH JOB
# =========================================

def is_tech_job(title):

    title_lower = title.lower()

    for keyword in TECH_KEYWORDS:

        if keyword in title_lower:
            return True

    return False


# =========================================
# CHECK FRESHER JOB
# =========================================

def is_fresher_job(title, experience):

    combined = (
        f"{title} {experience}"
    ).lower()

    for keyword in FRESHER_KEYWORDS:

        if keyword in combined:
            return True

    exp_match = re.search(
        r'0\s*-\s*1',
        combined
    )

    if exp_match:
        return True

    return False