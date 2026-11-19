"""Шаг 1. xlsx → raw.* без изменений содержимого (только имена колонок)."""
import re
from pathlib import Path

import pandas as pd

from db import copy_df

SHEET_PRICES = "ALL_PRICES_REDUCED"
SHEET_CASES = "Cases_summary"

# имя колонки в файле (после нормализации) → колонка raw.all_prices_reduced
COLUMNS = {
    "id": "id", "sku": "sku", "product name": "product_name", "volume": "volume", "unit": "unit",
    "validity days": "validity_days", "currency": "currency", "coverage": "coverage",
    "region name": "region_name", "top rest": "top_rest", "base cost, usd": "base_cost_usd",
    "gb_bucket": "gb_bucket", "market": "market", "subregion": "subregion",
    "base cost, eur": "base_cost_eur", "base cost_non-expiring": "base_cost_non_expiring",
    "recommended price, euro": "recommended_price_eur", "comp1": "comp1", "comp2": "comp2",
    "comp3": "comp3", "_comp1": "comp1_alt", "_comp2": "comp2_alt", "_comp3": "comp3_alt",
    "qty": "qty", "recommended_price 1": "recommended_price_1",
    "recommended_price 2": "recommended_price_2", "recommended_price 3": "recommended_price_3",
    "best_comp (clean)": "best_comp_clean", "median_comp (clean)": "median_comp_clean",
}
REQUIRED = ["region_name", "subregion", "market", "volume", "validity_days", "gb_bucket",
            "base_cost_eur", "qty", "recommended_price_eur"]
INT_COLS = ["id", "validity_days", "qty"]


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name)).strip().lower()


def read_prices(xlsx: Path) -> pd.DataFrame:
    df = pd.read_excel(xlsx, sheet_name=SHEET_PRICES)
    df = df.rename(columns={c: COLUMNS[norm(c)] for c in df.columns if norm(c) in COLUMNS})
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise SystemExit(f"В листе {SHEET_PRICES} нет колонок: {', '.join(missing)}")
    df = df[[c for c in COLUMNS.values() if c in df.columns]]
    df = df[df["region_name"].notna()].reset_index(drop=True)
    for c in INT_COLS:
        if c in df.columns:
            df[c] = df[c].astype("Int64")
    if "base_cost_non_expiring" in df.columns:
        df["base_cost_non_expiring"] = df["base_cost_non_expiring"].astype("string")
    df.insert(0, "row_num", range(1, len(df) + 1))
    return df


def read_cases(xlsx: Path) -> pd.DataFrame:
    try:
        df = pd.read_excel(xlsx, sheet_name=SHEET_CASES)
    except ValueError:
        return pd.DataFrame(columns=["metric", "scenario", "value"])
    df = df.rename(columns={df.columns[0]: "metric"})
    return df.melt(id_vars="metric", var_name="scenario", value_name="value")


def load(conn, xlsx: Path) -> int:
    prices, cases = read_prices(xlsx), read_cases(xlsx)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE raw.all_prices_reduced, raw.cases_summary CASCADE")
    copy_df(conn, "raw.all_prices_reduced", prices)
    copy_df(conn, "raw.cases_summary", cases)
    conn.commit()
    return len(prices)
