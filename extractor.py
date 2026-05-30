from bs4 import BeautifulSoup


# =========================================
# NAUKRI JOB EXTRACTION
# =========================================

def extract_job_cards(html):

    soup = BeautifulSoup(html, "lxml")

    jobs = []

    cards = soup.find_all(
        "div",
        class_="cust-job-tuple"
    )

    if not cards:

        cards = soup.find_all(
            "article"
        )

    for card in cards:

        title_tag = card.find(
            "a",
            class_="title"
        )

        company_tag = card.find(
            "a",
            class_="comp-name"
        )

        location_tag = card.find(
            "span",
            class_="locWdth"
        )

        exp_tag = card.find(
            "span",
            class_="expwdth"
        )

        salary_tag = card.find(
            "span",
            class_="sal-wrap"
        )

        apply_link = ""

        if title_tag:

            apply_link = title_tag.get(
                "href",
                ""
            )

            if (
                apply_link
                and
                apply_link.startswith("/")
            ):

                apply_link = (
                    "https://www.naukri.com"
                    + apply_link
                )

        job = {

            "title":
                title_tag.text.strip()
                if title_tag else "",

            "company":
                company_tag.text.strip()
                if company_tag else "",

            "location":
                location_tag.text.strip()
                if location_tag else "",

            "experience":
                exp_tag.text.strip()
                if exp_tag else "",

            "salary":
                salary_tag.text.strip()
                if salary_tag else "",

            "apply_link":
                apply_link,

            "description":
                "",

            "skills":
                [],

            "category":
                "",

            "source":
                "naukri"
        }

        jobs.append(job)

    return jobs


# =========================================
# NAUKRI DESCRIPTION EXTRACTION
# =========================================

def extract_job_description(html):

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    possible_classes = [

        "styles_JDC__dang-inner-html__h0K4t",
        "dang-inner-html",
        "job-desc",
        "styles_job-desc-container__txpYf"

    ]

    for class_name in possible_classes:

        desc_div = soup.find(
            "div",
            class_=class_name
        )

        if desc_div:

            return desc_div.get_text(
                separator=" ",
                strip=True
            )

    body = soup.get_text(
        separator=" ",
        strip=True
    )

    return body[:10000]


# =========================================
# LINKEDIN JOB EXTRACTION
# =========================================

def extract_linkedin_jobs(html):

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    jobs = []

    cards = soup.find_all(
        "div",
        class_="base-card"
    )

    if not cards:

        cards = soup.find_all(
            "li"
        )

    for card in cards:

        title_tag = card.find(
            "h3",
            class_="base-search-card__title"
        )

        company_tag = card.find(
            "h4",
            class_="base-search-card__subtitle"
        )

        location_tag = card.find(
            "span",
            class_="job-search-card__location"
        )

        link_tag = card.find(
            "a",
            href=True
        )

        apply_link = ""

        if link_tag:

            apply_link = link_tag.get(
                "href",
                ""
            )

        job = {

            "title":
                title_tag.text.strip()
                if title_tag else "",

            "company":
                company_tag.text.strip()
                if company_tag else "",

            "location":
                location_tag.text.strip()
                if location_tag else "",

            "experience":
                "0-1 years",

            "salary":
                "",

            "apply_link":
                apply_link,

            "description":
                "",

            "skills":
                [],

            "category":
                "",

            "source":
                "linkedin"
        }

        if (
            job["title"]
            and
            job["company"]
        ):

            jobs.append(job)

    return jobs


# =========================================
# LINKEDIN DESCRIPTION EXTRACTION
# =========================================

def extract_linkedin_description(html):

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    possible_classes = [

        "show-more-less-html__markup",
        "description__text",
        "jobs-description",
        "jobs-box__html-content"

    ]

    for class_name in possible_classes:

        desc_div = soup.find(
            "div",
            class_=class_name
        )

        if desc_div:

            return desc_div.get_text(
                separator=" ",
                strip=True
            )

    body = soup.get_text(
        separator=" ",
        strip=True
    )

    return body[:10000]