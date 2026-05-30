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
    "Deep Learning",
    "Data Science",

    "OpenAI",
    "Gemini",
    "LangChain",
    "LlamaIndex",
    "Hugging Face",
    "RAG",
    "LLM",
    "Generative AI",

    "Git",
    "Linux",
    "CI/CD",

    "Power BI",
    "Tableau",
    "Pandas",
    "NumPy",
    "Scikit-Learn",

    "Android",
    "Flutter",
    "React Native"
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
    "data analyst",
    "data scientist",

    "machine learning",
    "deep learning",

    "ai",
    "ml",
    "artificial intelligence",

    "genai",
    "generative ai",
    "llm",

    "cloud",
    "devops",

    "analyst",
    "programmer",

    "intern",
    "trainee",

    "python",
    "react",
    "java",

    "research engineer",
    "ai engineer",
    "ml engineer"
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
    "entry-level",

    "graduate",
    "new graduate",

    "intern",
    "internship",

    "trainee",

    "junior",

    "associate engineer",
    "associate developer"
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

    return sorted(
        list(
            set(found_skills)
        )
    )


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