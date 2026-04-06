# Study Tracker

A desktop spaced-repetition app powered by the **Ebbinghaus forgetting curve**. It automatically schedules reviews at the optimal time so you retain more while studying less.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Flask](https://img.shields.io/badge/Flask-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

## Features

- **Adaptive scheduling** — reviews are timed using a real forgetting-curve model (`R(t) = a + (1−a)·e^(−kt)`), not fixed intervals
- **Flashcards** — create, edit, and quiz yourself with Q&A cards per topic (supports LaTeX math)
- **Practice problems** — AI-generated problems with hints, step-by-step solutions, and attempt tracking
- **PDF import** — drop a PDF and let AI extract flashcards or practice problems from it
- **AI card generation** — describe a topic in plain text and generate cards instantly
- **Card variation** — AI rewrites existing cards to test the same concepts from different angles
- **Concept tracking** — tag cards with concepts and track retention at the concept level
- **Statistics dashboard** — animated visualisations of retention curves, rating distributions, streaks, and topic health
- **Desktop notifications** — get reminded when topics are due for review
- **Export / Import** — full JSON backup and restore
- **Dark mode** — toggleable light and dark themes
- **Native window** — runs as a desktop app via pywebview (also accessible at `localhost:5000`)

## Quick Start

```bash
git clone https://github.com/congman5/SRS-Study.git
cd SRS-Study/ForgettingCurve
pip install -r requirements.txt
python app.py
```

The app opens in a native desktop window. You can also visit http://localhost:5000 in your browser.

## API Key (optional)

AI features (card generation, PDF import, hints, problem solving) are powered by the Anthropic API.  
You can set your key in two ways:

**Option A — In-app settings (easiest)**  
Click the ⚙️ gear icon in the header, paste your key, and hit Save.

**Option B — Environment file**
```bash
cp .env.example .env
# edit .env and paste your Anthropic API key
```

Get a key at [console.anthropic.com](https://console.anthropic.com/). The app works without one — AI features are simply disabled.

### Optional dependencies

```bash
pip install anthropic   # AI card generation, hints & PDF import
pip install PyPDF2      # PDF text extraction
```

## How the Curve Works

Each topic's retention is modelled as:

```
R(t) = a + (1 − a) · e^(−k · t)
```

| Parameter | Meaning | After each review |
|-----------|---------|-------------------|
| **a** | Long-term retention floor | Rises → memory floor gets higher |
| **k** | Forgetting rate | Falls → forgetting slows down |
| **t** | Days since last review | — |

A review is scheduled when retention drops below **80%**, producing a naturally expanding schedule: ~Day 1 → 3 → 7 → 21 → 55 → 130 …

## Project Structure

| Path | Purpose |
|------|---------|
| `ForgettingCurve/app.py` | Flask server + embedded HTML/CSS/JS (single-file app) |
| `ForgettingCurve/topics.db` | SQLite database (auto-created at runtime) |
| `ForgettingCurve/requirements.txt` | Python dependencies |
| `ForgettingCurve/.env.example` | Template for environment variables |
| `ForgettingCurve/remotion-stats/` | React components for the statistics dashboard |
| `ForgettingCurve/StudyTracker.bat` | Windows launcher (double-click to run) |
