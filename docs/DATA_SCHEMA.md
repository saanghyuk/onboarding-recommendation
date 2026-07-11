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
- Update the SQL in `notebooks/weekly_data_pull.ipynb`
- Update the top-of-file constants in `scripts/service_reco_weekly_build.py`
- Rerun the notebook and confirm the build script still writes a fresh lookup

The minimum contract is EVENTS 4 columns · WEB 4 columns · MASTER 2 columns.

---

## 6. Product metadata gaps

Raw payloads only carry brand, name, price. Season / occasion / style tags are absent. Current mitigations:
- Regex-based `CATEGORY_RULES` in the build script (seasonal keywords, golf tokens)
- `WINTER_TOKENS` / `SUMMER_TOKENS` / `GOLF_TOKENS` filters inside `bandit.sample()` (`app.py:182`)

The [production roadmap](PRODUCTION_ROADMAP.md) covers evolving this to an embedding-based tagger and a product-catalogue sync.

---

## Related Files

- `notebooks/weekly_data_pull.ipynb`
- `scripts/service_reco_weekly_build.py`
- `app.py` — `BanditDB` class
- [WEEKLY_WORKFLOW.md](WEEKLY_WORKFLOW.md)
- [MODEL_SPEC.md](MODEL_SPEC.md)
