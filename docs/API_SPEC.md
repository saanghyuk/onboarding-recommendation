# API Specification

**Purpose**: exact request / response contract for every endpoint in `app.py`.

Base URL example: `https://reco.example.com` (production) · `http://localhost:8000` (local).

---

## 1. User-facing endpoints

### 1.1 `GET /api/recommendations`

Thompson-samples the matched cohort and returns top-`k` products.

**Query params** (defined in `app.py:376-389`):

| Name | Type | Required | Notes |
|---|---|---|---|
| `style` | string | yes | `golf` · `sports_casual` · `formal` · `outdoor` (garbage → `sports_casual`) |
| `price` | string | yes | `low` (~30k) · `mid` (30–80k) · `high` (80k+) (garbage → `mid`) |
| `item` | string | yes | `top` · `bottom` · `shoes` · `outer` · `browse` (garbage → `browse`) |
| `k` | int | no | Number of products to return. Default 60, `ge=12`, `le=200` |
| `session_id` | string | no | Persisted client UUID |
| `os_name` | string | no | `iOS` triggers a soft premium boost |
| `hour` | int | no | 0–23 (reserved for future contextual features) |

**Response 200** (shape from `app.py:425-439`):

```json
{
  "meta": {
    "season": "summer",
    "pull_date": "2026-07-10"
  },
  "query": {"style": "golf", "price": "mid", "item": "browse", "os_name": null, "hour": null},
  "matched_key": "golf__mid__browse",
  "is_fallback": false,
  "cohort_size": 385,
  "n_exploit": 42,
  "n_explore": 18,
  "n_total": 60,
  "products": [
    {
      "product_id": "P0012345",
      "brand": "SampleBrand",
      "name": "Summer Cool-Touch Polo",
      "price": 33047,
      "ts_score": 0.87,
      "alpha": 6.2,
      "beta": 95.4,
      "n_impressions": 12,
      "n_clicks": 2,
      "n_purchases": 0,
      "exp_type": "exploit",
      "reason": "Summer season · curated pick"
    }
  ],
  "top12": [ ... ]
}
```

Notes:
- `products` is the current field. `top12` is a **backward-compatibility alias** that carries the same list — do not rely on it being exactly 12 items.
- Impressions are recorded server-side automatically (`bandit.record_impressions`, `app.py:419`). Clients do not need to fire impression events.
- `matched_key` is what the client must send in subsequent `/api/feedback` calls.
- `is_fallback: true` means the ladder in §5 of [MODEL_SPEC.md](MODEL_SPEC.md) was hit.

**Response 404**: all three fallback levels came back empty.
```json
{"detail": "No cohort match: outdoor__high__shoes"}
```

**Response 503**: lookup JSON is missing.
```json
{"detail": "Lookup not found: /app/data/reco_lookup/reco_lookup_latest.json"}
```

---

### 1.2 `POST /api/feedback`

Update the Beta posterior for one (cohort × product) with a signal.

**Request body** (Pydantic model `Feedback`, `app.py:442`):
```json
{
  "session_id": "xxx-yyy-zzz",
  "cohort_key": "golf__mid__browse",
  "product_id": "P0012345",
  "signal": "click"
}
```

**Accepted signals** (`app.py:246`):

| Signal | Update | When to fire |
|---|---|---|
| `click` | α += 1.0 | User tapped the card |
| `purchase` | α += 5.0 | Order-completed callback |
| `skip` | β += 0.5 | Card left the viewport with < 300 ms exposure |
| `dwell_2s` | α += 0.2 | Card stayed in the viewport ≥ 2 s |

**Response 200**:
```json
{"ok": true, "alpha_after": 3.0, "beta_after": 1.0}
```

**Response 400**: unknown signal name.  
**Response 404**: `(cohort_key, product_id)` not present in the posterior table.

Notes:
- Fire-and-forget from the client. Failures should not block UI.
- Every event is also appended to `feedback_log` in `bandit.db`.

---

### 1.3 `POST /api/nlu`

Parse a natural-language sentence into `(style, price, item)`. Always returns 200; uses Claude Haiku when `ANTHROPIC_API_KEY` is set and falls back to a rule-based parser otherwise (`app.py:621`).

**Request body**:
```json
{"text": "I want a mid-range polo for golf rounds"}
```

**Response 200** (rule-based fallback shape):
```json
{
  "ok": true,
  "style": "golf",
  "price": "mid",
  "item": "top",
  "season_hint": null,
  "brand_hint": null,
  "raw_text": "I want a mid-range polo for golf rounds",
  "engine": "rule-based"
}
```

`engine` values:
- `claude-haiku-4-5` — LLM fully parsed all three axes
- `hybrid-rule-fallback` — LLM parsed some axes, rules filled the rest
- `rule-based` — no API key or LLM error, purely regex-driven

**Response 400**: empty text or `> 500` chars.

---

### 1.4 `POST /api/quiz-log`

Append a step-level quiz log line to `logs/quiz_log_YYYY-MM-DD.jsonl`.

**Request body** (Pydantic model `QuizLog`, `app.py:466`):
```json
{
  "session_id": "xxx-yyy-zzz",
  "device": {"os": "iOS", "app_version": "1.0.0"},
  "answers": {"style": "golf"},
  "step": "q1_selected",
  "ts_client": "2026-07-12T12:34:56Z"
}
```

Typical `step` values: `started`, `q1_selected`, `q2_selected`, `q3_selected`, `completed`, `skipped`.

**Response 200**: `{"ok": true, "logged_step": "q1_selected"}`

---

### 1.5 `GET /api/quiz-config`

UI chip metadata (labels, emoji, subtitles) for the three quiz axes. See `app.py:339`.

---

## 2. Ops / debug endpoints

### 2.1 `GET /api/health`
```json
{
  "status": "ok",
  "server_time": "2026-07-12T01:23:45",
  "lookup_pull_date": "2026-07-10",
  "lookup_window_weeks": 4,
  "lookup_season": "summer",
  "total_cohorts": 60,
  "total_first_purchase_users": 15393,
  "bandit": {
    "total_posteriors": 5432,
    "total_feedback": 128,
    "feedback_by_signal": {"click": 96, "skip": 20, "purchase": 5, "dwell_2s": 7}
  }
}
```
If the lookup file is missing, returns `{"status": "degraded", "reason": "lookup missing"}` with a 200.

### 2.2 `GET /api/bandit-stats`
Learning progress summary (`app.py:285`):
- `total_posteriors`, `total_cohorts`, `total_feedback`
- `feedback_by_signal` counts
- `top_learned_products` — products with `n_impressions ≥ 3` sorted by empirical CTR

### 2.3 `GET /api/stats/quiz-logs`
Returns today's quiz-log counts, broken down by step, plus unique session count.

### 2.4 `POST /api/rebuild`
Manual pool rebuild. Requires header `X-Admin-Token: <secret>` matching the `ADMIN_TOKEN` env variable (see `app.py`). Runs `scripts/service_reco_weekly_build.py --window-weeks=52` and reloads the lookup cache.

Response:
```json
{"ok": true, "stdout_tail": "...", "stderr_tail": ""}
```

---

## 3. Auth & CORS

- **CORS**: `app.py` currently allows `*`. Restrict `allow_origins` to your production domains before shipping.
- **Rate limiting**: not implemented in-process. Use Nginx / Cloudflare (recommended: 60 req/min per IP, 300 req/min per session).
- **Admin token**: only `POST /api/rebuild`. Everything else is unauthenticated.

---

## 4. Error format

Standard FastAPI:
```json
{"detail": "message"}
```
Status codes used: 200 / 400 / 401 / 404 / 500 / 503.

---

## 5. End-to-end sample flow

```
1. GET /api/quiz-config
2. POST /api/quiz-log {step: "started"}
3. POST /api/quiz-log {step: "q1_selected", answers: {"style": "golf"}}
4. POST /api/quiz-log {step: "q2_selected", answers: {"price": "mid"}}
5. POST /api/quiz-log {step: "q3_selected", answers: {"item": "browse"}}
6. POST /api/quiz-log {step: "completed"}
7. GET  /api/recommendations?style=golf&price=mid&item=browse&session_id=xxx
        → matched_key = "golf__mid__browse"
8. POST /api/feedback {cohort_key: "golf__mid__browse", product_id: "P001", signal: "dwell_2s"}
9. POST /api/feedback {cohort_key: "golf__mid__browse", product_id: "P001", signal: "click"}
10. POST /api/feedback {cohort_key: "golf__mid__browse", product_id: "P001", signal: "purchase"}
```

---

## Related Files

- `app.py` (all endpoints)
- [FRONTEND_INTEGRATION.md](FRONTEND_INTEGRATION.md) — client code
- [MODEL_SPEC.md](MODEL_SPEC.md) — algorithm rationale
- [DATA_SCHEMA.md](DATA_SCHEMA.md) — CSV / SQLite schema
