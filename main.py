# main.py

from playwright.sync_api import sync_playwright

from extractor import (
    extract_job_cards,
    extract_job_description,
    extract_linkedin_jobs,
    extract_linkedin_description
)

from parser import (
    extract_skills,
    is_tech_job,
    is_fresher_job
)

from categorizer import categorize_job

from cleanup import is_dead_job

from datetime import datetime

import json
import time
import random


# =========================================
# LOAD EXISTING JOBS
# =========================================

jobs_data = []

existing_jobs = set()

try:

    with open(
        "output/jobs.json",
        "r",
        encoding="utf-8"
    ) as f:

        jobs_data = json.load(f)

        for job in jobs_data:

            unique_key = (
                job["title"].lower().strip() +
                job["company"].lower().strip()
            )

            existing_jobs.add(unique_key)

except:

    jobs_data = []


# =========================================
# SEARCH URLS
# =========================================

NAUKRI_URLS = [

    "https://www.naukri.com/fresher-software-engineer-jobs",
    "https://www.naukri.com/fresher-software-developer-jobs",
    "https://www.naukri.com/fresher-python-developer-jobs",
    "https://www.naukri.com/fresher-java-developer-jobs",
    "https://www.naukri.com/fresher-full-stack-developer-jobs",
    "https://www.naukri.com/fresher-react-developer-jobs",
    "https://www.naukri.com/fresher-data-analyst-jobs",
    "https://www.naukri.com/fresher-data-scientist-jobs",
    "https://www.naukri.com/fresher-machine-learning-engineer-jobs",
    "https://www.naukri.com/fresher-ai-engineer-jobs",
    "https://www.naukri.com/fresher-devops-engineer-jobs",
    "https://www.naukri.com/fresher-cloud-engineer-jobs",
    "https://www.naukri.com/fresher-qa-engineer-jobs"

]

LINKEDIN_SEARCHES = [

    "software engineer",
    "software developer",

    "associate software engineer",

    "python developer",
    "java developer",

    "backend developer",
    "frontend developer",

    "react developer",

    "full stack developer",

    "data analyst",
    "business analyst",

    "data scientist",

    "machine learning engineer",
    "ai engineer",

    "cloud engineer",

    "qa engineer",
    "test engineer",

    "application support engineer",

    "graduate engineer trainee",

    "software engineer intern",
    "developer intern"

]


# =========================================
# START PLAYWRIGHT
# =========================================

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context(

        user_agent=(
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),

        viewport={
            "width": 1366,
            "height": 768
        }
    )

    page = context.new_page()

# =========================================
# NAUKRI SCRAPING
# =========================================

    for NAUKRI_URL in NAUKRI_URLS:

        try:

            print(
                f"\nOpening Naukri: "
                f"{NAUKRI_URL}"
            )

            page.goto(
                NAUKRI_URL,
                timeout=60000
            )

            time.sleep(8)

            page.mouse.wheel(
                0,
                4000
            )

            time.sleep(3)

            naukri_html = page.content()

            naukri_jobs = extract_job_cards(
                naukri_html
            )

            print(
                f"Naukri Jobs Found: "
                f"{len(naukri_jobs)}"
            )

            for job in naukri_jobs:

                try:

                    if not job["apply_link"]:
                        continue

                    if not is_tech_job(
                        job["title"]
                    ):
                        continue

                    if not is_fresher_job(
                        job["title"],
                        job["experience"]
                    ):
                        continue

                    unique_key = (
                        job["title"].lower().strip()
                        +
                        job["company"].lower().strip()
                    )

                    if unique_key in existing_jobs:

                        continue

                    print(
                        f"Naukri Opening: "
                        f"{job['title']}"
                    )

                    time.sleep(
                        random.uniform(2, 5)
                    )

                    job_page = context.new_page()

                    try:

                        job_page.goto(
                            job["apply_link"],
                            timeout=60000
                        )

                        time.sleep(5)

                        job_html = (
                            job_page.content()
                        )

                        if is_dead_job(
                            job_html
                        ):

                            continue

                        description = (
                            extract_job_description(
                                job_html
                            )
                        )

                        skills = (
                            extract_skills(
                                description
                            )
                        )

                        category = (
                            categorize_job(
                                job["title"],
                                description
                            )
                        )

                        job["description"] = (
                            description
                        )

                        job["skills"] = skills

                        job["category"] = (
                            category
                        )

                        job["source"] = (
                            "naukri"
                        )

                        job["scraped_at"] = str(
                            datetime.now()
                        )

                        jobs_data.append(
                            job
                        )

                        existing_jobs.add(
                            unique_key
                        )

                        with open(
                            "output/jobs.json",
                            "w",
                            encoding="utf-8"
                        ) as f:

                            json.dump(
                                jobs_data,
                                f,
                                indent=4,
                                ensure_ascii=False
                            )
                        print(
                            f"Saved Naukri: "
                            f"{job['title']} | "
                            f"{category}"
                        )

                    finally:

                        job_page.close()

                except Exception as e:

                    print(
                        f"Naukri Job Error: "
                        f"{e}"
                    )

        except Exception as e:

            print(
                f"Naukri Search Error: "
                f"{e}"
            )

    # =========================================
    # LINKEDIN SCRAPING
    # =========================================

    for search_term in LINKEDIN_SEARCHES:

        try:

            linkedin_url = (
                "https://www.linkedin.com/jobs/search/"
                f"?keywords={search_term.replace(' ', '%20')}"
                "&location=India"
                "&geoId=102713980"
                "&f_E=2"
            )

            print(
                f"\nSearching LinkedIn: "
                f"{search_term}"
            )

            page.goto(
                linkedin_url,
                timeout=60000
            )

            time.sleep(8)

            for _ in range(5):

                page.mouse.wheel(
                    0,
                    3000
                )

                time.sleep(2)

            linkedin_html = page.content()

            linkedin_jobs = (
                extract_linkedin_jobs(
                    linkedin_html
                )
            )

            print(
                f"LinkedIn Jobs Found: "
                f"{len(linkedin_jobs)}"
            )

            for job in linkedin_jobs:

                try:

                    if not job["apply_link"]:
                        continue

                    if not is_tech_job(
                        job["title"]
                    ):
                        continue
                    if (
                        job["location"]
                        and
                        "india" not in job["location"].lower()
                        ):
                        continue

                    if not is_fresher_job(
                        job["title"],
                        job["experience"]
                    ):
                        continue

                    unique_key = (
                        job["title"].lower().strip()
                        +
                        job["company"].lower().strip()
                    )

                    if unique_key in existing_jobs:

                        continue

                    print(
                        f"LinkedIn Opening: "
                        f"{job['title']}"
                    )

                    job_page = context.new_page()

                    try:

                        job_page.goto(
                            job["apply_link"],
                            timeout=60000
                        )

                        time.sleep(5)

                        job_html = (
                            job_page.content()
                        )

                        if is_dead_job(
                            job_html
                        ):

                            continue

                        description = (
                            extract_linkedin_description(
                                job_html
                            )
                        )

                        skills = (
                            extract_skills(
                                description
                            )
                        )

                        category = (
                            categorize_job(
                                job["title"],
                                description
                            )
                        )

                        job["description"] = (
                            description
                        )

                        job["skills"] = skills

                        job["category"] = (
                            category
                        )

                        job["source"] = (
                            "linkedin"
                        )

                        job["scraped_at"] = str(
                            datetime.now()
                        )

                        jobs_data.append(
                            job
                        )

                        existing_jobs.add(
                            unique_key
                        )
                        with open(
                            "output/jobs.json",
                            "w",
                            encoding="utf-8"
                        ) as f:

                            json.dump(
                                jobs_data,
                                f,
                                indent=4,
                                ensure_ascii=False
                            )

                        print(
                            f"Saved LinkedIn: "
                            f"{job['title']} | "
                            f"{job['location']} | "
                            f"{category}"
                        )

                    finally:

                        job_page.close()

                except Exception as e:

                    print(
                        f"LinkedIn Job Error: "
                        f"{e}"
                    )

        except Exception as e:

            print(
                f"LinkedIn Search Error: "
                f"{e}")


    # =========================================
    # SAVE FINAL JSON
    # =========================================

    with open(
        "output/jobs.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            jobs_data,
            f,
            indent=4,
            ensure_ascii=False
        )

    print(
        f"\nSuccessfully saved "
        f"{len(jobs_data)} total jobs"
    )