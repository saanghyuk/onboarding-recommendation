# Onboarding Recommendation Engine

A **cold-start recommendation engine** for e-commerce onboarding.
Turn a 3-tap quiz — or a voice utterance, or a Tinder-style swipe — into a
live-learning product feed in under 30 seconds of user time, and let the
model keep learning from every click, purchase, and skip.

Built for the moment you know least about a new user: their first session.

---

## Why this engine

A new user's first session has no data. Most commerce apps patch this with
a hand-curated "trending" list that converts poorly and never adapts. This
engine attacks that gap directly.

- **Start from a 3-tap onboarding** — user answers three axes
  (`style · price · item`) and lands in one of ~60 cohort buckets built
  from your existing buyer base.
- **Learn from the first click** — no re-training loop. A single atomic
  SQL update moves the Beta(α, β) posterior in real time. The next
  request is already smarter.
- **Warehouse-agnostic** — Snowflake, BigQuery, Postgres, or DuckDB. Swap
  the SQL, keep everything else.
- **UI-free by design** — this repo ships only the API and model. Bring
  your own front-end (5 reference implementations included below).

## Algorithm at a glance

- **Thompson Sampling bandit** over `(cohort, product)` — Beta(α, β)
  posteriors with a K-rescaled weakly-informative prior, so the first
  click actually moves the ranking (naive implementations get drowned by
  the seed weight and never learn).
- **Weekly cohort builder** — Bayesian smoothing with recency half-life,
  brand-diversity cap, and exploit / explore split.
- **Bayesian active learning (swipe mode)** — posterior over 6 archetypes,
  information-gain-based next-card selection, early stop at 4-5 swipes
  once confidence crosses 0.75.
- **Category and season hygiene** — configurable filters that keep, e.g.,
  golf brands out of casual cohorts and winter coats out of summer feeds.
  Point the vocab list at your own catalogue.
- **NLU with rule-based fallback** — Claude Haiku when the API key is
  available, otherwise a deterministic keyword parser that still returns
  sensible triples.
- **Enum-safe API** — garbage input gets clamped to sensible defaults
  instead of raising 500.

## Five onboarding UX modes, one API

Chatbot, quiz, voice, swipe, persona — pick the one that fits your brand.
All five reduce to the same `(style, price, item)` triple and hit the same
`GET /api/recommendations` endpoint. To ship a new UX, produce those three
axes any way you like and pass them in.

| Mode | When to use |
|---|---|
| Chatbot | Conversational-tone apps, lifestyle brands, guided onboarding |
| Quiz | Lowest drop-off. Default when retention is the priority |
| Voice | Strong first impression, accessibility, LLM-forward apps |
| Swipe | Gamified. Works best where taste is expressive (fashion, beauty) |
| Persona | Identity-matching brands ("this is my kind of thing") |

Reference implementations live in the demo front-end. See
`docs/FRONTEND_INTEGRATION.md` for wiring notes.

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
onboarding-recommendation/
├── README.md                                  <- this file
├── ARCHITECTURE.md                            <- system diagram + components
├── LICENSE
├── requirements.txt
├── .gitignore
│
├── app.py                                     <- FastAPI + Thompson sampling bandit
├── scripts/
│   ├── generate_sample_data.py                <- synthetic fixture (no warehouse needed)
│   └── service_reco_weekly_build.py           <- weekly batch (bring your own SQL)
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
    ├── reco_lookup/                           <- lookup JSON (fixture or batch output)
    ├── bandit.db                              <- SQLite posterior store
    └── quiz_logs.jsonl
```

This repo ships **only the API and model**. The demo UIs (chatbot / quiz / voice / swipe / persona) are reference implementations — you bring your own front-end. See `docs/FRONTEND_INTEGRATION.md` for wiring notes.

---

## Quick Start (30 seconds)

```bash
git clone https://github.com/saanghyuk/onboarding-recommendation
cd onboarding-recommendation
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Synthetic fixture — 60 cohorts, 600 products. Runs standalone, no warehouse.
python scripts/generate_sample_data.py

uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Then hit the API:

```bash
curl "http://localhost:8000/api/recommendations?style=golf&price=high&item=top&k=12"
```

Send some feedback and watch the posterior move:

```bash
curl -X POST http://localhost:8000/api/feedback \
  -H "Content-Type: application/json" \
  -d '{"session_id":"u1","cohort_key":"golf__high__top",
       "product_id":"go2t-000","signal":"purchase"}'
```

A `purchase` bumps α by 5.0. Re-run the recommendations call and you'll see that product hold rank 1 in nearly every draw within a few events — that's real online learning, not a nightly batch.

Ready to plug in your real catalogue? Jump to [Bring your own warehouse](#bring-your-own-warehouse) below.

---

## Adapt to your vertical

The default vocabulary in this repo is a menswear-shopping example:
`style ∈ {golf, sports_casual, formal, outdoor}`, `price ∈ {low, mid, high}`,
`item ∈ {top, bottom, shoes, outer, browse}`, currency `KRW`,
category tokens like `GOLF_TOKENS` / `WINTER_TOKENS` / `SUMMER_TOKENS`.

**All of these are swappable.** Point the enums, price buckets, category
vocab, and NLU keyword lists at your own domain:

| Your vertical | Suggested axes |
|---|---|
| Beauty | `skin_type`, `price`, `product_type` (cleanser / toner / serum / …) |
| Home goods | `room`, `price`, `category` (lighting / storage / decor / …) |
| Grocery | `diet`, `price`, `department` (produce / bakery / dairy / …) |
| Sportswear | `sport`, `price`, `item` |

Concrete steps in `docs/MODEL_SPEC.md` §2 and `docs/DATA_SCHEMA.md` §1.

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
- `scripts/generate_sample_data.py` — synthetic fixture generator (default entry point)
- `scripts/service_reco_weekly_build.py` — weekly batch template (bring your own SQL)
- `docs/PRODUCTION_ROADMAP.md` — everything a backend team needs to take this demo to production

---

## License

MIT. See [LICENSE](LICENSE).
