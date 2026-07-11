#!/usr/bin/env python3
"""Generate a synthetic reco_lookup fixture so the service runs standalone.

The real weekly batch (service_reco_weekly_build.py) requires a data
warehouse (Snowflake/BigQuery) and historical purchase records. That is
usually unavailable in a fresh clone. This script builds a small
synthetic lookup so `uvicorn app:app` boots into a working demo.

Usage:
    python scripts/generate_sample_data.py

Output:
    data/reco_lookup/reco_lookup_latest.json
"""
import json
import random
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "reco_lookup" / "reco_lookup_latest.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

STYLES = ["golf", "sports_casual", "formal", "outdoor"]
PRICES = ["low", "mid", "high"]
ITEMS = ["top", "bottom", "shoes", "outer", "browse"]

# Synthetic brand pool per style — generic names so the fixture is
# portable across projects.
BRAND_POOL = {
    "golf":          ["FairwayCo",  "GreenPath",  "SwingLine",  "IronBloom",  "ParHouse"],
    "sports_casual": ["EverydayCo", "PaceLab",    "DailyForm",  "MoveWell",   "CityRun"],
    "formal":        ["StudioW",    "ClassicMode","OxfordRow",  "HighTailor", "OnLine"],
    "outdoor":       ["Ridgeway",   "SummitCraft","TrailHouse", "RiverKit",   "PineFold"],
}

# Item -> generic product-name template
NAME_TEMPLATES = {
    "top":    ["Cotton Polo",       "Signature Tee",    "Piqué Knit Shirt",  "Mesh Polo",       "Linen Half-Sleeve"],
    "bottom": ["Slim Chino",        "Comfort Pant",     "Straight Trouser",  "Tapered Slack",   "Cargo Short"],
    "shoes":  ["Court Sneaker",     "Classic Loafer",   "Trainer 03",        "Field Boot",      "Runner Lite"],
    "outer":  ["Field Jacket",      "Windbreaker",      "Utility Vest",      "Blazer Two",      "Zip Overshirt"],
    "browse": ["Everyday Tee",      "Signature Polo",   "Field Cap",         "Belt No.3",       "Utility Pouch"],
}

# Price bucket ranges (KRW; adapt for your currency downstream).
PRICE_RANGE = {
    "low":  (12_000, 29_000),
    "mid":  (30_000, 79_000),
    "high": (80_000, 260_000),
}

random.seed(42)


def make_cohort(style, price, item):
    brands = BRAND_POOL[style]
    names = NAME_TEMPLATES[item]
    low, high = PRICE_RANGE[price]
    products = []
    # 30 products per cohort so the default k=60 request stays informative even
    # after the brand-diversity cap trims a few. Adopters can tune this.
    for i in range(30):
        brand = brands[i % len(brands)]
        name = names[i % len(names)]
        pid = f"{style[:2]}{price[0]}{item[0]}-{i:03d}"
        # Some products get a small "sold" signal so the seeded prior
        # is informative (not all uniform).
        n_bought = random.choice([0, 0, 0, 1, 1, 2, 3, 5, 8, 12])
        exp_type = "explore" if i >= 7 else "exploit"
        products.append({
            "product_id": pid,
            "brand": brand,
            "name": f"{name} v{i+1}",
            "price": random.randint(low, high),
            "n_bought": n_bought,
            "exp_type": exp_type,
        })
    return {
        "cohort_size": 100,
        "top12": products,
    }


def build_lookup():
    cohorts = {}
    for s in STYLES:
        for p in PRICES:
            for it in ITEMS:
                key = f"{s}__{p}__{it}"
                cohorts[key] = make_cohort(s, p, it)
    return {
        "meta": {
            "pull_date": str(date.today()),
            "window_weeks": 4,
            "season": "summer",
            "total_cohorts": len(cohorts),
            # total_first_purchase_users is exposed via /api/health only.
            # For a synthetic fixture we expose a plausible number.
            "total_first_purchase_users": 3200,
            "generator": "generate_sample_data.py",
        },
        "cohorts": cohorts,
    }


def main():
    lookup = build_lookup()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False, indent=2)
    n_products = sum(len(c["top12"]) for c in lookup["cohorts"].values())
    print(f"[sample] wrote {OUT}")
    print(f"[sample] {len(lookup['cohorts'])} cohorts, {n_products} products, "
          f"season={lookup['meta']['season']}")


if __name__ == "__main__":
    main()
