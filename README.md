# Onboarding Recommendation Service

**Menswear cold-start onboarding recommendation** with online Thompson Sampling. A new user answers a 3-tap quiz (style / price / item), and the server returns a personalised product list drawn from a weekly-refreshed cohort pool. Every user event (click / purchase / skip / dwell) updates a Beta posterior in real time.

---

## Purpose

A single line: **turn a 3-tap quiz into a live-learning recommendation engine that can be cloned, deployed and integrated end-to-end.**

- **Input**: three quiz answers (`style`, `price`, `item`)
- **Output**: top-N personalised products with reasons
- **Learning**: click / purchase / skip / dwell events update Beta(α, β) posteriors per (cohort × product)
- **Refresh**: a weekly batch rebuilds the candidate pool from the internal purchase warehouse

---

## Where to look first

| Goal | Doc |
|---|---|
| Full system overview | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Local dev setup | [docs/SETUP.md](docs/SETUP.md) |
| Weekly Jupyter workflow | [docs/WEEKLY_WORKFLOW.md](docs/WEEKLY_WORKFLOW.md) |
| iOS / Android / Web integration | [docs/FRONTEND_INTEGRATION.md](docs/FRONTEND_INTEGRATION.md) |
| REST API reference | [docs/API_SPEC.md](docs/API_SPEC.md) |
| Bandit algorithm spec | [docs/MODEL_SPEC.md](docs/MODEL_SPEC.md) |
| Data schema (CSV + SQLite) | [docs/DATA_SCHEMA.md](docs/DATA_SCHEMA.md) |
| Vercel / Docker / prod deploy | [docs/DEPLOY.md](docs/DEPLOY.md) |
| Demo → production roadmap | [docs/PRODUCTION_ROADMAP.md](docs/PRODUCTION_ROADMAP.md) |

---

## Repository layout

```
service_app/
├── README.md                                  <- this file
├── ARCHITECTURE.md                            <- system diagram + components
├── requirements.txt
├── .gitignore
│
├── app.py                                     <- FastAPI + Thompson sampling bandit
├── scripts/
│   └── service_reco_weekly_build.py           <- Weekly batch (pool rebuild)
├── static/                                    <- 5 demo UIs
│   ├── index.html      (default landing)
│   ├── simple.html     (chip-based quiz)
│   ├── voice.html      (natural-language / NLU)
│   ├── swipe.html      (Tinder-style)
│   └── persona.html    (persona picker)
│
├── notebooks/
│   └── weekly_data_pull.ipynb                 <- warehouse pull template
│
├── docs/
│   ├── SETUP.md
│   ├── DEPLOY.md
│   ├── WEEKLY_WORKFLOW.md
│   ├── DATA_SCHEMA.md
│   ├── FRONTEND_INTEGRATION.md
│   ├── API_SPEC.md
│   ├── MODEL_SPEC.md
│   └── PRODUCTION_ROADMAP.md
│
└── data/  logs/                               <- runtime-generated (gitignored)
    ├── raw/                                   <- Jupyter upload target
    ├── incoming/                              <- pending new files
    ├── processed/
    ├── reco_lookup/                           <- weekly build output
    ├── bandit.db                              <- SQLite posterior store
    └── quiz_logs.jsonl
```

---

## Quick Start (5 minutes)

```bash
# 1. Clone + Python env
git clone <this-repo-url> onboarding-recommendation
cd onboarding-recommendation
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Prepare data folders
mkdir -p data/raw data/processed data/reco_lookup data/incoming logs

# 3. Drop three warehouse extracts into data/raw/ (see docs/DATA_SCHEMA.md)
#    - events_cohort_slim.csv
#    - web_events.csv
#    - user_master_coldstart.parquet

# 4. Build the first lookup (~5 min)
python scripts/service_reco_weekly_build.py

# 5. Start the API server
uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# 6. Health check
curl http://localhost:8000/api/health

# 7. First recommendation
curl "http://localhost:8000/api/recommendations?style=golf&price=mid&item=browse&session_id=test-1&k=60"

# 8. Send a feedback signal
curl -X POST http://localhost:8000/api/feedback \
  -H "Content-Type: application/json" \
  -d '{"cohort_key":"golf__mid__browse","product_id":"P0001","signal":"click","session_id":"test-1"}'
```

Open `http://localhost:8000/` for the default UI, or try `/simple`, `/voice`, `/swipe`, `/persona` for the other four quiz variants.

---

## Two learning loops

- **Weekly (10 min human step)** — Jupyter pulls the last 4 weeks of purchase events → CSVs land on the server → cron picks them up → new `reco_lookup_*.json` is written. Only *new* (cohort × product) pairs get seeded; existing posteriors are preserved.
- **Realtime (fully automatic)** — every quiz answer + product interaction hits `POST /api/feedback` and updates the Beta posterior in SQLite (`data/bandit.db`).

You need both: the weekly loop injects new products and reflects seasonality, and the realtime loop learns from the users you already have.

Full detail: [ARCHITECTURE.md](ARCHITECTURE.md) · [docs/MODEL_SPEC.md](docs/MODEL_SPEC.md).

---

## Endpoints at a glance

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/recommendations?style=&price=&item=&k=60` | Thompson-sampled top-N (12 ≤ k ≤ 200) |
| `POST` | `/api/feedback` | click / purchase / skip / dwell_2s signal |
| `POST` | `/api/nlu` | Natural-language → (style, price, item) (Claude Haiku + rule-based fallback) |
| `POST` | `/api/quiz-log` | Log each quiz step |
| `GET` | `/api/quiz-config` | UI chip options |
| `GET` | `/api/health` | Server + lookup + bandit status |
| `GET` | `/api/bandit-stats` | Learning progress |
| `POST` | `/api/rebuild` | Manual pool rebuild (admin token) |

Contract: [docs/API_SPEC.md](docs/API_SPEC.md).

---

## Tech stack

- **Backend**: FastAPI · uvicorn · Python 3.11 – 3.13
- **Bandit store**: SQLite (WAL mode) · migrating to Postgres in production ([roadmap](docs/PRODUCTION_ROADMAP.md))
- **Batch**: DuckDB · pandas · numpy · pyarrow
- **Algorithm**: Thompson sampling with K-rescaled weakly-informative Beta prior (K = 100)
- **Optional LLM**: Anthropic Claude Haiku for the `/api/nlu` route (rule-based fallback if `ANTHROPIC_API_KEY` is unset)

---

## Related Files

- `app.py` — FastAPI service, bandit, NLU
- `scripts/service_reco_weekly_build.py` — weekly batch
- `notebooks/weekly_data_pull.ipynb` — Jupyter warehouse pull template
- `docs/PRODUCTION_ROADMAP.md` — everything a backend team needs to take this demo to production

---

## License

Released for public reference. Use at your own risk; no warranty. See individual docs for operational caveats.
