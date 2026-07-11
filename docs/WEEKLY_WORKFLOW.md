# Weekly Workflow — Jupyter to Auto-Rebuild

**Purpose**: describe exactly what the human operator does each week and what the server does automatically afterwards.

---

## 0. Summary

**Human step (~10 min per week)**:
1. Run your warehouse query (starter SQL lives in `scripts/service_reco_weekly_build.py` and inside the cells shown below).
2. Export the three resulting files.
3. Upload them to `data/incoming/` on the server.
4. Done.

> No warehouse yet? `python scripts/generate_sample_data.py` gives you a
> synthetic lookup so the service boots and you can wire clients before the
> data pipeline is ready.

**Automatic step (no human involvement)**:
- Cron / file-watcher detects the fresh, complete set
- Moves it to `data/raw/`
- Runs `scripts/service_reco_weekly_build.py`
- Atomically swaps `reco_lookup_latest.json`
- Seeds only **new** (cohort × product) posteriors — existing learning is preserved

---

## 1. One-time prerequisites

### 1.1 Warehouse access
- Have credentials for your warehouse (Snowflake, BigQuery, Postgres, …)
- Install the appropriate connector (e.g. `snowflake-connector-python`)

### 1.2 Query environment

Any tool that can run SQL and export CSV / parquet works — Jupyter,
DBeaver, a warehouse console, `duckdb` CLI, or a plain Python script.
The cells in §2.1 show one Python-based flow; adapt to your team's habit.

### 1.3 Authentication
Configure whichever auth mode your warehouse supports:
- SSO / external browser (recommended for interactive use)
- Password + MFA
- Key-pair (for automated runs)

Store credentials via environment variables or your warehouse-vendor config file — never commit them.

---

## 2. Weekly steps

### 2.1 Notebook run

**Cell 1 — Warehouse connection** (example: Snowflake)
```python
import snowflake.connector
conn = snowflake.connector.connect(
    account='...',
    user='...',
    authenticator='externalbrowser',
    warehouse='...',
    database='...',
    schema='...',
    role='...',
)
```

**Cell 2 — Date range**
```python
from datetime import datetime, timedelta
pull_date    = datetime.now().date()
window_start = pull_date - timedelta(weeks=4)
print(f"Pull: {window_start} -> {pull_date}")
```

**Cell 3 — App purchase events**
```python
import pandas as pd

sql_events = f"""
SELECT USER_ID, EVENT_TIMESTAMP, EVENT_CAT_CODE, PRODUCTS_JSON
FROM MY_WAREHOUSE.EVENTS
WHERE EVENT_CAT_CODE LIKE '%airbridge.ecommerce.order.completed'
  AND PRODUCTS_JSON IS NOT NULL
  AND EVENT_TIMESTAMP BETWEEN '{window_start}' AND '{pull_date}'
"""
df_events = pd.read_sql(sql_events, conn)
df_events.to_csv('../data/incoming/events_cohort_slim.csv', index=False)
print(f"events: {len(df_events):,}")
```

**Cell 4 — Web purchase events**
```python
sql_web = f"""
SELECT EVENT_TS, EVENT_CAT_CODE, CLIENT_ID, GOAL_SEMANTIC_JSON
FROM MY_WAREHOUSE.WEB_EVENTS
WHERE EVENT_CAT_CODE LIKE '%airbridge.ecommerce.order.completed'
  AND GOAL_SEMANTIC_JSON IS NOT NULL
  AND EVENT_TS BETWEEN '{window_start}' AND '{pull_date}'
"""
df_web = pd.read_sql(sql_web, conn)
df_web.to_csv('../data/incoming/web_events.csv', index=False)
print(f"web events: {len(df_web):,}")
```

**Cell 5 — User master**
```python
sql_master = """
SELECT USER_ID, WEB_USER_UUID, INSTALL_TS, ATT_STATUS, PLATFORM
FROM MY_WAREHOUSE.USER_MASTER_COLDSTART
"""
df_master = pd.read_sql(sql_master, conn)
df_master.to_parquet('../data/incoming/user_master_coldstart.parquet', index=False)
print(f"master: {len(df_master):,}")
```

**Cell 6 — Sanity check**
```python
assert len(df_events) > 100, 'events too few'
assert len(df_web)    > 100, 'web too few'
assert len(df_master) > 100, 'master too few'
print("three files ready")
```

**Cell 7 — Upload (optional automation)**
```python
import subprocess
subprocess.run([
    'scp',
    '../data/incoming/events_cohort_slim.csv',
    '../data/incoming/web_events.csv',
    '../data/incoming/user_master_coldstart.parquet',
    'user@reco.example.com:/data/incoming/',
])
```
Alternative upload paths: `rsync`, S3 client, SFTP client, or `flyctl ssh sftp`.

### 2.2 Wall-clock

| Step | Time |
|---|---|
| Warehouse pull (cells 3–5) | 3–5 min |
| Sanity check | seconds |
| Upload | 1–3 min |
| **Total** | **5–10 min** |

---

## 3. Server-side automation

### 3.1 Cron watcher (recommended, simple)

Drop the sample `check_and_build.sh` below onto the server (a good spot is
`/opt/reco/check_and_build.sh` or your project's `scripts/`), `chmod +x`,
then register a cron entry:

`/etc/cron.d/reco-check`:
```
*/5 * * * * ubuntu /opt/reco/check_and_build.sh >> /var/log/reco_watcher.log 2>&1
```

Sample `check_and_build.sh` (copy verbatim, tune paths):
```bash
#!/bin/bash
INCOMING=/data/incoming
RAW=/data/raw
FILES=("events_cohort_slim.csv" "web_events.csv" "user_master_coldstart.parquet")

for f in "${FILES[@]}"; do
  [ -f "$INCOMING/$f" ] || exit 0
  AGE=$(( $(date +%s) - $(stat -c %Y "$INCOMING/$f") ))
  [ $AGE -gt 3600 ] && exit 0      # stale files — skip
done

echo "[$(date)] new files detected, building..."
mv "$INCOMING"/*.csv "$INCOMING"/*.parquet "$RAW/"

# already at repo root
/app/service_app/.venv/bin/python scripts/service_reco_weekly_build.py
STATUS=$?

if [ $STATUS -eq 0 ]; then
  echo "[$(date)] build OK"
  [ -n "$SLACK_WEBHOOK" ] && curl -X POST "$SLACK_WEBHOOK" -d '{"text":"weekly reco build OK"}'
else
  echo "[$(date)] build FAILED"
  mkdir -p "$INCOMING/failed"
  mv "$RAW"/*.csv "$RAW"/*.parquet "$INCOMING/failed/"
  [ -n "$SLACK_WEBHOOK" ] && curl -X POST "$SLACK_WEBHOOK" -d '{"text":"weekly reco build FAILED"}'
fi
```

### 3.2 Alternative: systemd + inotify (instant pickup)
```
[Unit]
Description=Onboarding Recommendation Reco File Watcher

[Service]
ExecStart=/app/service_app/scripts/watcher_daemon.sh
Restart=always
```
`watcher_daemon.sh` calls `inotifywait -m /data/incoming -e close_write` and triggers the same build logic.

### 3.3 Build execution flow
```
1. verify /data/incoming has the three expected files
2. move to /data/raw
3. run service_reco_weekly_build.py (~5 min)
4. write reco_lookup_YYYY-MM-DD.json
5. atomic rename → reco_lookup_latest.json
6. FastAPI LookupCache picks it up on the next request (mtime detect)
7. seed_from_lookup() inserts ONLY new (cohort, product) posteriors
8. optional Slack alert
```

### 3.4 Delta report
The build script writes `data/reco_lookup/delta_YYYY-MM-DD.json`:
```json
[
  {
    "cohort": "golf__mid__browse",
    "new": ["P0099887", "P0099888"],
    "dropped": ["P0011122"],
    "this_cohort_size": 385,
    "last_cohort_size": 372
  }
]
```
Useful for tracking pool churn and diagnosing sudden coverage regressions.

---

## 4. Failure handling

### 4.1 Log locations
- `/var/log/reco_watcher.log` — watcher stdout / stderr
- `/var/log/reco_build.log` — build script output
- Slack channel — real-time alerts (if configured)

### 4.2 Failure quarantine
Bad inputs are moved to `/data/incoming/failed/`. The previous `reco_lookup_latest.json` remains live, so users see no downtime.

### 4.3 Common causes
- Warehouse column names renamed → sanity check fails
- CSV not UTF-8 → DuckDB read fails
- Products JSON schema changed → `json_extract` fails
- Sparse 4-week window (particularly for niche cohorts)

---

## 5. FAQ

**Does the batch have to run on Mondays?** No. Any day of the week — pick one and set the cron accordingly.

**Can I run it daily?** Yes. Each run takes ~5 minutes. The main trade-off is warehouse load.

**What if we skip a week?** The previous lookup stays live. New products just enter with a one-week lag. Realtime learning continues uninterrupted.

**Are old lookup files auto-deleted?** No. Delete manually (`rm reco_lookup_202[45]*.json`) when disk pressure appears. The `_latest.json` pointer is always up to date.

**Can the server pull directly from the warehouse, skipping Jupyter?** Yes, once credential management is set up. See the [production roadmap](PRODUCTION_ROADMAP.md).

**When do we reset `bandit.db`?** Never in production. In development, `rm data/bandit.db` re-seeds from the current lookup on the next request.

---

## Related Files

- `scripts/service_reco_weekly_build.py`
- `scripts/generate_sample_data.py`
- [DATA_SCHEMA.md](DATA_SCHEMA.md)
- [MODEL_SPEC.md](MODEL_SPEC.md)
- [DEPLOY.md](DEPLOY.md)
- [PRODUCTION_ROADMAP.md](PRODUCTION_ROADMAP.md)
