"""Расчёт базовой цены (Baseline) и запись в pricing.prices как сценарий `baseline`.

Логика перенесена из ноутбука esim_pricing_FINAL.ipynb (раздел 2.2 Baseline):
  1. целевая цена = себестоимость / (1 − целевая маржа): 30% для 1–10 ГБ, 20% для 20+ ГБ и безлимита;
  2. округление до ближайшей цены формата X,Y9, но не ниже себестоимости;
  3. правила сетки: лесенка объёмов (больший пакет дороже меньшего, цена за ГБ не растёт),
     бессрочный пакет выводится из 30-дневного той же страны и объёма (премия по объёму,
     не дороже +20 €, не дешевле паритета себестоимости).

Скрипт работает только с базой (читает raw.all_prices_reduced), поэтому им можно
пересчитать Baseline после обновления себестоимости или параметров, не трогая остальное.

Запуск:  python loader/baseline.py
         python loader/baseline.py --margin-small 0.32 --margin-big 0.22   # другие цели маржи
         python loader/baseline.py --dry-run                               # только посчитать и сверить
"""
import argparse

import numpy as np
import pandas as pd

from db import connect

# --- параметры из раздела Settings ноутбука ---------------------------------------
TARGET_MARGIN = {"1-10GB": 0.30, "20+GB": 0.20}      # по продажам; в 20+GB входит и безлимит
PRICE_STEP = 0.10                                     # цены вида X,Y9: 0.69, 4.49, 12.99
NOEXP_PREMIUM = {1: 0.25, 3: 0.25, 5: 0.25, 10: 0.30, 20: 0.30, 50: 0.40, 100: 0.40, 200: 0.40}
NOEXP_MAX_EUR = 20.0                                  # бессрочный не дороже срочного более чем на 20 €
PAIR_VALIDITY = 30                                    # срочный пакет, от которого считается бессрочный
SCENARIO_ID = "baseline"


def to_x9(p, how="nearest"):
    k = (np.asarray(p, dtype=float) + 0.01) / PRICE_STEP
    k = np.floor(k + 1e-9) if how == "down" else np.ceil(k - 1e-9) if how == "up" else np.round(k)
    return np.round(k * PRICE_STEP - 0.01, 2)


class Grid:
    """Тарифная сетка и правила из ноутбука: лесенка объёмов и бессрочные пакеты."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.ladder_mask = (df["bucket"] != "Unlimited") & df["vol"].notna()
        exp30 = df[(df["plan"] == "expiring") & (df["validity"] == PAIR_VALIDITY)].reset_index()[["index", "region", "vol"]]
        noexp = df[df["plan"] == "non-expiring"].reset_index()[["index", "region", "vol"]]
        pairs = exp30.merge(noexp, on=["region", "vol"], suffixes=("_exp", "_noexp"))
        if not pairs["index_noexp"].is_unique:
            raise SystemExit("У бессрочного пакета несколько 30-дневных пар — проверьте данные")
        pairs["premium"] = pairs["vol"].map(NOEXP_PREMIUM)
        pairs["parity"] = df.loc[pairs["index_noexp"], "cost"].to_numpy() / df.loc[pairs["index_exp"], "cost"].to_numpy()
        self.pairs = pairs
        exp_any = df[df["plan"] == "expiring"].reset_index()[["index", "region", "vol", "validity"]]
        self.all_pairs = exp_any.merge(noexp, on=["region", "vol"], suffixes=("_exp", "_noexp"))

    def ladder_table(self, price):
        df = self.df
        t = df.loc[self.ladder_mask, ["region", "validity", "vol"]].assign(price=np.asarray(price)[self.ladder_mask.to_numpy()])
        return t.sort_values(["region", "validity", "vol"])

    def noexp_price(self, p_exp, pr):
        p = np.maximum(p_exp * pr["parity"].to_numpy(),
                       np.minimum(p_exp * (1 + pr["premium"].to_numpy()), p_exp + NOEXP_MAX_EUR))
        return np.maximum(to_x9(p, "up"), to_x9(p_exp + PRICE_STEP, "up"))

    def add_noexp(self, price):
        price = pd.Series(price, index=self.df.index, dtype=float).copy()
        pr = self.pairs
        price[pr["index_noexp"]] = self.noexp_price(price[pr["index_exp"]].to_numpy(), pr)
        need = price[self.all_pairs["index_exp"]].to_numpy() + PRICE_STEP
        floor_n = pd.Series(need, index=self.all_pairs["index_noexp"]).groupby(level=0).max()
        price[floor_n.index] = np.maximum(price[floor_n.index], to_x9(floor_n.to_numpy(), "up"))
        return price

    def fix_ladder(self, price, floor, plan):
        price = pd.Series(price, index=self.df.index, dtype=float).copy()
        floor = pd.Series(floor, index=self.df.index)
        t = self.ladder_table(price)
        t = t[(t["validity"] == -1) if plan == "non-expiring" else (t["validity"] != -1)]
        for _, g in t.groupby(["region", "validity"]):
            idx, vols = g.index.to_list(), g["vol"].to_numpy()
            for k in range(1, len(idx)):                       # от меньшего пакета к большему
                lo, hi = idx[k - 1], idx[k]
                if price[hi] < price[lo] + PRICE_STEP - 1e-9:
                    price[hi] = to_x9(price[lo] + PRICE_STEP, "up")
                limit = price[lo] * vols[k] / vols[k - 1] + PRICE_STEP
                if price[hi] > limit + 1e-9 and to_x9(limit, "down") >= floor[hi]:
                    price[hi] = to_x9(limit, "down")
            for k in range(len(idx) - 1, 0, -1):               # обратно: поднять меньший пакет
                lo, hi = idx[k - 1], idx[k]
                need = (price[hi] - PRICE_STEP) * vols[k - 1] / vols[k]
                if price[lo] < need - 1e-9:
                    price[lo] = to_x9(need, "up")
        return price

    def fix_ladder_all(self, price, floor, plan, passes=5):
        for _ in range(passes):
            new = self.fix_ladder(price, floor, plan)
            if new.equals(price):
                break
            price = new
        return price

    def apply_rules(self, price, floor):
        price = self.add_noexp(self.fix_ladder_all(price, floor, "expiring"))
        nx = (self.df["plan"] == "non-expiring").to_numpy()
        price[nx] = np.maximum(price[nx].to_numpy(), pd.Series(floor, index=self.df.index)[nx].to_numpy())
        return self.fix_ladder_all(price, floor, "non-expiring")


def read_raw(conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("""SELECT row_num, region_name, volume::float, validity_days, gb_bucket,
                              base_cost_eur::float, coalesce(qty, 0),
                              best_comp_clean::float, median_comp_clean::float
                       FROM raw.all_prices_reduced ORDER BY row_num""")
        rows = cur.fetchall()
    if not rows:
        raise SystemExit("raw.all_prices_reduced пуста — сначала загрузите xlsx (run_all.py)")
    df = pd.DataFrame(rows, columns=["row_num", "region", "vol", "validity", "bucket", "cost", "qty", "best", "median_comp"])
    df["bucket"] = np.select([df["bucket"].eq("Unlimited"), df["vol"] <= 3, df["vol"] <= 10],
                             ["Unlimited", "1-3GB", "5-10GB"], "20+GB")
    df["plan"] = np.select([df["validity"] == -1, df["bucket"] == "Unlimited"], ["non-expiring", "unlimited"], "expiring")
    df["grp"] = np.where(df["bucket"].isin(["1-3GB", "5-10GB"]), "1-10GB", "20+GB")
    return df


def compute(df: pd.DataFrame, target_margin=TARGET_MARGIN) -> pd.Series:
    target_price = df["cost"] / (1 - df["grp"].map(target_margin))
    floor = pd.Series(to_x9(df["cost"], "up"), index=df.index)          # только «не ниже себестоимости»
    return Grid(df).apply_rules(np.maximum(to_x9(target_price), floor), floor)


def kpi(df: pd.DataFrame, price: pd.Series) -> dict:
    q = df["qty"]
    has = df["best"].notna()
    q_comp = q[has].sum()
    rev_g = (q * price).groupby(df["grp"]).sum()
    rev_b = (q * price).groupby(df["bucket"]).sum()
    prof = q * (price - df["cost"])
    return {
        "TOP-1, % of sales": (q * (has & (price <= df["best"] - 0.01))).sum() / q_comp,
        "in market, % of sales": (q * (has & (price <= df["median_comp"] - 0.01))).sum() / q_comp,
        "revenue, €": (q * price).sum(),
        "gross profit, €": prof.sum(),
        "margin 1-3GB": prof.groupby(df["bucket"]).sum()["1-3GB"] / rev_b["1-3GB"],
        "margin 5-10GB": prof.groupby(df["bucket"]).sum()["5-10GB"] / rev_b["5-10GB"],
        "margin 1-10GB": prof.groupby(df["grp"]).sum()["1-10GB"] / rev_g["1-10GB"],
        "margin 20+GB": prof.groupby(df["grp"]).sum()["20+GB"] / rev_g["20+GB"],
    }


def compare(conn, got: dict) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT metric, value FROM raw.cases_summary WHERE scenario = 'Baseline'")
        ref = dict(cur.fetchall())
    ok = True
    print(f"  {'Метрика':<24}{'Расчёт':>14}{'Файл':>14}")
    for k, v in got.items():
        if k not in ref or ref[k] is None:
            continue
        r = float(ref[k])
        good = abs(v - r) <= max(0.05, abs(r) * 1e-6)
        ok &= good
        fmt = (lambda x: f"{x:,.1f}".replace(",", " ")) if abs(r) > 1 else (lambda x: f"{x:.2%}")
        print(f"  {k:<24}{fmt(v):>14}{fmt(r):>14}  {'✓' if good else '✗'}")
    return ok


def write(conn, df: pd.DataFrame, price: pd.Series) -> int:
    rows = pd.DataFrame({"row_num": df["row_num"], "price": price.round(2)})
    with conn.cursor() as cur:
        # Baseline — цены, которые сейчас видит покупатель на витрине
        cur.execute("UPDATE pricing.scenarios SET is_live = false WHERE scenario_id <> %s", (SCENARIO_ID,))
        cur.execute("""INSERT INTO pricing.scenarios (scenario_id, title, source_column, is_live)
                       VALUES (%s, 'Baseline · целевая маржа 30/20, формат X,Y9, правила сетки (сейчас на витрине)',
                               'loader/baseline.py', true)
                       ON CONFLICT (scenario_id) DO UPDATE SET title = EXCLUDED.title, is_live = true""", (SCENARIO_ID,))
        cur.execute("CREATE TEMP TABLE tmp_baseline (row_num int, price numeric(12,2)) ON COMMIT DROP")
        with cur.copy("COPY tmp_baseline (row_num, price) FROM STDIN") as cp:
            for r in rows.itertuples(index=False):
                cp.write_row((int(r.row_num), float(r.price)))
        cur.execute("""INSERT INTO pricing.prices (product_id, scenario_id, price_eur)
                       SELECT p.product_id, %s, t.price
                       FROM tmp_baseline t JOIN catalog.products p ON p.source_row_num = t.row_num
                       ON CONFLICT (product_id, scenario_id) DO UPDATE SET price_eur = EXCLUDED.price_eur""",
                    (SCENARIO_ID,))
        n = cur.rowcount
    conn.commit()
    return n


def run(conn, target_margin=TARGET_MARGIN, dry_run=False, verbose=True) -> bool:
    df = read_raw(conn)
    price = compute(df, target_margin)
    ok = compare(conn, kpi(df, price)) if target_margin == TARGET_MARGIN else True
    if not dry_run:
        n = write(conn, df, price)
        if verbose:
            print(f"  Записано цен сценария '{SCENARIO_ID}': {n}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--margin-small", type=float, default=TARGET_MARGIN["1-10GB"], help="целевая маржа 1–10 ГБ")
    ap.add_argument("--margin-big", type=float, default=TARGET_MARGIN["20+GB"], help="целевая маржа 20+ ГБ и безлимита")
    ap.add_argument("--dry-run", action="store_true", help="посчитать и сверить, но не писать в базу")
    args = ap.parse_args()
    tm = {"1-10GB": args.margin_small, "20+GB": args.margin_big}
    with connect() as conn:
        print("Baseline" + (" (сверка с Cases_summary)" if tm == TARGET_MARGIN else f" с маржой {tm}"))
        ok = run(conn, tm, args.dry_run)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
