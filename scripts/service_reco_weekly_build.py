"""
Weekly Reco Batch - rebuild the per-product personalized recommendation lookup.
Runs automatically every Monday at 04:00 KST via cron.

Usage:
  python service_reco_weekly_build.py                        # today, 4-week window
  python service_reco_weekly_build.py --pull-date=2026-07-10 # specific date
  python service_reco_weekly_build.py --window-weeks=8       # 8-week window
"""
import argparse
import duckdb
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

parser = argparse.ArgumentParser()
parser.add_argument('--pull-date', default=None, help='reference date YYYY-MM-DD (default: today)')
parser.add_argument('--window-weeks', type=int, default=4, help='rolling window in weeks (default: 4)')
parser.add_argument('--top-k', type=int, default=30, help='top-K products stored per cohort (default: 30)')
parser.add_argument('--alpha', type=float, default=5.0, help='Bayesian prior alpha')
parser.add_argument('--beta', type=float, default=1000.0, help='Bayesian prior beta')
parser.add_argument('--recency-halflife', type=float, default=14.0, help='recency decay half-life in days (weight for recent sales)')
parser.add_argument('--new-cutoff-days', type=int, default=14, help='first-sale within this many days marks is_new')
parser.add_argument('--category-boost', type=float, default=1.5, help='matching-category product boost inside a category cohort')
parser.add_argument('--category-penalty', type=float, default=0.4, help='category product penalty outside a matching cohort')
parser.add_argument('--new-boost', type=float, default=1.3, help='is_new product boost')
args = parser.parse_args()

if args.pull_date:
    PULL_DATE = pd.to_datetime(args.pull_date)
else:
    PULL_DATE = pd.Timestamp.now().normalize()
WINDOW_START = PULL_DATE - pd.Timedelta(weeks=args.window_weeks)

print(f"[BUILD] Pull date = {PULL_DATE.date()} | Window = {WINDOW_START.date()} -> {PULL_DATE.date()} ({args.window_weeks} weeks)")

# Relative to repo root (parent of scripts/) so it runs from any cwd
REPO_ROOT = Path(__file__).parent.parent
OUT_DIR = REPO_ROOT / "data" / "reco_lookup"
OUT_DIR.mkdir(parents=True, exist_ok=True)

c = duckdb.connect()
c.execute("PRAGMA memory_limit='6GB'")
c.execute("PRAGMA threads=8")

EVENTS = str(REPO_ROOT / "data" / "raw" / "events_cohort_slim.csv")
WEB    = str(REPO_ROOT / "data" / "raw" / "web_events.csv")
MASTER = str(REPO_ROOT / "data" / "processed" / "user_master_coldstart.parquet")

# =================================================================
# 1. Unpack purchase events, apply window filter
# =================================================================
print(f"\n[STEP 1] App + web purchase events, window filter: {WINDOW_START.date()} -> {PULL_DATE.date()}")

# App order events
c.execute(f"""
CREATE OR REPLACE TABLE purch_app AS
SELECT USER_ID, EVENT_TIMESTAMP AS purch_ts, PRODUCTS_JSON AS products_raw, 'app' AS src
FROM read_csv('{EVENTS}', header=true, sample_size=1000000)
WHERE EVENT_CAT_CODE LIKE '%airbridge.ecommerce.order.completed'
  AND PRODUCTS_JSON IS NOT NULL
  AND EVENT_TIMESTAMP BETWEEN TIMESTAMP '{WINDOW_START}' AND TIMESTAMP '{PULL_DATE}'
""")
# Web order events (products live inside GOAL_SEMANTIC_JSON.products)
c.execute(f"""
CREATE OR REPLACE TABLE purch_web AS
SELECT
  COALESCE(m.USER_ID, 'web_' || w.CLIENT_ID) AS USER_ID,
  w.EVENT_TS AS purch_ts,
  json_extract(w.GOAL_SEMANTIC_JSON, '$.products')::VARCHAR AS products_raw,
  'web' AS src
FROM read_csv('{WEB}', header=true, sample_size=1000000) w
LEFT JOIN '{MASTER}' m ON w.CLIENT_ID = m.WEB_USER_UUID
WHERE w.EVENT_CAT_CODE LIKE '%airbridge.ecommerce.order.completed'
  AND w.GOAL_SEMANTIC_JSON IS NOT NULL
  AND w.EVENT_TS BETWEEN TIMESTAMP '{WINDOW_START}' AND TIMESTAMP '{PULL_DATE}'
""")
n_app = c.execute("SELECT COUNT(*) FROM purch_app").fetchone()[0]
n_web = c.execute("SELECT COUNT(*) FROM purch_web").fetchone()[0]
print(f"  - App orders = {n_app:,} | Web orders = {n_web:,} | Union = {n_app + n_web:,}")

# Union app + web
c.execute("""
CREATE OR REPLACE TABLE purch AS
SELECT USER_ID, purch_ts, products_raw, src FROM purch_app
UNION ALL
SELECT USER_ID, purch_ts, products_raw, src FROM purch_web
WHERE products_raw IS NOT NULL AND LENGTH(products_raw) > 2
""")
n_purch = c.execute("SELECT COUNT(*) FROM purch").fetchone()[0]
n_purch_u = c.execute("SELECT COUNT(DISTINCT USER_ID) FROM purch").fetchone()[0]
print(f"  - Total window purchases = {n_purch:,} | Buyers = {n_purch_u:,}")

# Unpack (both schemas are arrays of product objects)
c.execute("""
CREATE OR REPLACE TABLE pp AS
SELECT
  p.USER_ID, p.purch_ts, p.src,
  json_extract_string(prod, '$.productID') AS product_id,
  json_extract_string(prod, '$.name')      AS name,
  json_extract_string(prod, '$.brandID')   AS brand_id,
  json_extract_string(prod, '$.brandName') AS brand,
  CAST(json_extract(prod, '$.price') AS DOUBLE) AS price,
  CAST(json_extract(prod, '$.quantity') AS INT) AS qty
FROM purch p, UNNEST(CAST(json(p.products_raw) AS JSON[])) AS t(prod)
WHERE json_extract_string(prod, '$.brandName') IS NOT NULL
  AND p.purch_ts IS NOT NULL
""")
n_pp = c.execute("SELECT COUNT(*) FROM pp").fetchone()[0]
n_prod = c.execute("SELECT COUNT(DISTINCT product_id) FROM pp").fetchone()[0]
n_brand = c.execute("SELECT COUNT(DISTINCT brand) FROM pp").fetchone()[0]
print(f"  - Total product lines = {n_pp:,} | unique products = {n_prod:,} | brands = {n_brand:,}")
src_split = c.execute("SELECT src, COUNT(*) FROM pp GROUP BY src").fetchall()
for s, n in src_split:
    print(f"    - {s}: {n:,}")

# Category tagging (English keyword regexes; extend as needed for your catalog)
CATEGORY_RULES = {
    'golf':    r'golf|Golf|GOLF|driver|iron|putter|caddie',
    'outdoor': r'outdoor|Outdoor|hiking|trekking|windbreaker|Windbreaker|trail|climbing',
    'formal':  r'suit|Suit|dress shirt|blazer|formal|tie|loafer|oxford',
    'sports':  r'sport|Sport|running|gym|training|fitness|soccer|basketball|baseball|sneaker|athletic',
    'casual':  r't-?shirt|short sleeve|sweatshirt|hoodie|casual|polo|pique',
    # Item axis
    'top':     r't-?shirt|short sleeve|long sleeve|sweatshirt|hoodie|polo|shirt|knit|sweater|blouse|inner',
    'bottom':  r'pants|trouser|slacks|shorts|chino|jogger|cargo|denim|jeans',
    'shoes':   r'sneaker|shoe|boot|sandal|slipper|loafer|golf shoe|trekking shoe',
    'outer':   r'padding|jacket|coat|windbreaker|down|parka|cardigan|blazer',
    # Season
    'summer':  r'cool|cooling|ice|summer|linen|short sleeve|shorts|sandal|light|lightweight',
    'winter':  r'wool|cashmere|fleece|warm|winter|padding|down|heavy|parka',
}

cat_case_when = ',\n'.join([f"  CASE WHEN regexp_matches(name, '{pattern}', 'i') THEN 1 ELSE 0 END AS cat_{cat}" for cat, pattern in CATEGORY_RULES.items()])
c.execute(f"CREATE OR REPLACE TABLE pp_tagged AS SELECT *, {cat_case_when} FROM pp")

# =================================================================
# 2. First purchase per user, build preference vector
# =================================================================
print(f"\n[STEP 2] First-purchase pull and preference vector")

c.execute("""
CREATE OR REPLACE TABLE first_p AS
WITH r AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY USER_ID ORDER BY purch_ts) AS rn FROM pp_tagged)
SELECT * FROM r WHERE rn = 1
""")
n_fp = c.execute("SELECT COUNT(*) FROM first_p").fetchone()[0]
print(f"  - First-purchase lines N = {n_fp:,}")

c.execute("""
CREATE OR REPLACE TABLE user_pref AS
WITH r AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY USER_ID ORDER BY purch_ts) AS rn FROM pp_tagged)
SELECT
  USER_ID,
  AVG(price) AS avg_price,
  SUM(cat_golf) AS n_golf, SUM(cat_outdoor) AS n_outdoor,
  SUM(cat_formal) AS n_formal, SUM(cat_sports) AS n_sports, SUM(cat_casual) AS n_casual,
  SUM(cat_top) AS n_top, SUM(cat_bottom) AS n_bottom, SUM(cat_shoes) AS n_shoes, SUM(cat_outer) AS n_outer,
  SUM(cat_summer) AS n_summer, SUM(cat_winter) AS n_winter
FROM r WHERE rn <= 3
GROUP BY USER_ID
""")

# =================================================================
# 3. Per-cohort top-K products: Bayesian smoothed + freshness + diversity
# =================================================================
print(f"\n[STEP 3] Pulling top-{args.top_k} per cohort")

# Season rule: determine current season
month = PULL_DATE.month
if month in [12, 1, 2]:
    SEASON = 'winter'; SEASON_BOOST = 'winter'; SEASON_PENALTY = 'summer'
elif month in [3, 4, 5]:
    SEASON = 'spring'; SEASON_BOOST = None; SEASON_PENALTY = None
elif month in [6, 7, 8]:
    SEASON = 'summer'; SEASON_BOOST = 'summer'; SEASON_PENALTY = 'winter'
else:
    SEASON = 'fall'; SEASON_BOOST = None; SEASON_PENALTY = None
print(f"  - Current season = {SEASON} | boost={SEASON_BOOST} | penalty={SEASON_PENALTY}")

NEW_CUTOFF = PULL_DATE - pd.Timedelta(days=args.new_cutoff_days)
print(f"  - new_cutoff = {NEW_CUTOFF.date()}")
print(f"  - Recency half-life = {args.recency_halflife}d | category_boost={args.category_boost} | category_penalty={args.category_penalty} | new_boost={args.new_boost}")

# ---------------- Auto-detect golf brands from data ----------------
# For each brand, compute the fraction of products that match golf keywords; if the
# fraction exceeds a threshold (or the brand name itself contains "GOLF"/"Golf"),
# treat the brand as a golf brand.
c.execute("""
CREATE OR REPLACE TABLE brand_golf_stats AS
SELECT brand,
  COUNT(DISTINCT product_id) AS n_products,
  SUM(CASE WHEN cat_golf > 0 THEN 1 ELSE 0 END) AS n_golf_products,
  SUM(CASE WHEN cat_golf > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(DISTINCT product_id) AS golf_ratio,
  CASE WHEN regexp_matches(brand, 'GOLF|Golf', 'i') THEN 1 ELSE 0 END AS brand_name_golf
FROM pp_tagged
GROUP BY brand
""")

# Data-driven golf brand:
#   (1) brand name contains GOLF/Golf, OR
#   (2) >=30% of the brand's products match golf keywords AND the brand has >=3 products
GOLF_BRAND_ROWS = c.execute("""
SELECT brand FROM brand_golf_stats
WHERE brand_name_golf = 1
   OR (golf_ratio >= 0.3 AND n_products >= 3)
""").fetchall()
GOLF_BRANDS = {r[0] for r in GOLF_BRAND_ROWS}
GOLF_BRANDS_SQL = ", ".join([f"'{b.replace(chr(39), chr(39)+chr(39))}'" for b in GOLF_BRANDS]) if GOLF_BRANDS else "''"
print(f"  - Auto-detected golf brands = {len(GOLF_BRANDS)} (name match OR golf_ratio>=30% and n>=3)")
print(f"    sample (up to 20): {sorted(list(GOLF_BRANDS))[:20]}")

# Cohort definition: Q1 style (4) x Q2 price (3) x Q3 item (5). User answers are multi-select.
# Precompute top-K sub-cohorts for each combination.
STYLES = ['golf', 'sports_casual', 'formal', 'outdoor']
PRICE_BINS = ['low', 'mid', 'high']
ITEMS = ['top', 'bottom', 'shoes', 'outer', 'browse']

# Style mapping (sports and casual are merged)
STYLE_COLS = {
    'golf': ['n_golf'],
    'sports_casual': ['n_sports', 'n_casual'],
    'formal': ['n_formal'],
    'outdoor': ['n_outdoor'],
}
ITEM_COLS = {
    'top': ['n_top'], 'bottom': ['n_bottom'], 'shoes': ['n_shoes'], 'outer': ['n_outer'],
    'browse': None,  # no filter
}
PRICE_RANGES = {
    'low': (0, 30000), 'mid': (30000, 80000), 'high': (80000, 5000000),
}

lookup = {}
for style in STYLES:
    for price_bin in PRICE_BINS:
        for item in ITEMS:
            key = f"{style}__{price_bin}__{item}"
            style_where = " OR ".join([f"up.{col} > 0" for col in STYLE_COLS[style]])
            price_lo, price_hi = PRICE_RANGES[price_bin]
            price_where = f"up.avg_price BETWEEN {price_lo} AND {price_hi}"
            if ITEM_COLS[item] is None:
                item_where = "1=1"
            else:
                item_where = " OR ".join([f"fp.cat_{col.replace('n_','')} > 0" for col in ITEM_COLS[item]])

            # Season filter on product
            if SEASON_PENALTY == 'winter':
                season_where = "fp.cat_winter = 0"  # if summer, exclude winter products
            elif SEASON_PENALTY == 'summer':
                season_where = "fp.cat_summer = 0"
            else:
                season_where = "1=1"

            # Main query: recency-weighted + is_new + is_golf tags
            query = f"""
            WITH cohort AS (
              SELECT DISTINCT USER_ID FROM user_pref up WHERE ({style_where}) AND ({price_where})
            ),
            cohort_products AS (
              SELECT c.USER_ID, fp.product_id, fp.brand, fp.name, fp.price, fp.purch_ts,
                     fp.cat_summer, fp.cat_winter, fp.cat_golf
              FROM cohort c JOIN first_p fp USING(USER_ID)
              WHERE ({item_where}) AND ({season_where})
            ),
            n_cohort AS (SELECT COUNT(*) AS n FROM cohort),
            prod_scores AS (
              SELECT
                product_id,
                MIN(brand) AS brand,
                MIN(name) AS name,
                AVG(price) AS price,
                COUNT(DISTINCT USER_ID) AS n_bought,
                -- Recency-weighted: sum of exp(-days_ago / halflife) (recent sales weighted)
                SUM(EXP(-EXTRACT('epoch' FROM (TIMESTAMP '{PULL_DATE}' - purch_ts)) / (86400.0 * {args.recency_halflife}))) AS recency_score,
                -- is_new: first sale within the window happened in the last N days (new arrival detection)
                CASE WHEN MIN(purch_ts) >= TIMESTAMP '{NEW_CUTOFF}' THEN 1 ELSE 0 END AS is_new,
                -- is_golf: brand in GOLF_BRANDS OR product name matches cat_golf
                CASE WHEN MIN(brand) IN ({GOLF_BRANDS_SQL}) OR MAX(cat_golf) > 0 THEN 1 ELSE 0 END AS is_golf,
                -- base_score: recency-weighted Bayesian smoothed
                (SUM(EXP(-EXTRACT('epoch' FROM (TIMESTAMP '{PULL_DATE}' - purch_ts)) / (86400.0 * {args.recency_halflife}))) + {args.alpha}) / ((SELECT n FROM n_cohort) + {args.beta}) AS base_score
              FROM cohort_products
              GROUP BY product_id
              HAVING COUNT(DISTINCT USER_ID) >= 1
            )
            SELECT product_id, brand, name, price, n_bought, recency_score, is_new, is_golf, base_score,
              (SELECT n FROM n_cohort) AS n_cohort
            FROM prod_scores
            ORDER BY base_score DESC LIMIT 500
            """
            try:
                r = c.execute(query).fetchdf()
                cohort_size = int(r.iloc[0]['n_cohort']) if len(r) > 0 else 0

                if len(r) == 0:
                    lookup[key] = {'cohort_size': cohort_size, 'style': style, 'price_bin': price_bin, 'item': item, 'top12': []}
                    continue

                # ------ Apply category boost/penalty + new boost, then recompute bayes_score ------
                golf_mult_val = args.category_boost if style == 'golf' else args.category_penalty
                r['golf_mult'] = r['is_golf'].map(lambda g: golf_mult_val if int(g) == 1 else 1.0)
                r['new_mult']  = r['is_new'].map(lambda n: args.new_boost if int(n) == 1 else 1.0)
                r['bayes_score'] = r['base_score'] * r['golf_mult'] * r['new_mult']

                # ------ Exploitation (top by Bayes score) ------
                exploit_pool = r.copy().sort_values('bayes_score', ascending=False)
                # ------ Exploration (Thompson sampling from the remainder) ------
                rng = np.random.default_rng(int(pd.to_datetime(PULL_DATE).strftime('%Y%m%d')))
                r_full = r.copy()
                r_full['ts_score'] = r_full.apply(
                    lambda row: rng.beta(int(row['n_bought']) + 1, max(cohort_size - int(row['n_bought']), 1) + 1),
                    axis=1
                )
                # is_new + is_golf boosts are also applied on the explore side
                r_full['ts_score'] = r_full['ts_score'] * r_full['is_golf'].map(lambda g: golf_mult_val if int(g) == 1 else 1.0) * r_full['is_new'].map(lambda n: args.new_boost if int(n) == 1 else 1.0)

                # Diversity re-rank
                selected = []
                brand_count = {}
                EXPLOIT_N, EXPLORE_N, TOTAL_N, BRAND_MAX = 70, 50, 120, 15

                def _reason(row, exp_type):
                    tags = []
                    if int(row['is_new']) == 1:
                        tags.append('new')
                    if int(row['is_golf']) == 1 and style == 'golf':
                        tags.append('golf')
                    tag_str = f" ({', '.join(tags)})" if tags else ''
                    if exp_type == 'exploit':
                        pct = row['n_bought']/max(cohort_size,1)*100
                        return f"{int(row['n_bought'])} of {cohort_size:,} ({pct:.1f}%) first-buyers{tag_str}"
                    return f"Real-time learning{tag_str}"

                # Phase A - exploitation
                for _, row in exploit_pool.iterrows():
                    b = row['brand']
                    if brand_count.get(b, 0) >= BRAND_MAX: continue
                    selected.append({
                        'product_id': row['product_id'], 'brand': row['brand'], 'name': row['name'],
                        'price': int(row['price']), 'n_bought': int(row['n_bought']),
                        'recency_score': round(float(row['recency_score']), 4),
                        'is_new': int(row['is_new']), 'is_golf': int(row['is_golf']),
                        'bayes_score': round(float(row['bayes_score']), 6),
                        'base_score': round(float(row['base_score']), 6),
                        'exp_type': 'exploit',
                        'reason': _reason(row, 'exploit'),
                    })
                    brand_count[b] = brand_count.get(b, 0) + 1
                    if len(selected) >= EXPLOIT_N: break

                # Phase B - exploration
                already_ids = {s['product_id'] for s in selected}
                explore_pool = r_full[~r_full['product_id'].isin(already_ids)].sort_values('ts_score', ascending=False)
                for _, row in explore_pool.iterrows():
                    b = row['brand']
                    if brand_count.get(b, 0) >= BRAND_MAX: continue
                    selected.append({
                        'product_id': row['product_id'], 'brand': row['brand'], 'name': row['name'],
                        'price': int(row['price']), 'n_bought': int(row['n_bought']),
                        'recency_score': round(float(row['recency_score']), 4),
                        'is_new': int(row['is_new']), 'is_golf': int(row['is_golf']),
                        'bayes_score': round(float(row['bayes_score']), 6),
                        'base_score': round(float(row['base_score']), 6),
                        'ts_score': round(float(row['ts_score']), 6),
                        'exp_type': 'trending' if int(row['is_new']) == 1 else 'explore',
                        'reason': _reason(row, 'explore'),
                    })
                    brand_count[b] = brand_count.get(b, 0) + 1
                    if len(selected) >= TOTAL_N: break

                lookup[key] = {
                    'cohort_size': cohort_size,
                    'style': style, 'price_bin': price_bin, 'item': item,
                    'top12': selected,  # field name kept for backward compatibility (actually top-30)
                    'n_exploit': sum(1 for s in selected if s['exp_type']=='exploit'),
                    'n_explore': sum(1 for s in selected if s['exp_type'] in ('explore', 'trending')),
                    'n_trending': sum(1 for s in selected if s['exp_type']=='trending'),
                    'n_golf': sum(1 for s in selected if s.get('is_golf') == 1),
                    'n_new': sum(1 for s in selected if s.get('is_new') == 1),
                }
            except Exception as e:
                lookup[key] = {'error': str(e)}

# =================================================================
# 4. Save lookup JSON
# =================================================================
print(f"\n[STEP 4] Saving lookup")

lookup_meta = {
    'pull_date': str(PULL_DATE.date()),
    'window_start': str(WINDOW_START.date()),
    'window_weeks': args.window_weeks,
    'season': SEASON,
    'season_penalty': SEASON_PENALTY,
    'total_cohorts': len(lookup),
    'total_first_purchase_users': n_fp,
    'total_purch_events': n_purch,
    'alpha': args.alpha, 'beta': args.beta,
    'recency_halflife_days': args.recency_halflife,
    'new_cutoff_days': args.new_cutoff_days,
    'category_boost': args.category_boost,
    'category_penalty': args.category_penalty,
    'new_boost': args.new_boost,
    'n_golf_brands_detected': len(GOLF_BRANDS),
    'built_at': str(pd.Timestamp.now()),
}
out = {'meta': lookup_meta, 'cohorts': lookup}

lookup_fname = OUT_DIR / f"reco_lookup_{PULL_DATE.date()}.json"
tmp_fname = lookup_fname.with_suffix('.tmp')
with open(tmp_fname, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
tmp_fname.replace(lookup_fname)
print(f"  - Saved: {lookup_fname}")

# Latest symlink
latest_fname = OUT_DIR / "reco_lookup_latest.json"
if latest_fname.exists() or latest_fname.is_symlink():
    latest_fname.unlink()
latest_fname.symlink_to(lookup_fname.name)
print(f"  - Symlink: {latest_fname} -> {lookup_fname.name}")

# =================================================================
# 5. Delta report vs. previous week
# =================================================================
print(f"\n[STEP 5] Delta report (vs. previous week)")

# Compare against the previous-week lookup if it exists
last_date = PULL_DATE - pd.Timedelta(days=7)
last_fname = OUT_DIR / f"reco_lookup_{last_date.date()}.json"
if last_fname.exists():
    with open(last_fname) as f:
        last = json.load(f)

    delta_report = []
    for key in lookup.keys():
        if key not in last['cohorts'] or 'top12' not in last['cohorts'][key]:
            continue
        this_ids = [p['product_id'] for p in lookup[key].get('top12', [])]
        last_ids = [p['product_id'] for p in last['cohorts'][key].get('top12', [])]
        new_entries = set(this_ids) - set(last_ids)
        dropped = set(last_ids) - set(this_ids)
        if new_entries or dropped:
            delta_report.append({
                'cohort': key,
                'new': list(new_entries),
                'dropped': list(dropped),
                'this_cohort_size': lookup[key].get('cohort_size', 0),
                'last_cohort_size': last['cohorts'][key].get('cohort_size', 0),
            })

    delta_fname = OUT_DIR / f"delta_{PULL_DATE.date()}_vs_{last_date.date()}.json"
    with open(delta_fname, 'w') as f:
        json.dump(delta_report, f, ensure_ascii=False, indent=2)
    print(f"  - Delta report saved: {delta_fname} ({len(delta_report)} cohorts changed)")
else:
    print(f"  - Previous-week file ({last_fname}) not found, delta will start next week")

# =================================================================
# 6. Summary
# =================================================================
print(f"\n[DONE]")
print(f"  - Cohorts built: {len(lookup)}")
print(f"  - Non-empty cohorts: {sum(1 for v in lookup.values() if v.get('top12'))}")
print(f"  - Total first-purchase users used: {n_fp:,}")
print(f"  - Window: {args.window_weeks} weeks")
print(f"  - Lookup file: {lookup_fname}")
