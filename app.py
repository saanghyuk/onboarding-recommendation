"""
Onboarding Recommendation Service - FastAPI + online Thompson Sampling bandit

Online reinforcement learning:
- Maintain a Beta(alpha, beta) posterior per (cohort_key, product_id) in SQLite
- Initial prior: seeded from the weekly batch as (n_bought, n_cohort - n_bought)
- Each request: Beta sample -> top-12 selection
- User feedback (click / purchase / skip / dwell):
    click     -> alpha += 1.0
    purchase  -> alpha += 5.0 (strong signal)
    skip      -> beta  += 0.5 (weak negative)
    dwell_2s  -> alpha += 0.2 (weak positive)
- Each weekly batch: new products get seeded priors; existing posteriors are kept.

Endpoints:
  GET  /                          -> HTML
  GET  /api/health                -> server, lookup, and bandit status
  GET  /api/quiz-config           -> quiz options
  GET  /api/recommendations       -> Thompson sampled top-12 (online RL)
  POST /api/feedback              -> click/purchase/skip signal, posterior update
  POST /api/quiz-log              -> user response log
  POST /api/rebuild               -> manual lookup rebuild (secret token)
  GET  /api/bandit-stats          -> learning progress statistics
  GET  /api/stats/quiz-logs       -> today's quiz log summary
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Header, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
DATA.mkdir(exist_ok=True); LOGS.mkdir(exist_ok=True)
LOOKUP_PATH = DATA / "reco_lookup" / "reco_lookup_latest.json"
DB_PATH = DATA / "bandit.db"
(DATA / "reco_lookup").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Onboarding Recommendation API - Online RL", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ============ Lookup cache ============
class LookupCache:
    def __init__(self):
        self._data, self._mtime = None, None

    def get(self):
        try:
            mtime = LOOKUP_PATH.stat().st_mtime
        except FileNotFoundError:
            raise HTTPException(503, f"Lookup not found: {LOOKUP_PATH}")
        if self._data is None or self._mtime != mtime:
            with open(LOOKUP_PATH) as f:
                self._data = json.load(f)
            self._mtime = mtime
            # Seed bandit with any new (cohort, product) pairs
            bandit.seed_from_lookup(self._data)
        return self._data

    def meta(self):
        return self.get()['meta']


# ============ Online Thompson Sampling bandit ============
DB_LOCK = threading.Lock()

class BanditDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with DB_LOCK:
            conn = self._conn()
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS posterior (
              cohort_key TEXT NOT NULL,
              product_id TEXT NOT NULL,
              brand TEXT,
              name TEXT,
              price INTEGER,
              alpha REAL DEFAULT 1.0,
              beta REAL DEFAULT 1.0,
              n_impressions INTEGER DEFAULT 0,
              n_clicks INTEGER DEFAULT 0,
              n_purchases INTEGER DEFAULT 0,
              n_skips INTEGER DEFAULT 0,
              exp_type TEXT DEFAULT 'exploit',
              seeded_from TEXT,
              last_updated TEXT,
              PRIMARY KEY (cohort_key, product_id)
            );
            CREATE INDEX IF NOT EXISTS idx_posterior_cohort ON posterior(cohort_key);
            CREATE TABLE IF NOT EXISTS feedback_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              session_id TEXT,
              cohort_key TEXT,
              product_id TEXT,
              signal TEXT,
              alpha_after REAL,
              beta_after REAL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback_log(ts);
            """)
            conn.close()

    # Weakly-informative prior scale controls the effective learning rate.
    # K=100 -> a click of alpha+=1 shifts the posterior mean ~1% (real learning).
    # K=5000 -> a click shifts only 0.02%, essentially no learning.
    PRIOR_K = 100

    def seed_from_lookup(self, lookup):
        """Seed initial priors from the weekly batch results (K-rescaled weakly-informative).
        The mean (popularity) is preserved but the posterior weight is shrunk by K so
        individual clicks actually move the posterior. Existing posteriors are kept."""
        with DB_LOCK:
            conn = self._conn()
            seeded_from = lookup['meta']['pull_date']
            n_new = 0
            K = self.PRIOR_K
            for cohort_key, cohort in lookup['cohorts'].items():
                if not cohort.get('top12'):
                    continue
                cs = max(cohort.get('cohort_size', 0), 1)
                for p in cohort['top12']:
                    n_bought = p.get('n_bought', 0)
                    # K-rescaled prior: mean preserved, variance up, learning rate up
                    prior_ctr = n_bought / cs
                    prior_alpha = prior_ctr * K + 1.0
                    prior_beta  = (1.0 - prior_ctr) * K + 1.0
                    exp_type = p.get('exp_type', 'exploit')  # keep original weekly batch tag
                    cur = conn.execute("SELECT 1 FROM posterior WHERE cohort_key=? AND product_id=?", (cohort_key, p['product_id']))
                    if cur.fetchone() is None:
                        conn.execute(
                            "INSERT INTO posterior(cohort_key, product_id, brand, name, price, alpha, beta, exp_type, seeded_from, last_updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (cohort_key, p['product_id'], p['brand'], p['name'], p['price'],
                             prior_alpha, prior_beta, exp_type, seeded_from, datetime.now().isoformat())
                        )
                        n_new += 1
            conn.close()
            print(f"[bandit] seeded {n_new} new (cohort, product) with K-scaled prior (K={K})")

    def sample(self, cohort_key: str, k: int = 60, exploration_boost: float = 1.0,
               brand_max: int = 4, season: str = 'summer'):
        """Thompson sample: draw K items from Beta posteriors, with brand diversity and season/style hygiene.
        exp_type is kept from the weekly batch tag (not recomputed - stable and interpretable)."""
        rng = np.random.default_rng()
        with DB_LOCK:
            conn = self._conn()
            rows = conn.execute(
                "SELECT product_id, brand, name, price, alpha, beta, n_impressions, n_clicks, n_purchases, COALESCE(exp_type, 'exploit') FROM posterior WHERE cohort_key=?",
                (cohort_key,)
            ).fetchall()
            conn.close()
        if not rows:
            return []

        # Cohort style parsing: guard against cross-style contamination
        style_cohort = cohort_key.split('__')[0] if '__' in cohort_key else ''
        # Season vocabulary (summer default -> block winter-only items)
        WINTER_TOKENS = ('long coat', 'padding', 'down jacket', 'corduroy', 'velvet', 'fleece-lined', 'FW ', '/FW', 'FW.', 'F/W', 'winter season', 'thermal', 'heat tech')
        SUMMER_TOKENS = ('short sleeve', 'cooling', 'ice', 'cool', 'mesh', 'shorts', 'linen', 'summer', 'water', 'SS ', 'S/S')
        # Category-specific keyword filter: only exclude when NOT in a matching cohort
        GOLF_TOKENS = ('golf', 'PGA', 'PXG')

        scored = []
        for r in rows:
            pid, brand, name, price, a, b, imp, clk, pur, exp_type = r
            name_str = (name or '')
            brand_str = (brand or '')
            combined = f"{brand_str} {name_str}"

            # Season hygiene: exclude winter-only products when season is summer
            if season == 'summer' and any(t in combined for t in WINTER_TOKENS):
                continue
            # Style contamination: exclude specialty items from non-matching cohorts
            if style_cohort != 'golf' and any(t in combined for t in GOLF_TOKENS):
                continue

            # Thompson sample from Beta(alpha, beta); exploration is naturally produced
            boost = 1.3 if exp_type == 'explore' else 1.0
            score = rng.beta(a * boost, b)

            # Reason string: attribute-based to keep user trust (no "0 clicks" disclosure)
            is_summer_hit = season == 'summer' and any(t in combined for t in SUMMER_TOKENS)
            if is_summer_hit:
                reason = "Summer season pick, matched to your taste"
            elif exp_type == 'explore':
                reason = "Trending new arrival, real-time"
            elif imp >= 3 and clk >= 1:
                # Only used when real learning data exists (not a disclosure)
                reason = "Popular pick, learning in real time"
            else:
                reason = "Curated for your taste, learning in real time"

            scored.append({
                'product_id': pid, 'brand': brand, 'name': name, 'price': int(price or 0),
                'ts_score': float(score), 'alpha': float(a), 'beta': float(b),
                'n_impressions': int(imp), 'n_clicks': int(clk), 'n_purchases': int(pur),
                'exp_type': exp_type, 'reason': reason,
            })

        scored.sort(key=lambda x: x['ts_score'], reverse=True)
        # Brand diversity: max brand_max per brand (default 4 - keeps variety while expanding pool)
        selected, brand_count = [], {}
        for p in scored:
            if brand_count.get(p['brand'], 0) >= brand_max:
                continue
            selected.append(p); brand_count[p['brand']] = brand_count.get(p['brand'], 0) + 1
            if len(selected) >= k: break
        return selected

    def record_impressions(self, cohort_key: str, product_ids: list):
        with DB_LOCK:
            conn = self._conn()
            for pid in product_ids:
                conn.execute("UPDATE posterior SET n_impressions = n_impressions + 1 WHERE cohort_key=? AND product_id=?", (cohort_key, pid))
            conn.close()

    def feedback(self, cohort_key: str, product_id: str, signal: str, session_id: Optional[str] = None):
        """
        signal: click / purchase / skip / dwell_2s
        Update alpha or beta and log the feedback.
        """
        SIGNAL_UPDATE = {
            'click':     ('alpha', 1.0, 'n_clicks'),
            'purchase':  ('alpha', 5.0, 'n_purchases'),
            'skip':      ('beta',  0.5, 'n_skips'),
            'dwell_2s':  ('alpha', 0.2, None),
        }
        if signal not in SIGNAL_UPDATE:
            raise ValueError(f"unknown signal: {signal}")
        param, delta, counter = SIGNAL_UPDATE[signal]

        with DB_LOCK:
            conn = self._conn()
            cur = conn.execute("SELECT alpha, beta FROM posterior WHERE cohort_key=? AND product_id=?", (cohort_key, product_id))
            row = cur.fetchone()
            if row is None:
                conn.close()
                return None
            alpha, beta = row
            if param == 'alpha':
                alpha += delta
            else:
                beta += delta
            if counter:
                conn.execute(
                    f"UPDATE posterior SET alpha=?, beta=?, {counter}={counter}+1, last_updated=? WHERE cohort_key=? AND product_id=?",
                    (alpha, beta, datetime.now().isoformat(), cohort_key, product_id)
                )
            else:
                conn.execute(
                    "UPDATE posterior SET alpha=?, beta=?, last_updated=? WHERE cohort_key=? AND product_id=?",
                    (alpha, beta, datetime.now().isoformat(), cohort_key, product_id)
                )
            conn.execute(
                "INSERT INTO feedback_log(ts, session_id, cohort_key, product_id, signal, alpha_after, beta_after) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(), session_id, cohort_key, product_id, signal, alpha, beta)
            )
            conn.close()
        return {'alpha_after': alpha, 'beta_after': beta}

    def stats(self):
        with DB_LOCK:
            conn = self._conn()
            n_rows = conn.execute("SELECT COUNT(*) FROM posterior").fetchone()[0]
            n_cohorts = conn.execute("SELECT COUNT(DISTINCT cohort_key) FROM posterior").fetchone()[0]
            n_feedback = conn.execute("SELECT COUNT(*) FROM feedback_log").fetchone()[0]
            by_signal = dict(conn.execute("SELECT signal, COUNT(*) FROM feedback_log GROUP BY signal").fetchall())
            top_learned = conn.execute("""
              SELECT cohort_key, brand, name, alpha, beta, n_impressions, n_clicks, n_purchases
              FROM posterior WHERE n_impressions >= 3
              ORDER BY (alpha - 1) / (alpha + beta - 2 + 0.001) DESC LIMIT 10
            """).fetchall()
            recent_feedback = conn.execute("SELECT ts, cohort_key, brand FROM feedback_log fl LEFT JOIN posterior p USING(cohort_key, product_id) ORDER BY id DESC LIMIT 10").fetchall()
            conn.close()
        return {
            'total_posteriors': n_rows,
            'total_cohorts': n_cohorts,
            'total_feedback': n_feedback,
            'feedback_by_signal': by_signal,
            'top_learned_products': [
                {'cohort': r[0], 'brand': r[1], 'name': r[2], 'alpha': r[3], 'beta': r[4],
                 'imp': r[5], 'clk': r[6], 'purch': r[7], 'ctr_estimate': (r[3]-1)/(r[3]+r[4]-2+0.001)}
                for r in top_learned
            ],
        }


bandit = BanditDB(DB_PATH)
cache = LookupCache()


# ============ Endpoints ============
@app.get("/api/health")
def health():
    try:
        meta = cache.meta()
        stats = bandit.stats()
        return {
            'status': 'ok', 'server_time': datetime.now().isoformat(),
            'lookup_pull_date': meta['pull_date'],
            'lookup_window_weeks': meta['window_weeks'],
            'lookup_season': meta['season'],
            'total_cohorts': meta['total_cohorts'],
            'total_first_purchase_users': meta['total_first_purchase_users'],
            'bandit': {
                'total_posteriors': stats['total_posteriors'],
                'total_feedback': stats['total_feedback'],
                'feedback_by_signal': stats['feedback_by_signal'],
            },
        }
    except HTTPException:
        return {'status': 'degraded', 'reason': 'lookup missing'}


@app.get("/api/quiz-config")
def quiz_config():
    return {
        'styles': [
            {'value': 'golf',           'label': 'Golf',            'emoji': 'flag', 'sub': 'Round'},
            {'value': 'sports_casual',  'label': 'Sports / Casual', 'emoji': 'run',  'sub': 'Everyday'},
            {'value': 'formal',         'label': 'Formal',          'emoji': 'tie',  'sub': 'Business'},
            {'value': 'outdoor',        'label': 'Outdoor',         'emoji': 'tent', 'sub': 'Hiking'},
        ],
        'prices': [
            {'value': 'low',  'label': 'Budget',    'emoji': '$',   'sub': 'under 30K'},
            {'value': 'mid',  'label': 'Mid',       'emoji': '$$',  'sub': '30-80K'},
            {'value': 'high', 'label': 'Premium',   'emoji': '$$$', 'sub': '80K+'},
        ],
        'items': [
            {'value': 'top',    'label': 'Top',       'emoji': 'shirt'},
            {'value': 'bottom', 'label': 'Bottom',    'emoji': 'pants'},
            {'value': 'shoes',  'label': 'Shoes',     'emoji': 'shoe'},
            {'value': 'outer',  'label': 'Outerwear', 'emoji': 'coat'},
            {'value': 'browse', 'label': 'Just browsing', 'emoji': 'cart'},
        ],
    }


STYLE_ENUM = {'golf', 'sports_casual', 'formal', 'outdoor'}
PRICE_ENUM = {'low', 'mid', 'high'}
ITEM_ENUM = {'top', 'bottom', 'shoes', 'outer', 'browse'}


def _normalize_enum(value: str, enum: set, default: str) -> str:
    """Trim + lowercase + enum guard. If the user sends garbage, fall back to a sensible default."""
    if not value:
        return default
    v = value.strip().lower()
    return v if v in enum else default


@app.get("/api/recommendations")
def recommendations(
    style: str = Query(...), price: str = Query(...), item: str = Query(...),
    os_name: Optional[str] = None, hour: Optional[int] = None,
    session_id: Optional[str] = None,
    k: int = Query(60, ge=12, le=200, description="Number of recommended products (default 60)"),
):
    # Ensure lookup loaded (this also seeds bandit for new cohorts)
    lookup = cache.get()

    # Enum guard: invalids are mapped to sensible defaults (explicit normalization, not silent fallback)
    style = _normalize_enum(style, STYLE_ENUM, 'sports_casual')
    price = _normalize_enum(price, PRICE_ENUM, 'mid')
    item = _normalize_enum(item, ITEM_ENUM, 'browse')

    season = lookup.get('meta', {}).get('season', 'summer')
    key = f"{style}__{price}__{item}"

    # Try Thompson sample from bandit posterior (season/style filter applied inside)
    products = bandit.sample(key, k=k, season=season)

    matched_key = key
    is_fallback = False
    if not products:
        # Fallback: keep price tier (avoid bouncing low/high users to mid)
        for fb_key in [f"{style}__{price}__browse", f"sports_casual__{price}__browse", "sports_casual__mid__browse"]:
            products = bandit.sample(fb_key, k=k, season=season)
            if products:
                matched_key = fb_key
                is_fallback = True
                break

    if not products:
        raise HTTPException(404, f"No cohort match: {key}")

    # OS covariate soft boost
    if os_name == 'iOS':
        exploit = [p for p in products if p['exp_type'] == 'exploit']
        explore = [p for p in products if p['exp_type'] == 'explore']
        exploit_sorted = sorted(exploit, key=lambda p: -p['price'])
        products = exploit[:3] + exploit_sorted[3:] + explore

    # Record impressions (beta updates come from skip events)
    bandit.record_impressions(matched_key, [p['product_id'] for p in products])

    n_exploit = sum(1 for p in products if p['exp_type'] == 'exploit')
    n_explore = len(products) - n_exploit

    # Public meta: raw metrics like total_first_purchase_users are hidden from clients
    public_meta = {
        'season': season,
        'pull_date': lookup['meta'].get('pull_date'),
    }
    return {
        'meta': public_meta,
        'query': {'style': style, 'price': price, 'item': item, 'os_name': os_name, 'hour': hour},
        'matched_key': matched_key,
        'is_fallback': is_fallback,
        'cohort_size': lookup['cohorts'].get(matched_key, {}).get('cohort_size', 0),
        'n_exploit': n_exploit, 'n_explore': n_explore,
        'n_total': len(products),
        'products': products,
        'top12': products,  # backward-compat alias so existing UIs keep working
    }


class Feedback(BaseModel):
    session_id: Optional[str] = None
    cohort_key: str
    product_id: str
    signal: str  # click / purchase / skip / dwell_2s


@app.post("/api/feedback")
def feedback(fb: Feedback):
    """User action -> posterior update (real-time reinforcement learning)."""
    try:
        result = bandit.feedback(fb.cohort_key, fb.product_id, fb.signal, fb.session_id)
        if result is None:
            raise HTTPException(404, "cohort/product not found in posterior")
        return {'ok': True, **result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/bandit-stats")
def bandit_stats():
    return bandit.stats()


class QuizLog(BaseModel):
    session_id: str
    device: dict
    answers: dict
    step: str
    ts_client: Optional[str] = None


@app.post("/api/quiz-log")
def quiz_log(log: QuizLog):
    log_path = LOGS / f"quiz_log_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    rec = log.model_dump(); rec['ts_server'] = datetime.now().isoformat()
    with open(log_path, 'a') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    return {'ok': True, 'logged_step': log.step}


@app.post("/api/rebuild")
def rebuild(x_admin_token: Optional[str] = Header(None)):
    if x_admin_token != os.environ.get("ADMIN_TOKEN", "change-me-in-production"):
        raise HTTPException(401, "invalid token")
    import sys
    script = ROOT / "scripts" / "service_reco_weekly_build.py"
    result = subprocess.run(
        [sys.executable, str(script), "--window-weeks=52"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800
    )
    cache._data = None
    _ = cache.get()  # force reload + seed
    return {'ok': result.returncode == 0, 'stdout_tail': result.stdout[-2000:], 'stderr_tail': result.stderr[-500:] if result.stderr else ''}


@app.get("/api/stats/quiz-logs")
def quiz_stats():
    log_path = LOGS / f"quiz_log_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    if not log_path.exists():
        return {'total': 0, 'by_step': {}, 'unique_sessions': 0}
    lines = log_path.read_text().strip().split('\n') if log_path.stat().st_size > 0 else []
    records = [json.loads(l) for l in lines if l]
    by_step, sessions = {}, set()
    for r in records:
        step = r.get('step', 'unknown')
        by_step[step] = by_step.get(step, 0) + 1
        sessions.add(r.get('session_id'))
    return {'total': len(records), 'by_step': by_step, 'unique_sessions': len(sessions)}


# ============ HTML routes (optional — demo UIs are not shipped in this repo) ============
# If you drop your own front-end at ./static/{index,simple,voice,swipe,persona}.html
# these routes will serve them. Otherwise `/` returns a small JSON landing and the
# demo paths return 404 with a helpful message. The API endpoints above always work.

def _api_landing():
    return {
        "service": "Onboarding Recommendation Engine",
        "status": "ok",
        "hint": "This repo ships the API only. Bring your own front-end.",
        "docs": {
            "api_spec": "See docs/API_SPEC.md",
            "frontend": "See docs/FRONTEND_INTEGRATION.md",
        },
        "endpoints": [
            "GET /api/health",
            "GET /api/quiz-config",
            "GET /api/recommendations?style=&price=&item=&k=60",
            "POST /api/feedback",
            "POST /api/nlu",
            "GET /api/bandit-stats",
        ],
    }


def _serve_static_html(filename: str):
    path = STATIC / filename
    if not path.exists():
        raise HTTPException(404, f"{filename} not in ./static/. This repo ships API-only — mount your own front-end at ./static/ or call the API directly. See docs/FRONTEND_INTEGRATION.md.")
    return HTMLResponse(path.read_text(encoding='utf-8'))


@app.get("/")
def index():
    path = STATIC / "index.html"
    if path.exists():
        return HTMLResponse(path.read_text(encoding='utf-8'))
    return JSONResponse(_api_landing())


@app.get("/simple", response_class=HTMLResponse)
def simple():
    return _serve_static_html("simple.html")


@app.get("/voice", response_class=HTMLResponse)
def voice():
    return _serve_static_html("voice.html")


@app.get("/swipe", response_class=HTMLResponse)
def swipe():
    """swipe.html: inject window.RECO_LOOKUP server-side so the client can render the swipe deck without an extra fetch. Rename the global if you rebrand the client."""
    path = STATIC / "swipe.html"
    if not path.exists():
        raise HTTPException(404, "swipe.html not in ./static/. See docs/FRONTEND_INTEGRATION.md.")
    html = path.read_text(encoding='utf-8')
    try:
        lookup = cache.get()
        # Slim payload: keep only top items per cohort (swipe UI uses cohort x top-few)
        slim = {'meta': lookup.get('meta', {}), 'cohorts': {}}
        for k, c in lookup.get('cohorts', {}).items():
            top12 = c.get('top12') or []
            if not top12:
                continue
            slim['cohorts'][k] = {
                'cohort_size': c.get('cohort_size', 0),
                'top12': top12[:4],
            }
        payload = json.dumps(slim, ensure_ascii=False)
        inject = f'<script>window.RECO_LOOKUP = {payload};</script>'
        html = html.replace('</head>', inject + '</head>', 1)
    except Exception:
        # If lookup is missing, still render the page - client handles the error panel
        pass
    return html


@app.get("/persona", response_class=HTMLResponse)
def persona():
    return _serve_static_html("persona.html")


# ============ NLU - Claude Haiku (used by the voice page) ============
NLU_SYSTEM = """You are the onboarding curator for a menswear cold-start recommendation service. Parse user utterances along three axes.

Axis definitions:
- style: "golf" | "sports_casual" | "formal" | "outdoor"
- price: "low" (under 30K) | "mid" (30-80K) | "high" (80K+)
- item: "top" | "bottom" | "shoes" | "outer" | "browse"

Mapping:
- style: golf/round/course -> golf | suit/shirt/formal/business -> formal | hiking/outdoor/trekking/camping -> outdoor | otherwise -> sports_casual
- price: budget/cheap/under 30K -> low | premium/high-end/over 80K/quality -> high | otherwise -> mid
- item: tee/shirt/knit/sweatshirt/polo -> top | pants/jeans/denim/slacks -> bottom | sneakers/shoes/loafers/boots -> shoes | jacket/coat/padding/windbreaker -> outer | just/anything/browsing -> browse

Return JSON only. No explanation:
{"style":"...","price":"...","item":"...","season_hint":"summer|winter|null","brand_hint":"brand|null"}"""

VALID_STYLES = {'golf','sports_casual','formal','outdoor'}
VALID_PRICES = {'low','mid','high'}
VALID_ITEMS = {'top','bottom','shoes','outer','browse'}


class NLURequest(BaseModel):
    text: str


def _nlu_rule_based(text: str) -> dict:
    """Keyword-based fallback when the LLM is unavailable.

    Ships with English + Korean vocabulary out of the box. Extend the
    alternations below with your own domain terms (any language) — the parser
    is a plain regex, so adding tokens is a one-line change.

    NOTE: order matters - stronger signals (golf / outdoor) first, ambiguous
    ones (shirt / 셔츠) last, because 'shirt' overlaps with 't-shirt'.
    """
    t = text.lower()  # Hangul is unaffected by lower() so this only normalises ASCII

    # style
    if re.search(r'golf|round|course|links|golf wear|골프|라운드|필드|골프복|골프장', t):
        style = 'golf'
    elif re.search(r'hiking|outdoor|trekking|camping|hike|mountain|backpacking|climbing|등산|아웃도어|트레킹|캠핑|하이킹|야외|산행|백패킹', t):
        style = 'outdoor'
    elif re.search(r'suit|formal|business|office|dress shirt|blazer|tie|meeting|정장|수트|포멀|비즈니스|출근|오피스|사무실|격식|드레스셔츠|드레스 셔츠|와이셔츠', t):
        style = 'formal'
    else:
        style = 'sports_casual'

    # price
    if re.search(r'budget|cheap|affordable|low[- ]?cost|under 30|inexpensive|가성비|저렴|싸게|3만원|이하|미만|저가|저렴한|싼|부담 없이', t):
        price = 'low'
    elif re.search(r'premium|luxury|high[- ]?end|over 80|top quality|expensive|designer|고급|프리미엄|8만|이상|비싸도|고가|명품|하이엔드|명품급', t):
        price = 'high'
    else:
        price = 'mid'

    # item: shoes first (a trekking shoe is still a shoe once outdoor style is decided)
    if re.search(r'sneaker|shoe|shoes|loafer|boot|runner|golf shoe|trekking shoe|hiking shoe|운동화|구두|신발|로퍼|부츠|스니커즈|골프화|런닝화|러닝화|워커|트레킹화|등산화|워킹화', t):
        item = 'shoes'
    elif re.search(r'jacket|coat|padding|windbreaker|blazer|parka|outer|outerwear|자켓|재킷|코트|패딩|바람막이|점퍼|아우터|블레이저|파카|외투', t):
        item = 'outer'
    elif re.search(r'pants|trouser|jeans|denim|slacks|shorts|chino|jogger|cargo|bottom|팬츠|바지|청바지|데님|슬랙스|반바지|숏팬츠|치노|조거|하의|쇼츠', t):
        item = 'bottom'
    elif re.search(r't[- ]?shirt|short sleeve|long sleeve|knit|sweatshirt|polo|hoodie|sweater|blouse|inner|top|shirt|티셔츠|반팔|긴팔|니트|맨투맨|폴로|후드|스웨터|블라우스|이너|카라티|카라 티|상의|폴로 셔츠|티|셔츠', t):
        item = 'top'
    else:
        item = 'browse'

    return {'style': style, 'price': price, 'item': item, 'season_hint': None, 'brand_hint': None, 'engine': 'rule-based'}


@app.post("/api/nlu")
def nlu(req: NLURequest):
    text = (req.text or '').strip()
    if not text:
        raise HTTPException(400, "text required")
    if len(text) > 500:
        raise HTTPException(400, "text too long")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    # Try LLM first; on any failure (missing key, network, parse error) fall back to rule-based
    if api_key:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            msg = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=200,
                system=NLU_SYSTEM,
                messages=[{"role": "user", "content": f'Utterance: "{text}"\n\nReturn JSON only:'}]
            )
            raw = (msg.content[0].text if msg.content else '').strip()
            m = re.search(r'\{[\s\S]*?\}', raw)
            parsed = {}
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except Exception:
                    parsed = {}
            style = parsed.get('style') if parsed.get('style') in VALID_STYLES else None
            price = parsed.get('price') if parsed.get('price') in VALID_PRICES else None
            item  = parsed.get('item')  if parsed.get('item')  in VALID_ITEMS  else None
            season = parsed.get('season_hint') if parsed.get('season_hint') in ('summer','winter') else None
            brand  = parsed.get('brand_hint') if isinstance(parsed.get('brand_hint'), str) and len(parsed.get('brand_hint','')) < 30 else None
            # If the LLM missed any axis, patch just that axis with the rule-based value (avoid fully-clamped defaults)
            if style and price and item:
                return {'ok': True, 'style': style, 'price': price, 'item': item,
                        'season_hint': season, 'brand_hint': brand,
                        'raw_text': text, 'engine': 'claude-haiku-4-5'}
            rb = _nlu_rule_based(text)
            return {'ok': True, 'style': style or rb['style'], 'price': price or rb['price'], 'item': item or rb['item'],
                    'season_hint': season, 'brand_hint': brand,
                    'raw_text': text, 'engine': 'hybrid-rule-fallback'}
        except Exception as e:
            print(f"[nlu] LLM error, rule-based fallback: {e}")

    # Rule-based fallback (no API key or LLM failed)
    rb = _nlu_rule_based(text)
    return {'ok': True, **rb, 'raw_text': text}


# Mount static assets only if the directory exists (this repo ships API-only).
if STATIC.exists() and STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# On startup: eager seed
@app.on_event("startup")
def startup():
    try:
        _ = cache.get()
        stats = bandit.stats()
        print(f"[startup] bandit: {stats['total_posteriors']} posteriors, {stats['total_feedback']} feedback events")
    except Exception as e:
        print(f"[startup] warning: {e}")
