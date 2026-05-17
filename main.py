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

NAUKRI_URL = (
    "https://www.naukri.com/"
    "fresher-software-engineer-jobs"
)

LINKEDIN_URL = (
    "https://www.linkedin.com/jobs/search/"
    "?keywords=fresher%20software%20engineer"
)


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

    print("\nOpening Naukri...\n")

    page.goto(
        NAUKRI_URL,
        timeout=60000
    )

    time.sleep(8)

    page.mouse.wheel(0, 4000)

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

            if not is_tech_job(job["title"]):
                continue

            if not is_fresher_job(
                job["title"],
                job["experience"]
            ):
                continue

            unique_key = (
                job["title"].lower().strip() +
                job["company"].lower().strip()
            )

            if unique_key in existing_jobs:

                print(
                    f"Duplicate Skipped: "
                    f"{job['title']}"
                )

                continue

            print(
                f"Naukri Opening: "
                f"{job['title']}"
            )

            time.sleep(
                random.uniform(2, 5)
            )

            job_page = context.new_page()

            job_page.goto(
                job["apply_link"],
                timeout=60000
            )

            time.sleep(5)

            job_html = job_page.content()

            if is_dead_job(job_html):

                print(
                    f"Dead Job Skipped: "
                    f"{job['title']}"
                )

                continue

            description = (
                extract_job_description(
                    job_html
                )
            )

            skills = extract_skills(
                description
            )

            category = categorize_job(
                job["title"],
                description
            )

            job["description"] = description

            job["skills"] = skills

            job["category"] = category

            job["source"] = "naukri"

            job["scraped_at"] = str(
                datetime.now()
            )

            jobs_data.append(job)

            existing_jobs.add(unique_key)

            print(
                f"Saved Naukri: "
                f"{job['title']}"
            )

            job_page.close()

        except Exception as e:

            print(
                f"Naukri Error: {e}"
            )

    # =========================================
    # LINKEDIN SCRAPING
    # =========================================

    print("\nOpening LinkedIn...\n")

    try:

        page.goto(
            LINKEDIN_URL,
            timeout=60000
        )

        time.sleep(8)

        for _ in range(3):

            page.mouse.wheel(0, 3000)

            time.sleep(2)

        linkedin_html = page.content()

        with open(
            "output/linkedin_debug.html",
            "w",
            encoding="utf-8"
        ) as f:

            f.write(linkedin_html)

        linkedin_jobs = extract_linkedin_jobs(
            linkedin_html
        )

        print(
            f"LinkedIn Jobs Found: "
            f"{len(linkedin_jobs)}"
        )

        for job in linkedin_jobs:

            try:

                if not job["apply_link"]:
                    continue

                if not is_tech_job(job["title"]):
                    continue

                if not is_fresher_job(
                    job["title"],
                    job["experience"]
                ):
                    continue

                unique_key = (
                    job["title"].lower().strip() +
                    job["company"].lower().strip()
                )

                if unique_key in existing_jobs:

                    print(
                        f"Duplicate Skipped: "
                        f"{job['title']}"
                    )

                    continue

                print(
                    f"LinkedIn Opening: "
                    f"{job['title']}"
                )

                time.sleep(
                    random.uniform(3, 6)
                )

                job_page = context.new_page()

                job_page.goto(
                    job["apply_link"],
                    timeout=60000
                )

                time.sleep(5)

                job_html = job_page.content()

                if is_dead_job(job_html):

                    print(
                        f"Dead Job Skipped: "
                        f"{job['title']}"
                    )

                    continue

                description = (
                    extract_linkedin_description(
                        job_html
                    )
                )

                skills = extract_skills(
                    description
                )

                category = categorize_job(
                    job["title"],
                    description
                )

                job["description"] = description

                job["skills"] = skills

                job["category"] = category

                job["source"] = "linkedin"

                job["scraped_at"] = str(
                    datetime.now()
                )

                jobs_data.append(job)

                existing_jobs.add(unique_key)

                print(
                    f"Saved LinkedIn: "
                    f"{job['title']}"
                )

                job_page.close()

            except Exception as e:

                print(
                    f"LinkedIn Job Error: {e}"
                )

    except Exception as e:

        print(
            f"LinkedIn Main Error: {e}"
        )

    browser.close()


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