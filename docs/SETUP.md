# Local Dev Setup

**Purpose**: get the service running on a laptop in under 30 minutes with a fully seeded lookup and bandit.

---

## 0. Prerequisites

- Linux, macOS, or Windows with WSL2
- Python 3.11 – 3.13 (3.14 has a pyexpat incompatibility with some deps)
- Git
- ~500 MB free disk for lookup + SQLite

Optional:
- A warehouse account (Snowflake / BigQuery / Postgres) for real data. Without it you can still run the service on synthetic fixtures — see §1.4.

---

## 1. Getting up and running (30 minutes)

### 1.1 Clone
```bash
git clone <this-repo-url> onboarding-recommendation
cd onboarding-recommendation
```

### 1.2 Python environment
```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1.3 Folder layout
```bash
mkdir -p data/incoming data/raw data/processed data/reco_lookup logs
```

### 1.4 Data preparation — pick one path

**Path A · Synthetic fixture (default, no warehouse)**

```bash
python scripts/generate_sample_data.py
# Writes data/reco_lookup/reco_lookup_latest.json with 60 cohorts × 30 products.
# Uses generic brand names (`FairwayCo`, `EverydayCo`, …) so it's portable.
```

You can skip §1.5 with this path — the fixture is already the lookup JSON the
service reads.

**Path B · Real warehouse (production wire-up)**

Produce three files in `data/raw/` matching [DATA_SCHEMA.md](DATA_SCHEMA.md):

- `events_cohort_slim.csv`
- `web_events.csv`
- `user_master_coldstart.parquet`

Then run the weekly batch below. The starter SQL is in
`scripts/service_reco_weekly_build.py` and covers Snowflake, BigQuery,
Postgres, and DuckDB. Adopt the parts you need.

### 1.5 Build the first lookup (Path B only)
```bash
python scripts/service_reco_weekly_build.py
# Takes ~5 minutes on real data, seconds on synthetic
```
Expected output:
```
[BUILD] Pull date = 2026-07-10 · Window = 2026-06-12 → 2026-07-10 (4 weeks)
[STEP 1] union app+web purchase events with window filter
[STEP 2] pull first-purchase preference vector
[STEP 3] per-cohort top-30 (auto-detected golf brands = 42)
[STEP 4] write lookup JSON
[DONE]
  · Cohorts built: 60
  · Non-empty cohorts: 56
```

### 1.6 Start the API
```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

### 1.7 Health check
```bash
curl http://localhost:8000/api/health | jq
```

Expected:
```json
{
  "status": "ok",
  "server_time": "2026-07-12T...",
  "lookup_pull_date": "2026-07-10",
  "lookup_window_weeks": 4,
  "lookup_season": "summer",
  "total_cohorts": 60,
  "total_first_purchase_users": 15393,
  "bandit": {
    "total_posteriors": 5432,
    "total_feedback": 0,
    "feedback_by_signal": {}
  }
}
```

### 1.8 First recommendation
```bash
curl "http://localhost:8000/api/recommendations?style=golf&price=mid&item=browse&session_id=test-1&k=60" | jq '.products[0]'
```

### 1.9 Fire a feedback signal
```bash
curl -X POST http://localhost:8000/api/feedback \
  -H "Content-Type: application/json" \
  -d '{"cohort_key":"golf__mid__browse","product_id":"P0001","signal":"click","session_id":"test-1"}'
```

### 1.10 (Optional) Try the five reference UI variants

This repo ships the API only. If you have your own `index.html` / `simple.html` / `voice.html` / `swipe.html` / `persona.html`, drop them into `./static/` and the routes below will serve them. Otherwise `/` returns a JSON landing and the other paths return 404.

- `http://localhost:8000/` — landing
- `http://localhost:8000/simple` — chip-only three-step quiz
- `http://localhost:8000/voice` — natural-language input (uses `/api/nlu`; rule-based fallback when `ANTHROPIC_API_KEY` is unset)
- `http://localhost:8000/swipe` — Tinder-style swipe (lookup injected as `window.RECO_LOOKUP`)
- `http://localhost:8000/persona` — persona picker

Wiring notes for iOS, Android, and web (including `click / skip / purchase` feedback) are in [FRONTEND_INTEGRATION.md](FRONTEND_INTEGRATION.md).

---

## 2. Environment variables

Create `.env` (loaded by your process manager / systemd / uvicorn wrapper):

```
DATA_DIR=./data
ADMIN_TOKEN=change-me
ANTHROPIC_API_KEY=            # optional — enables LLM NLU
SLACK_WEBHOOK=                # optional — weekly build alerts
SENTRY_DSN=                   # optional — production error tracing
```

---

## 3. Production checklist (not covered here — see [DEPLOY.md](DEPLOY.md))

- [ ] Persistent volume mounted at `data/`
- [ ] TLS + domain
- [ ] CORS restricted (`app.py:52`)
- [ ] Rate limiting (Nginx / Cloudflare)
- [ ] Weekly cron / GitHub Actions trigger
- [ ] Alerts (Slack, Sentry)

---

## 4. Troubleshooting

### 4.1 `ModuleNotFoundError: No module named 'duckdb'`
Ensure Python 3.11–3.13. Recreate the venv:
```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 4.2 `Lookup not found`
The weekly build has not been run yet. Run `python scripts/service_reco_weekly_build.py`.

### 4.3 `HTTPException(404: No cohort match)`
All three fallback levels are empty. Inspect the lookup:
```bash
python -c "import json; d=json.load(open('data/reco_lookup/reco_lookup_latest.json')); print([k for k,v in d['cohorts'].items() if not v.get('top12')])"
```

### 4.4 SQLite lock errors
`DB_LOCK` in `app.py:79` serialises writes for a single-process uvicorn. Do not run multiple workers against the same SQLite file — see [PRODUCTION_ROADMAP.md](PRODUCTION_ROADMAP.md) for the Postgres migration plan.

### 4.5 Reset the bandit state
```bash
rm data/bandit.db
# Restart uvicorn — the lookup will reseed on the first request.
```

---

## Related Files

- `app.py`
- `scripts/generate_sample_data.py`
- `scripts/service_reco_weekly_build.py`
- [DEPLOY.md](DEPLOY.md)
- [WEEKLY_WORKFLOW.md](WEEKLY_WORKFLOW.md)
- [FRONTEND_INTEGRATION.md](FRONTEND_INTEGRATION.md)
- [API_SPEC.md](API_SPEC.md)
