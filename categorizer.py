# =========================================
# CATEGORY DATABASE
# =========================================

CATEGORIES = {

    "Backend": [
        "python",
        "django",
        "flask",
        "fastapi",
        "node.js",
        "backend",
        "api",
        "java",
        "spring",
        "express"
    ],

    "Frontend": [
        "react",
        "angular",
        "vue",
        "frontend",
        "javascript",
        "typescript",
        "ui",
        "html",
        "css"
    ],

    "Full Stack": [
        "full stack",
        "fullstack"
    ],

    "Data Science / AI": [
        "data science",
        "machine learning",
        "deep learning",
        "tensorflow",
        "pytorch",
        "data analyst",
        "data scientist",
        "ai",
        "ml",
        "artificial intelligence",
        "llm",
        "large language model",
        "generative ai",
        "genai",
        "langchain",
        "rag",
        "ollama",
        "openai",
        "gemini",
        "vector database",
        "hugging face"
    ],

    "DevOps / Cloud": [
        "docker",
        "kubernetes",
        "aws",
        "azure",
        "gcp",
        "devops",
        "cloud",
        "terraform",
        "jenkins",
        "ci/cd"
    ],

    "Mobile Development": [
        "android",
        "ios",
        "flutter",
        "react native",
        "kotlin",
        "swift"
    ]
}


# =========================================
# CATEGORY DETECTOR
# =========================================

def categorize_job(title, description):

    combined = (
        title + " " + description
    ).lower()

    scores = {}

    for category, keywords in CATEGORIES.items():

        score = 0

        for keyword in keywords:

            if keyword in combined:
                score += 1

        scores[category] = score

    best_category = max(
        scores,
        key=scores.get
    )

    if scores[best_category] == 0:
        return "Other"

    return best_category