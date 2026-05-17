# categorizer.py

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
        "spring"
    ],

    "Frontend": [

        "react",
        "angular",
        "vue",
        "frontend",
        "javascript",
        "typescript",
        "ui"
    ],

    "Full Stack": [

        "full stack"
    ],

    "Data Science": [

        "data science",
        "machine learning",
        "deep learning",
        "tensorflow",
        "pytorch",
        "data analyst",
        "ai",
        "ml"
    ],

    "DevOps": [

        "docker",
        "kubernetes",
        "aws",
        "azure",
        "gcp",
        "devops",
        "cloud"
    ],

    "Mobile Development": [

        "android",
        "ios",
        "flutter",
        "react native"
    ]
}


# =========================================
# CATEGORY DETECTOR
# =========================================

def categorize_job(title, description):

    combined = (
        title + " " + description
    ).lower()

    for category, keywords in CATEGORIES.items():

        for keyword in keywords:

            if keyword in combined:
                return category

    return "Other"