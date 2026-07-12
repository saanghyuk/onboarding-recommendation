# Data Schema

**Purpose**: describe the three warehouse extracts the weekly batch consumes, the `reco_lookup_*.json` it produces, and the SQLite tables the FastAPI server maintains.

---

## 1. Warehouse extracts (weekly input)

| File | Warehouse source | Format | Required columns | Refresh |
|---|---|---|---|---|
| `events_cohort_slim.csv` | `EVENTS` table | CSV | 4 | Weekly |
| `web_events.csv` | `WEB_EVENTS` table | CSV | 4 | Weekly |
| `user_master_coldstart.parquet` | `USER_MASTER` table | Parquet | 2 | Weekly (or monthly) |

Storage path:
- Jupyter writes to `service_app/data/incoming/`
- The file watcher moves complete sets to `service_app/data/raw/` before running the build

### 1.1 `events_cohort_slim.csv` — app purchase events

| Column | Type | Notes |
|---|---|---|
| `USER_ID` | string | App-side user identifier |
| `EVENT_TIMESTAMP` | timestamp | Keep KST or UTC consistent within a batch |
| `EVENT_CAT_CODE` | string | e.g. `airbridge.ecommerce.order.completed` |
| `PRODUCTS_JSON` | string (JSON array) | See schema below |

`PRODUCTS_JSON` element schema (must include `productID`, `name`, `brandName`, `price`):
```json
[
  {
    "productID": "P0012345",
    "name": "Summer Cool-Touch Polo",
    "brandID": "B0087",
    "brandName": "SampleBrand",
    "price": 33000,
    "quantity": 1
  }
]
```

Reference SQL:
```sql
SELECT USER_ID, EVENT_TIMESTAMP, EVENT_CAT_CODE, PRODUCTS_JSON
FROM MY_WAREHOUSE.EVENTS
WHERE EVENT_CAT_CODE LIKE '%airbridge.ecommerce.order.completed'
  AND PRODUCTS_JSON IS NOT NULL
  AND EVENT_TIMESTAMP BETWEEN DATEADD(WEEK, -4, CURRENT_TIMESTAMP()) AND CURRENT_TIMESTAMP();
```

### 1.2 `web_events.csv` — web purchase events

| Column | Type | Notes |
|---|---|---|
| `EVENT_TS` | timestamp | Web event time |
| `EVENT_CAT_CODE` | string | Same category filter as app events |
| `CLIENT_ID` | string | Cookie / fingerprint |
| `GOAL_SEMANTIC_JSON` | string (JSON object) | See below |

`GOAL_SEMANTIC_JSON` shape:
```json
{
  "products": [
    {"productID": "P0012345", "name": "Summer Cool-Touch Polo",
     "brandID": "B0087", "brandName": "SampleBrand",
     "price": 33000, "quantity": 1}
  ],
  "total_amount": 33000,
  "order_id": "ORD-2026-07-10-001"
}
```

Reference SQL:
```sql
SELECT EVENT_TS, EVENT_CAT_CODE, CLIENT_ID, GOAL_SEMANTIC_JSON
FROM MY_WAREHOUSE.WEB_EVENTS
WHERE EVENT_CAT_CODE LIKE '%airbridge.ecommerce.order.completed'
  AND GOAL_SEMANTIC_JSON IS NOT NULL
  AND EVENT_TS BETWEEN DATEADD(WEEK, -4, CURRENT_TIMESTAMP()) AND CURRENT_TIMESTAMP();
```

DuckDB uses `json_extract(GOAL_SEMANTIC_JSON, '$.products')` to reach the array.

### 1.3 `user_master_coldstart.parquet` — user profile

Required:

| Column | Type | Notes |
|---|---|---|
| `USER_ID` | string | Joins to `EVENTS.USER_ID` |
| `WEB_USER_UUID` | string | Joins to `WEB_EVENTS.CLIENT_ID` |

Optional (nice to have):
`INSTALL_TS`, `ATT_STATUS`, `PLATFORM`, `AD_CHANNEL`.

The build script uses this table to fold web purchases into the same identity when possible.

### 1.4 Quality checks (run at the end of the notebook)

```python
assert len(df_events) > 100
assert len(df_web)    > 100
assert len(df_master) > 100

import json
sample = df_events['PRODUCTS_JSON'].dropna().iloc[0]
parsed = json.loads(sample)
assert isinstance(parsed, list) and len(parsed) > 0
assert {'productID', 'brandName', 'price'} <= set(parsed[0])

sample_web = df_web['GOAL_SEMANTIC_JSON'].dropna().iloc[0]
assert 'products' in json.loads(sample_web)

assert df_master['USER_ID'].notna().sum() > 0
assert df_master['WEB_USER_UUID'].notna().sum() > 0
print("data quality OK")
```

---

## 2. `reco_lookup_latest.json` (weekly output)

Written by `scripts/service_reco_weekly_build.py` to `data/reco_lookup/`. Shape (simplified):

```json
{
  "meta": {
    "pull_date": "2026-07-10",
    "window_weeks": 4,
    "season": "summer",
    "total_cohorts": 60,
    "total_first_purchase_users": 15393,
    "n_golf_brands_detected": 42
  },
  "cohorts": {
    "golf__mid__browse": {
      "cohort_size": 385,
      "top12": [
        {
          "product_id": "P0012345",
          "brand": "SampleBrand",
          "name": "Summer Cool-Touch Polo",
          "price": 33000,
          "n_bought": 34,
          "is_new": 0,
          "is_golf": 1,
          "exp_type": "exploit",
          "bayes_score": 0.132
        }
      ]
    }
  }
}
```

`top12` is the historical field name; the array can hold up to 120 products (70 exploit + 50 explore). The `/swipe` route inlines a slim version of this JSON as `window.RECO_LOOKUP` (see `app.py:530`).

---

## 3. SQLite (`data/bandit.db`)

Schema is created lazily in `BanditDB._init_db` (`app.py:92`). WAL journaling is enabled.

### 3.1 `posterior`

| Column | Type | Notes |
|---|---|---|
| `cohort_key` | TEXT (PK part) | e.g. `golf__mid__browse` |
| `product_id` | TEXT (PK part) | Warehouse product ID |
| `brand` | TEXT | Copied from the lookup at seed time |
| `name` | TEXT | Product name |
| `price` | INTEGER | Won |
| `alpha` | REAL, default 1.0 | Beta α (successes) |
| `beta` | REAL, default 1.0 | Beta β (failures) |
| `n_impressions` | INTEGER, default 0 | Incremented per served card |
| `n_clicks` | INTEGER, default 0 | Incremented on `click` signal |
| `n_purchases` | INTEGER, default 0 | Incremented on `purchase` signal |
| `n_skips` | INTEGER, default 0 | Incremented on `skip` signal |
| `exp_type` | TEXT, default `'exploit'` | `exploit` · `explore` · `trending` |
| `seeded_from` | TEXT | `pull_date` at seed time |
| `last_updated` | TEXT | ISO timestamp |

Primary key: `(cohort_key, product_id)`. Index on `cohort_key`.

Seeding formula (`app.py:133 seed_from_lookup`):
```
prior_ctr   = n_bought / cohort_size
prior_alpha = prior_ctr * K + 1.0       # K = 100
prior_beta  = (1 - prior_ctr) * K + 1.0
```
Only *new* `(cohort_key, product_id)` pairs are inserted; existing posteriors are preserved across weekly rebuilds.

### 3.2 `feedback_log`

Append-only event log.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK autoincrement | |
| `ts` | TEXT | ISO timestamp |
| `session_id` | TEXT | UUID from the client |
| `cohort_key` | TEXT | |
| `product_id` | TEXT | |
| `signal` | TEXT | `click` · `purchase` · `skip` · `dwell_2s` |
| `alpha_after` | REAL | α after applying the update |
| `beta_after` | REAL | β after applying the update |

Index on `ts`.

---

## 4. Quiz log (`logs/quiz_log_YYYY-MM-DD.jsonl`)

One JSON object per line, produced by `POST /api/quiz-log`:
```json
{"session_id":"abc","device":{"os":"iOS"},"answers":{"style":"golf"},
 "step":"q1_selected","ts_client":"2026-07-12T00:00:00Z",
 "ts_server":"2026-07-12T00:00:01.234"}
```

---

## 5. Schema evolution

When warehouse column names change:
- Update the SQL in whatever query tool you use to produce the three exports (Jupyter, DBeaver, warehouse console, `duckdb` CLI, …)
- Update the top-of-file constants in `scripts/service_reco_weekly_build.py`
- Re-run the query and confirm the build script still writes a fresh lookup

The minimum contract is EVENTS 4 columns · WEB 4 columns · MASTER 2 columns.

---

## 6. Category assignment strategies

Cohorts are `{style}__{price}__{item}`. Both `style` and `item` require each product to carry a category tag. There are three ways to produce those tags — pick the one that fits your commerce stack.

### Option A · Use your product catalog's `category` column  ✅ recommended

Most commerce backends already store a category taxonomy per SKU
(`men > tops > polo`, `home > lighting > pendant`, …). This is the fastest and
most accurate path — no NLP, no false positives from mixed-keyword product
names ("blouson t-shirt", "cardigan shirt", etc.).

Replace the `CATEGORY_RULES` regex block in
`scripts/service_reco_weekly_build.py` (around line 121) with a JOIN against
your catalog:

```python
# Before (regex tagger, default in this repo):
cat_case_when = ',\n'.join([
    f"CASE WHEN regexp_matches(name, '{p}', 'i') THEN 1 ELSE 0 END AS cat_{cat}"
    for cat, p in CATEGORY_RULES.items()
])
c.execute(f"CREATE OR REPLACE TABLE pp_tagged AS SELECT *, {cat_case_when} FROM pp")

# After (catalog JOIN, recommended):
c.execute("""
CREATE OR REPLACE TABLE pp_tagged AS
SELECT
  pp.*,
  CASE WHEN cat.style_l1  = 'golf'          THEN 1 ELSE 0 END AS cat_golf,
  CASE WHEN cat.style_l1  = 'outdoor'       THEN 1 ELSE 0 END AS cat_outdoor,
  CASE WHEN cat.style_l1  = 'formal'        THEN 1 ELSE 0 END AS cat_formal,
  CASE WHEN cat.style_l1  = 'sports_casual' THEN 1 ELSE 0 END AS cat_sports,
  CASE WHEN cat.item_type = 'top'           THEN 1 ELSE 0 END AS cat_top,
  CASE WHEN cat.item_type = 'bottom'        THEN 1 ELSE 0 END AS cat_bottom,
  CASE WHEN cat.item_type = 'shoes'         THEN 1 ELSE 0 END AS cat_shoes,
  CASE WHEN cat.item_type = 'outer'         THEN 1 ELSE 0 END AS cat_outer,
  CASE WHEN cat.season    = 'summer'        THEN 1 ELSE 0 END AS cat_summer,
  CASE WHEN cat.season    = 'winter'        THEN 1 ELSE 0 END AS cat_winter
FROM pp
LEFT JOIN my_warehouse.product_catalog cat USING (product_id)
""")
```

That single JOIN eliminates the mixed-keyword misclassification (blouson polo
tagged as outer, cardigan shirt tagged as top+outer, etc.) that a regex tagger
suffers from.

You should also replace the same categories inside `bandit.sample()`
(`app.py:180`) with catalog-driven filters — the `WINTER_TOKENS`,
`SUMMER_TOKENS`, `GOLF_TOKENS` sets are the runtime hygiene filter and use
the same taxonomy.

### Option B · Regex tagger (the default this repo ships with)

Used only when no product catalog is available. Fast to author, cheap to run,
but noisy on product names that mix categories. Extend the `CATEGORY_RULES`
dictionary in `scripts/service_reco_weekly_build.py` with your own vocabulary
(any language — regex matches any Unicode).

Known limitations:

- **False positives** — "블루종 티셔츠" hits both `outer` (blouson) and `top`
  (t-shirt) and shows up in both cohorts.
- **False negatives** — season-specific or trending keywords ("쿨링 팬츠",
  "cooling shorts") that were not anticipated get dropped from the summer
  boost.
- **Maintenance drift** — every new season / new-arrival wave needs a
  vocabulary refresh.

Use this as a **placeholder while your catalog integration is in progress**,
not as a permanent solution.

### Option C · Embedding tagger (advanced)

For catalogs where the category field is missing or inconsistent, replace the
regex step with a similarity classifier:

- Encode each product's `title + brand` (and optionally the image) with a
  frozen sentence / vision model. Suggested:
  - Korean text → **Ko-SBERT**, **KoSimCSE**, or Anthropic embeddings
  - Multi-lingual text → **multilingual-e5-large**, **BGE-M3**
  - Image → **CLIP** (`clip-vit-large-patch14`)
- Compute a centroid per known category from labelled seed products
  (~30 examples per category is enough)
- Assign each unknown product to the nearest centroid above a similarity
  threshold; leave low-confidence ones untagged

Typical accuracy: 90 %+ on Korean fashion catalogs vs ~65 % for the regex
baseline in this repo. Cost is a one-time embedding job (a few hundred
thousand products in ~30 minutes on a single GPU) plus refresh whenever new
SKUs land.

Cache the embeddings in your warehouse alongside the catalog so the weekly
build stays a plain SQL step.

---

### Summary table

| Option | Accuracy | Effort | When to use |
|---|---|---|---|
| **A. Catalog JOIN** | Highest (your ground truth) | Low — one JOIN | You have a category column. **Default recommendation.** |
| **B. Regex tagger** | Medium; noisy on mixed names | Low — extend the dict | Bootstrap, MVP, or fallback when no catalog exists |
| **C. Embedding tagger** | High (~90 %+) | Medium — one-time embed job | No catalog and mixed-keyword catalogue that regex mis-tags |

---

## Related Files

- `scripts/service_reco_weekly_build.py`
- `scripts/generate_sample_data.py`
- `app.py` — `BanditDB` class
- [WEEKLY_WORKFLOW.md](WEEKLY_WORKFLOW.md)
- [MODEL_SPEC.md](MODEL_SPEC.md)
