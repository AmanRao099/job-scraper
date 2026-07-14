# Intelligent Tech Job Scraper

A modular Playwright-based job scraping system focused on fresher and entry-level tech jobs from platforms like Naukri and LinkedIn.

This project extracts technical job listings, filters fresher roles, parses skills from job descriptions, categorizes jobs, removes dead listings, and stores structured job data in JSON format.

---

# Features

- Playwright browser automation
- Naukri job scraping
- LinkedIn public job scraping
- Fresher / entry-level filtering
- Technical job filtering
- Skill extraction
- Job categorization
- Dead job detection
- Duplicate filtering
- Structured JSON storage
- Modular architecture

---

# Tech Stack

- Python
- Playwright
- BeautifulSoup4
- lxml

---

# Project Structure

```bash
job_scraper/
│
├── main.py
├── extractor.py
├── parser.py
├── categorizer.py
├── cleanup.py
├── requirements.txt
├── README.md
│
├── output/
│   └── jobs.json
```

---

# Extracted Job Fields

Each job contains:

```json
{
  "title": "",
  "company": "",
  "location": "",
  "experience": "",
  "salary": "",
  "apply_link": "",
  "description": "",
  "skills": [],
  "category": "",
  "source": "",
  "scraped_at": ""
}
```

---

# Supported Sources

Currently Supported:

- Naukri
- LinkedIn Public Jobs

Planned Sources:

- Foundit
- Hirist
- Wellfound
- RemoteOK
- We Work Remotely

---

# Job Categories

Current categories include:

- Backend
- Frontend
- Full Stack
- Data Science
- DevOps
- Mobile Development

---

# Installation

## 1. Clone Repository

```bash
git clone <your_repo_url>
cd job_scraper
```

---

## 2. Create Virtual Environment

### Windows

```bash
python -m venv venv
```

### Linux / Mac

```bash
python3 -m venv venv
```

---

## 3. Activate Virtual Environment

### Windows

```bash
venv\Scripts\activate
```

### Linux / Mac

```bash
source venv/bin/activate
```

---

## 4. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 5. Install Playwright Browsers

```bash
playwright install
```

---

# How To Run

The scraper now includes a React UI and a FastAPI backend. You need to run both to use the dashboard.

## 1. Start the Backend

In a terminal, ensure your virtual environment is active and run:

```bash
python api.py
```

The backend server will start on `http://localhost:8000`.

## 2. Start the React UI

Open a *new* terminal window, navigate to the `UI` folder, install Node dependencies, and start the development server:

```bash
cd UI
npm install
npm run dev
```

Visit the local URL provided by Vite (usually `http://localhost:5173`) to view the dashboard and start scraping!

---

## (Optional) Run without UI:

If you prefer to run the scraper directly from the terminal without the web interface:

```bash
python main.py
```

The scraper will:

1. Open Playwright browser
2. Scrape fresher tech jobs
3. Filter technical roles
4. Extract descriptions
5. Detect skills
6. Categorize jobs
7. Remove duplicates
8. Save output to JSON

---

# Output

Scraped jobs are saved in:

```bash
output/jobs.json
```

LinkedIn debug HTML:

```bash
output/linkedin_debug.html
```

---

# Current Filtering Logic

The scraper currently filters:

- Technical jobs only
- Fresher / entry-level jobs
- Duplicate jobs
- Dead / invalid jobs

---

# Skill Extraction

The parser detects technologies such as:

- Python
- Java
- React
- Node.js
- AWS
- Docker
- Kubernetes
- TensorFlow
- SQL
- MongoDB
- FastAPI
- Django
- Flask

and more.

---

# Anti-Detection Measures

Current protections include:

- Real browser automation using Playwright
- Human-like scrolling
- Randomized delays
- User-agent spoofing
- Reduced crawl volume


---

# Disclaimer

This project is for educational and research purposes only.

Users are responsible for complying with the terms and policies of any websites accessed by this scraper.

---

# Author

Bimal  
