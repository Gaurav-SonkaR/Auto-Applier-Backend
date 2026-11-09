# AutoApply AI

A local-only autonomous job application system. Scrapes job portals, generates ATS-tailored LaTeX resumes via Groq LLM, auto-fills application forms with Selenium, and sends cold emails — all from your machine. Nothing leaves your network except LLM API calls.

---

## Architecture

```
Frontend (React + Vite :5173)
    ↕ REST + WebSocket
Backend (FastAPI :8000)
    ├── LangGraph Workflows
    │   ├── Portal Apply Flow  →  Scrape → JD Parse → Resume → Apply
    │   └── Cold Email Flow    →  Excel → Classify → Resume → Send
    ├── Services
    │   ├── LLM Router         →  Groq (primary) / Ollama (local) / HuggingFace (fallback)
    │   ├── LaTeX Compiler     →  pdflatex → PDF
    │   ├── QA Cache           →  ChromaDB semantic cache for form Q&A
    │   └── Email Sender       →  Gmail SMTP SSL, 40/day hard limit
    └── SQLite                 →  Jobs, Applications, Emails, Run Logs
```

---

## Requirements

- Python 3.11+
- Node.js 18+
- [pdflatex](https://www.latex-project.org/get/) (MiKTeX on Windows)
- [Ollama](https://ollama.com) running locally (for local LLM + embeddings)
- A [Groq](https://console.groq.com) API key (free tier)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) (for cold email)

---

## Quick Start

### 1. Clone & install Python deps

```bash
git clone <repo-url>
cd agentic
py -m pip install -r requirements.txt
# Install Google Chrome or Chromium. Selenium Manager resolves a matching driver.
```

### 2. Pull Ollama models

```bash
ollama pull phi3:mini
ollama pull gemma2:2b
ollama pull nomic-embed-text
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```env
GROQ_API_KEY=gsk_...          # Required — get from console.groq.com
LINKEDIN_EMAIL=you@gmail.com  # Portal login credentials
LINKEDIN_PASSWORD=yourpassword
```

For cold email:
```env
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

For Google job discovery (optional, free 2,500/month):
```env
SERPER_API_KEY=your_key       # Get from serper.dev
```

### 4. Add your resume & profile

- Verify `master_profile.json` has your skills, experience, and education
- The LaTeX resume template ships at `templates/resume_template.tex` — edit it
  to change layout. Its structure is load-bearing: `resume_generator.py` injects
  tailored content by matching the file's exact shape, and the contracts are
  documented in the template's own header. `tests/test_resume_template.py`
  fails loudly if an edit breaks one.
- Optionally upload your own resume PDFs on the **Settings** page. The one
  marked *master* drives portal job search: leave the job title blank when
  starting a run and its extracted title and top skills become the query.

### 5. Create your login

The UI and every API route require a sign-in. There is **no sign-up page** —
the only way an account can exist is running this on the machine hosting the
backend, so nobody who can reach the app over the network can create one:

```bash
python scripts/set_password.py
```

It writes `AUTH_USERNAME`, `AUTH_PASSWORD_HASH` (scrypt) and `AUTH_SECRET_KEY`
into `.env`. Your password itself is never stored — only its hash. Re-run it any
time to change the password; add `--rotate-secret` to also sign out every
existing session.

### 6. Start the backend

```bash
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

The API auto-creates `data/autoapply.db` on first start.

### 7. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**

---

## Usage

### Portal Apply Pipeline

1. Open the dashboard → **Portal Apply Pipeline** panel
2. Select portal (LinkedIn / Naukri / Indeed / Wellfound / Cutshort)
3. Enter job title and location
4. Click **Start Run** — the pipeline will:
   - Scrape matching job listings
   - Fetch and parse each job description
   - Generate an ATS-tailored PDF resume (LaTeX → PDF)
   - Auto-fill and submit the application form
5. Watch live events in the **Live Events** feed
6. Check **Flagged** tab for jobs that need your attention (CAPTCHAs, external ATS redirects)

### Cold Email Campaign

1. Prepare an Excel/CSV with columns: `company`, `email`, `name` (contact name)
2. Open the dashboard → **Cold Emails** tab
3. Upload your file, enter Gmail credentials, set batch size
4. Click **Send Emails** — sends up to 40/day with delays to avoid spam filters

### Flagged Jobs

Jobs are flagged (not failed) when:
- A CAPTCHA was detected — open the job URL manually and solve it
- The apply button redirected to an external ATS — click the link in the Flagged tab to apply directly

Retry flagged jobs from the Flagged tab after handling them manually.

---

## Project Structure

```
Auto-Applier-Backend/
├── backend/
│   └── app/
│       ├── main.py            # FastAPI app entry point
│       ├── config.py          # Pydantic-settings (all env vars)
│       ├── database.py        # SQLite engine, schema creation, migrations
│       ├── constants.py       # External-ATS domains + helper
│       ├── models/            # SQLModel tables (Job, Resume, Application,
│       │                      #   ColdEmailLog, JobStageEvent, RunLog)
│       ├── routers/           # FastAPI routers (jobs, resumes, runs,
│       │                      #   dashboard, cold_email, websocket)
│       ├── schemas/           # Pydantic request/response models per domain
│       ├── services/          # scraper, jd_parser, resume_generator, applier,
│       │                      #   discovery, form_autofill, resume_library,
│       │                      #   llm_client, latex_compiler, qa_cache,
│       │                      #   browser_pool, email_sender, portal_templates/
│       ├── workflows/         # LangGraph orchestration (run_loop + per-run flows)
│       └── utils/             # file_naming, validators, profile, file_serving
├── templates/
│   └── resume_template.tex    # ATS-clean LaTeX master template
├── tests/                     # pytest suite
├── data/                      # SQLite DB + ChromaDB (gitignored)
├── resumes/                   # Generated PDFs; library/ holds uploads (gitignored)
├── screenshots/               # Selenium apply-time screenshots (gitignored)
├── logs/                      # App logs + saved portal sessions (gitignored)
├── master_profile.json
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

The frontend lives in the separate `auto-applier/` repo (Vite + React + TypeScript
+ TailwindCSS).

### Pipeline

```
                        ┌─ portal jobs ──► apply ──► APPLIED
scrape ─► fetch_jd ─► parse ─► resume ─┤
                        └─ portal="web" ─► READY_TO_APPLY  (you apply manually)
```

**Smart Job Apply** (`portal="web"`) discovers jobs on company career pages via
Google, builds a tailored resume, and stops. The apply link and PDF appear on the
**Ready to Apply** page; mark the job applied once you've submitted it.
**Job portals** (LinkedIn, Naukri, Indeed, Wellfound, Cutshort, Instahyre) are
applied to automatically.

### Docker

```bash
cp .env.example .env      # fill in your keys
docker compose up -d --build
docker compose logs -f backend
```

The API is published on `127.0.0.1:8000` only — it has no authentication, so
reach it remotely with an SSH tunnel rather than opening the port:

```bash
ssh -L 8000:127.0.0.1:8000 user@your-instance
```

### Tests

```bash
pytest
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | Yes | — | Groq API key (llama-3.3-70b-versatile) |
| `OLLAMA_BASE_URL` | No | `http://localhost:11434` | Ollama server URL |
| `SERPER_API_KEY` | No | — | Serper.dev Google Search API (optional) |
| `LINKEDIN_EMAIL` | No | — | LinkedIn login email |
| `LINKEDIN_PASSWORD` | No | — | LinkedIn login password |
| `NAUKRI_EMAIL` | No | — | Naukri login email |
| `NAUKRI_PASSWORD` | No | — | Naukri login password |
| `GMAIL_ADDRESS` | For cold email | — | Gmail address |
| `GMAIL_APP_PASSWORD` | For cold email | — | Gmail App Password (not regular password) |
| `COLD_EMAIL_DAILY_LIMIT` | No | `40` | Max emails per day |
| `SELENIUM_HEADLESS` | No | `true` | Set `false` to watch the browser |
| `SELENIUM_MAX_DRIVERS` | No | `2` | Maximum concurrent Selenium browser sessions |
| `CHROME_BINARY` | No | auto-detected | Optional Chrome/Chromium executable path |
| `RESUME_MAX_RETRIES` | No | `3` | Max LLM retries for ATS score |
| `ATS_SCORE_THRESHOLD` | No | `85` | Minimum ATS keyword match % |
| `DAILY_SEARCH_LIMIT` | No | `100` | Max Serper searches per day |
| `FRONTEND_PORT` | No | `5173` | Frontend dev server port |

---

## Running Tests

```bash
py -m pytest tests/ -v
```

Integration tests (require Ollama + pdflatex running):
```bash
py -m pytest tests/integration/ -v
```

---

## Security Notes

- The backend binds to `127.0.0.1` only — not accessible from other machines
- Credentials are read from `.env` only, never logged or sent to the frontend
- CAPTCHAs are never bypassed — jobs are flagged for manual review
- Cold email has a hard 40/day limit enforced in code
- Gmail App Password is used, never your regular Google password
