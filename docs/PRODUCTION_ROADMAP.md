# Production Roadmap — Demo to Production

**Purpose**: everything a backend team needs to do to take this repo from a working demo into a production-grade recommendation service. Organised as P0 (blockers), P1 (first month), P2 (mature).

Read this after skimming [ARCHITECTURE.md](../ARCHITECTURE.md) and [MODEL_SPEC.md](MODEL_SPEC.md). Each item has an intent, a concrete action, and a hint at where in the codebase to touch.

---

## P0 — Blockers (do these before real users see it)

### P0.1 Real data pipeline

**Problem**: today the weekly batch reads three CSVs uploaded by a human via Jupyter. Fine for a pilot, unreliable for production.

**Actions**:
- **Warehouse connection**: replace the notebook workflow with a scheduled job that pulls from Snowflake / BigQuery / Redshift directly. Use secret management (AWS Secrets Manager, Doppler, 1Password) — never bake keys into the image.
- **Weekly cron**: pick one of GitHub Actions, Cloud Scheduler, or an in-cluster CronJob. Trigger `/api/rebuild` or invoke `scripts/service_reco_weekly_build.py` server-side.
- **Product catalogue sync**: pull stock, price, image URL, category, season, brand each day from the merchandising system. Products missing from this catalogue must be filterable at serve time (out-of-stock guard).
- **Category tagger**: replace the default regex `CATEGORY_RULES` (`scripts/service_reco_weekly_build.py`) with a JOIN against your catalog's category column — this is the **recommended path** and eliminates the mixed-keyword misclassifications the regex baseline suffers from (e.g. "블루종 티셔츠" landing in both `outer` and `top`). If no catalog category exists, use an embedding classifier as a fallback. Full code sketch and comparison table in [DATA_SCHEMA.md § 6](DATA_SCHEMA.md#6-category-assignment-strategies). Do the same swap for the runtime hygiene filters (`WINTER_TOKENS` / `GOLF_TOKENS` in `app.py:180`).

**Where to touch**: `scripts/service_reco_weekly_build.py`, `app.py:182` (token filters), infra layer (secret store, scheduler).

### P0.2 Bandit storage upgrade

**Problem**: `app.py:79 DB_LOCK` and the `SELECT then UPDATE` sequence in `BanditDB.feedback` (`app.py:241`) serialise all writes and are not multi-worker safe. SQLite works for one uvicorn process, no further.

**Actions**:
- **Move to Postgres** (or Redis for α/β, Postgres for logs). Keep the `posterior` schema; add row-level locking or use `UPDATE posterior SET alpha = alpha + $delta WHERE ... RETURNING alpha, beta` for atomic increments.
- **Replace SELECT then UPDATE with a single atomic statement**:
  ```sql
  UPDATE posterior
     SET alpha = alpha + $alpha_delta,
         beta  = beta  + $beta_delta,
         n_clicks = n_clicks + $click_delta,
         last_updated = now()
   WHERE cohort_key = $1 AND product_id = $2
  RETURNING alpha, beta;
  ```
- **Multi-worker safety**: after Postgres, uvicorn can run with `--workers > 1` behind a load balancer.
- **Redis alternative**: store α/β as two counters per (cohort_key, product_id) with `HINCRBYFLOAT`. Persist snapshots to Postgres every N seconds for durability.

**Where to touch**: `BanditDB` class (`app.py:81-309`), migrations, connection pooling (`asyncpg` or `psycopg[binary,pool]`).

### P0.3 Feedback-signal reliability

**Problem**: the demo assumes clients fire feedback events fire-and-forget with no retry. Real networks drop packets, apps get backgrounded.

**Actions**:
- **Client-side retry queue**: a persistent queue on device (SQLite / Room / IndexedDB) that flushes when the app comes back online. Bound the queue.
- **Impression-based implicit skip**: if a product was impressioned but no `click / dwell_2s` arrived within a session, treat it as an implicit skip (small β nudge). Requires deriving events server-side from `n_impressions` deltas or from a client "session-ended" heartbeat.
- **Session / user identity**: `session_id` today is device-local. Add a logged-in `user_id` header and merge posteriors when the same user has multiple `session_id`s. Consider a per-user posterior layer on top of per-cohort.

**Where to touch**: client SDK code (out of this repo), the `Feedback` model (`app.py:442`), and a new `POST /api/session-end` event.

### P0.4 Cold-start for new products

**Problem**: `seed_from_lookup()` seeds new products with a prior derived from that cohort's average CTR. That is fine for well-populated cohorts, poor for thin ones and for products with zero window purchases.

**Actions**:
- **Product embeddings for warm-start**: compute an embedding per product (title + brand + category via a sentence transformer or a merchandising taxonomy). For a new product, seed its prior from its nearest neighbours already in the cohort.
- **Hierarchical Bayes for thin cohorts**: pool α, β across cohorts sharing the same style or price bin (partial pooling). This shortens the burn-in for new cohorts and new products.
- **Explicit "new" boost decay**: today's `new_mult = 1.3` is static. Decay it after `T` impressions or after M clicks — otherwise perpetual novelty burns budget.

**Where to touch**: `seed_from_lookup()` (`app.py:133`), a new `embeddings/` module, and possibly `service_reco_weekly_build.py` for the offline pooling.

---

## P1 — First month (do these before you scale marketing)

### P1.1 Monitoring & alerting

- **Metrics**: CTR, CVR, revenue per session, requests per second, response p50 / p95 / p99. Expose a `/metrics` Prometheus endpoint.
- **Bandit drift alarm**: alert when the top-K posterior mean changes by more than X percentiles week over week (indicates a data-quality regression, not real learning).
- **Lookup age alarm**: page when `pull_date` is older than 8 days.
- **Deep `/api/health`**: extend the current shallow check with dependency probes — Postgres reachable, lookup mtime recent, disk usage < 80 %.

### P1.2 Rollback & versioning

- **Lookup archive**: keep every `reco_lookup_YYYY-MM-DD.json`; add a `POST /api/rollback?to=YYYY-MM-DD` for one-command reversion.
- **Auto-revert**: if a fresh build causes `is_fallback` rate to spike, roll back automatically.
- **Posterior snapshots**: daily dump of `posterior` to blob storage. Enables both audit and disaster recovery.

### P1.3 Reward re-tuning

- **Purchase weight**: current `α += 5` per purchase is too close to a click (`α += 1`). In production the ratio should be 20–50× — clicks are cheap, orders are the reward.
- **Price-scaled reward**: weight `purchase` by margin or by `price / cohort_median_price` so the bandit does not prefer cheap items just because they convert more often.
- **A/B holdouts before shipping**: any reward change ships behind an experiment (see P1.5).

Update location: `SIGNAL_UPDATE` in `app.py:246`.

### P1.4 Feedback hygiene

- **Posterior decay**: multiply α and β by 0.9–0.95 weekly to keep the bandit adaptable to seasonal shifts. Apply at weekly-build time inside `seed_from_lookup`.
- **Seasonal flip detection**: automatically detect when the season transitions (build script already sets `SEASON`) and either shrink posteriors toward the seasonal prior or reset the winter-only / summer-only products.

### P1.5 A / B testing infrastructure

- **Holdout arm**: hold back 5–10 % of traffic on a "control" policy (e.g. deterministic top-by-recency). Compare CTR / CVR.
- **Experiment framework**: adopt GrowthBook or Statsig; wire `experiment_id` into the log rows.
- **Bandit hyperparameter tuning**: run experiments on `PRIOR_K`, `brand_max`, purchase weight, explore boost.

---

## P2 — Mature (when the basics work well)

### P2.1 Personalisation tier

Progression:
1. Cohort bandit (today)
2. Contextual bandit (LinUCB / Thompson sampling with features) — feature vector = one-hot cohort + `os_name` + `hour` + user-history embedding
3. Two-tower neural (user tower / item tower) — batch-trained offline, served via ANN index

Each tier is a drop-in replacement for `BanditDB.sample()`.

### P2.2 Business rules

- **Editorial curation**: allow-list / block-list per cohort (pin editor's picks, exclude a brand for legal reasons).
- **New-product boost**: explicit boost curve rather than the static `new_mult`.
- **Brand-diversity constraints**: today `brand_max = 4` is a fixed cap. Make it configurable per cohort or per market.
- **Region / time-slot targeting**: different inventories per store, per weekend vs weekday.

### P2.3 Slate optimisation

The current pipeline scores products independently. Real slates should consider co-recommendation — showing three near-duplicate polos wastes screen space.

Options:
- **PMED** (permutation-based most-efficient decision) or **cascade bandits**
- **Slate MAB** (Ie et al. 2019, Swaminathan et al. 2017)
- **Determinantal point processes** for diversity-aware slates

### P2.4 LLM cost control

`/api/nlu` today calls Claude Haiku on every request when `ANTHROPIC_API_KEY` is set. Add:
- Per-session and per-day token budgets
- Redis-based rate limiting per IP
- Local caching keyed on `hash(text)` (many users say similar things during onboarding)
- Fall back to rule-based (already implemented at `app.py:583`) when budget is exhausted

---

## Suggested sequencing

1. Week 1: P0.1 warehouse + P0.2 Postgres — dev environment
2. Week 2: P0.2 in staging + P0.3 client-side retry
3. Week 3: P0.4 warm-start + P1.1 monitoring
4. Week 4: P1.2 rollback + P1.3 reward re-tuning behind A/B
5. Month 2: P1.4 posterior decay, P1.5 experiment framework
6. Month 3+: P2.1 personalisation tier, then P2.3 slate optimisation once base metrics are stable

---

## Related Files

- [ARCHITECTURE.md](../ARCHITECTURE.md)
- [MODEL_SPEC.md](MODEL_SPEC.md) — current algorithm
- [API_SPEC.md](API_SPEC.md) — endpoints that need extending
- [DATA_SCHEMA.md](DATA_SCHEMA.md) — schemas to migrate to Postgres
- `app.py` — where every change lands
