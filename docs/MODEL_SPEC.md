# Model Specification — Thompson Sampling Bandit + Recency-Weighted Bayesian Pool

**Purpose**: precise reference for the algorithm implemented in `app.py` and `scripts/service_reco_weekly_build.py`. Aimed at ML engineers, reviewers, and anyone auditing the ranking.

---

## 0. One line

A cold-start user is mapped to one of 60 discrete cohorts, each cohort keeps a `Beta(α, β)` posterior per candidate product, `Thompson sampling` selects top-N per request, and every user event updates α or β.

---

## 1. Model family

**Contextual multi-armed bandit** with a discrete context (`cohort_key`).
- Context: `(style, price_bin, item)` — 4 × 3 × 5 = **60 cohorts**
- Arm: candidate product (up to 120 per cohort, refreshed weekly)
- Reward: `click / purchase / skip / dwell_2s`
- Policy: **Thompson sampling** — sample `score ~ Beta(α, β)`, take argmax

Rationale: Thompson sampling gives O(√T log T) Bayesian regret with natural exploration / exploitation balance (Chapelle & Li, 2011).

---

## 2. Cohort definition

### 2.1 Axes
- `style` ∈ `{golf, sports_casual, formal, outdoor}`
- `price` ∈ `{low (~30k), mid (30–80k), high (80k+)}`
- `item` ∈ `{top, bottom, shoes, outer, browse}` — `browse` means "no item filter"

**Key format**: `{style}__{price}__{item}` (e.g. `golf__mid__browse`).

### 2.2 Enum normalisation
Garbage input is clamped to a sensible default (`app.py:368 _normalize_enum`):
- `style` → `sports_casual`
- `price` → `mid`
- `item` → `browse`

### 2.3 Season & style hygiene

Applied inside `bandit.sample()` (`app.py:182`):
- `WINTER_TOKENS` — products with winter-only tokens are excluded when `season == 'summer'`
- `SUMMER_TOKENS` — used to tag summer-oriented reason strings
- `GOLF_TOKENS` — golf products are excluded from non-golf cohorts

---

## 3. Weekly batch — pool generation

### 3.1 Inputs
- 4-week rolling window ending at `PULL_DATE`
- App + web purchase events unioned by user identity via `user_master_coldstart.parquet`

### 3.2 Per-product signals (SQL sketch)

```sql
SELECT
  product_id,
  MIN(brand) AS brand,
  MIN(name)  AS name,
  AVG(price) AS price,
  COUNT(DISTINCT USER_ID) AS n_bought,
  SUM(EXP(-EXTRACT('epoch' FROM (TIMESTAMP '{PULL_DATE}' - purch_ts))
          / (86400.0 * 14))) AS recency_score,
  CASE WHEN MIN(purch_ts) >= TIMESTAMP '{NEW_CUTOFF}' THEN 1 ELSE 0 END AS is_new,
  CASE WHEN MIN(brand) IN ({GOLF_BRANDS_SQL}) OR MAX(cat_golf) > 0 THEN 1 ELSE 0 END AS is_golf,
  (SUM(EXP(...)) + {alpha}) / ((SELECT n FROM n_cohort) + {beta}) AS base_score
FROM cohort_products
GROUP BY product_id
HAVING COUNT(DISTINCT USER_ID) >= 1;
```

### 3.3 Python boost application

```python
golf_mult = 1.5 if style == 'golf' else 0.4
new_mult  = 1.3 if is_new else 1.0
bayes_score = base_score * golf_mult * new_mult
```

### 3.4 Golf-brand auto-detect

Recomputed each build:
```
brand qualifies as GOLF if
  (1) brand name matches regex 'GOLF' / 'Golf' / (locale token), OR
  (2) ≥ 30 % of the brand's window products carry a golf keyword AND the brand has ≥ 3 products
```

Raising the threshold to `n ≥ 5` is on the roadmap.

### 3.5 Season handling

```python
month = PULL_DATE.month
if   month in (12, 1, 2): SEASON, PENALTY = 'winter', 'summer'
elif month in (3, 4, 5):  SEASON, PENALTY = 'spring', None
elif month in (6, 7, 8):  SEASON, PENALTY = 'summer', 'winter'
else:                     SEASON, PENALTY = 'fall',   None
```
If a penalty season is set, the corresponding categorical flag (`cat_winter`, etc.) is filtered out during the build's WHERE clause.

### 3.6 Exploit / Explore split

```
For each cohort:
  Phase A — Exploit (EXPLOIT_N = 70)
    sort by bayes_score desc
    same brand max BRAND_MAX = 15 during selection
    exp_type = 'exploit'

  Phase B — Explore (EXPLORE_N = 50)
    ts_score = Beta(n_bought + 1, cohort_size - n_bought + 1) * (1 + is_new) * (1 + is_golf)
    same brand max BRAND_MAX during selection
    exp_type = 'trending' if is_new else 'explore'
```

Final pool per cohort: up to 120 products, written under the `top12` key in `reco_lookup_*.json` (historical name).

---

## 4. Realtime — Thompson sampling

### 4.1 Sample logic (`app.py:164 BanditDB.sample`)

```python
def sample(cohort_key, k=60, exploration_boost=1.0, brand_max=4, season='summer'):
    rows = SELECT ... FROM posterior WHERE cohort_key = ?
    for row in rows:
        # season / style hygiene filters (WINTER_TOKENS, GOLF_TOKENS)
        boost = 1.3 if row.exp_type == 'explore' else 1.0
        score = rng.beta(row.alpha * boost, row.beta)
        ...
    scored.sort(by score desc)
    # brand diversity: max brand_max per brand
    return top_k
```

### 4.2 K-rescaled weakly-informative prior

Seed formula (`app.py:133 seed_from_lookup`, `PRIOR_K = 100`):
```python
prior_ctr   = n_bought / cohort_size
prior_alpha = prior_ctr * K + 1.0
prior_beta  = (1 - prior_ctr) * K + 1.0
```

Why K = 100:
- K = cohort_size → posterior is so heavy that a single click shifts the mean ~0.02 %. Effectively frozen.
- K = 100 → a click bumps the mean ~1 %. Online learning is actually visible.

### 4.3 Explore boost

Items with `exp_type ∈ {'explore', 'trending'}` sample from `Beta(α * 1.3, β)`. This is optimism in the face of uncertainty — early impressions rise, if clicks fail to arrive β naturally catches up.

### 4.4 Brand diversity

Server-side default `brand_max = 4` per brand across the returned pool. This is looser than the batch's `BRAND_MAX = 15` (which acts across up to 120 items).

### 4.5 iOS soft boost

If `os_name == 'iOS'`:
- Keep the top 3 exploit items in place
- Re-sort the rest of exploit by price desc (premium first)
- Then append the explore items

Rationale: measured higher revenue on iOS in prior offline analysis. This is a demo-grade heuristic — replace with a proper contextual bandit in production (see [roadmap](PRODUCTION_ROADMAP.md)).

---

## 5. Fallback ladder

If Thompson sampling on the primary cohort returns nothing, walk this ladder (`app.py:399`):
1. `{style}__{price}__browse` — relax item
2. `sports_casual__{price}__browse` — relax style, **keep price**
3. `sports_casual__mid__browse` — last resort

The response includes `is_fallback: true` and `matched_key` reports the cohort that actually served results.

If all three levels are empty, the endpoint returns `404`.

---

## 6. Feedback → posterior update

### 6.1 Signal weights

Defined in `app.py:246 SIGNAL_UPDATE`.

| Signal | Update | Reason |
|---|---|---|
| `click` | α += 1.0 | Baseline positive |
| `purchase` | α += 5.0 | Strong positive |
| `skip` | β += 0.5 | Weak negative |
| `dwell_2s` | α += 0.2 | Weak positive |

The 5× purchase-to-click multiplier is a demo default; the [production roadmap](PRODUCTION_ROADMAP.md) recommends 20–50× once purchase volume is high enough.

### 6.2 Impression recording

`GET /api/recommendations` calls `bandit.record_impressions(matched_key, product_ids)` (`app.py:419`). This increments `n_impressions` for each served product; it is used for CTR, dashboards and later reward tuning.

### 6.3 Fallback impression key

Impressions are recorded against `matched_key` (the cohort that actually served the pool) — not the requested `cohort_key`. Recording against the requested key would land on a non-existent posterior row.

---

## 7. Cold start behaviour

### 7.1 First deployment (empty `bandit.db`)
1. Run the weekly build → `reco_lookup_latest.json`
2. On the first request, `LookupCache.get()` calls `seed_from_lookup()` and populates the `posterior` table
3. Subsequent requests sample normally

### 7.2 Weekly refresh
`seed_from_lookup()` only inserts new `(cohort_key, product_id)` pairs. Existing rows are untouched — learning accumulates across weekly rebuilds.

### 7.3 Retired products
Currently not pruned. A product removed from the pool still has its posterior row and can be sampled. Fix on the roadmap: filter rows against the current lookup at sample time or hard-delete on rebuild.

---

## 8. Hyperparameter reference

| Parameter | Default | Where |
|---|---|---|
| `alpha` | 5.0 | build script `--alpha` (Bayesian smoothing numerator) |
| `beta` | 1000.0 | build script `--beta` (denominator) |
| `window_weeks` | 4 | build script `--window-weeks` |
| `top_k` | 30 | build script `--top-k` (SQL LIMIT is 500, cut to 120 in Python) |
| `recency_halflife` | 14 days | build script `--recency-halflife` |
| `new_cutoff_days` | 14 | build script `--new-cutoff-days` |
| `golf_boost` | 1.5 | build script `--golf-boost` (style=golf) |
| `golf_penalty` | 0.4 | build script `--golf-penalty` (other styles) |
| `new_boost` | 1.3 | build script `--new-boost` |
| `PRIOR_K` | 100 | `app.py` `BanditDB.PRIOR_K` |
| `EXPLOIT_N` | 70 | build script |
| `EXPLORE_N` | 50 | build script |
| `TOTAL_N` | 120 | build script |
| `BRAND_MAX (batch)` | 15 | build script |
| `brand_max (serve)` | 4 | `BanditDB.sample()` default |
| `k (serve)` | 60 | `/api/recommendations` default (min 12, max 200) |

---

## 9. Known limitations

- `is_new` misses products whose first sale predates the window
- Golf-brand `n ≥ 3` threshold is prone to false positives — raise to `n ≥ 5`
- Retired products leave orphaned posteriors
- SQLite `SELECT then UPDATE` is not multi-worker safe — move to Postgres and use atomic `UPDATE alpha = alpha + ?`
- Regex-based category tagging is coarse — see [PRODUCTION_ROADMAP.md](PRODUCTION_ROADMAP.md) for the embedding tagger plan

---

## 10. References

- Chapelle, O. & Li, L. (2011). *An Empirical Evaluation of Thompson Sampling.* NIPS.
- Weakly-informative Beta priors for Bayesian A/B testing (Gelman et al., BDA3 Ch. 5).
- Contextual bandits and slate optimisation — see roadmap for the LinUCB / two-tower plan.

---

## Related Files

- `app.py` — bandit, sampling, fallback
- `scripts/service_reco_weekly_build.py` — batch scoring
- [DATA_SCHEMA.md](DATA_SCHEMA.md) — lookup + SQLite schemas
- [API_SPEC.md](API_SPEC.md) — how the algorithm is exposed
- [PRODUCTION_ROADMAP.md](PRODUCTION_ROADMAP.md) — production-grade upgrades
