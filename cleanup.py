# =========================================
# DEAD JOB CHECK
# =========================================

from bs4 import BeautifulSoup


def is_dead_job(html):

    if not html:
        return True

    html_lower = html.lower()

    # HARD DEAD SIGNALS ONLY
    hard_dead_signals = [

        "404 page",
        "job not found",
        "this job does not exist",
        "page not found",
        "invalid job",
        "job you are looking for is not available"

    ]

    for signal in hard_dead_signals:

        if signal in html_lower:
            return True

    # Parse visible title/body only
    soup = BeautifulSoup(html, "lxml")

    title = soup.title.text.lower() if soup.title else ""

    visible_text = soup.get_text(
        separator=" ",
        strip=True
    ).lower()

    # Strong combined checks only
    if (
        "expired" in title and
        "job" in title
    ):
        return True

    if (
        "job expired" in visible_text[:1500]
    ):
        return True

    return False