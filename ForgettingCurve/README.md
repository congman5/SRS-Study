# Study Tracker

Spaced repetition scheduler built on the Ebbinghaus forgetting curve.

## How it works

Each topic is modelled as:

    R(t) = a + (1 − a) · e^(−k · t)

where **t** is days since the last review, **a** is the long-term retained
floor, and **k** is the forgetting rate.  The next review is scheduled
automatically for when R(t) drops below **80%**.

After each review the curve improves:
- **a** rises  → the memory floor gets higher
- **k** falls  → forgetting slows down

This produces a naturally expanding schedule (~Day 1 → 3 → 7 → 21 → 55 →
130 …) that adapts to how well the material has consolidated, rather than
using fixed hard-coded dates.

## Setup

```bash
pip install -r requirements.txt
```

### API key (optional)

AI-powered features (PDF import, card generation) require an
[Anthropic API key](https://console.anthropic.com/).  
Copy the example env file and fill in your key:

```bash
cp .env.example .env
# then edit .env and paste your key
```

If no key is provided the app still works — the AI features are simply disabled.

### Optional dependencies

```bash
pip install anthropic   # AI card generation & PDF import
pip install PyPDF2      # PDF text extraction
```

### Run

```bash
python app.py
```

The app opens automatically in a desktop window.  
You can also visit http://localhost:5000 in your browser.

## Files

| File           | Purpose                              |
|----------------|--------------------------------------|
| `app.py`       | Flask app + embedded HTML (one file) |
| `topics.db`    | SQLite database (auto-created)       |
| `requirements.txt` | Python dependencies              |
