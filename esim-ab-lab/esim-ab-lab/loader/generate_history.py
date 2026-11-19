"""Шаг 3. Синтетическая история магазина: пользователи, сессии, события, заказы.

Главное свойство: число заказов по каждому тарифу точно равно QTY из файла,
а цена продажи — цена выбранного сценария (по умолчанию `baseline`). Поэтому выручка
в базе совпадает с выручкой этого сценария из листа Cases_summary, и это проверяется после загрузки.

Всё остальное (время, пользователи, просмотры без покупки) — модель с
явными параметрами из PARAMS. Скрытые сегменты пользователей пишутся в
sim.user_traits: у реального магазина их нет, они нужны симулятору.
"""
import json

import numpy as np
import pandas as pd

from db import copy_df

PARAMS = {
    "seed": 42,
    "start": "2026-08-01",            # период истории; в файле период не указан — это допущение
    "days": 92,
    "scenario": "baseline",       # история на базовых ценах; итоговые — кандидат в вариант B
    "refund_rate": 0.015,
    "checkout_conversion": 0.55,      # buy_click → purchase
    "destination_to_click": 0.16,     # просмотр направления → buy_click
    "bounce_share": 0.15,             # сессии только с каталогом
    "direct_landing_share": 0.30,     # сессии, начатые сразу со страницы направления
    "buyer_share_of_visitors": 0.22,
    "returning_user_share": 0.35,     # зарегистрированы до начала периода
    "segments": {
        #            доля среди   среднее заказов  чувствительность    поездок
        #            не-покупателей   на покупателя    к цене (Beta a,b)   в год
        "budget":   {"visitors": 0.35, "repeat": 1.15, "beta": [8, 3], "trips": 2.0},
        "tourist":  {"visitors": 0.35, "repeat": 1.25, "beta": [5, 5], "trips": 2.5},
        "explorer": {"visitors": 0.15, "repeat": 1.60, "beta": [4, 5], "trips": 6.0},
        "business": {"visitors": 0.15, "repeat": 1.80, "beta": [2, 8], "trips": 9.0},
    },
}
SEGMENTS = list(PARAMS["segments"])
WEEKDAY = np.array([0.95, 0.95, 1.00, 1.08, 1.12, 0.98, 0.92])          # пн..вс
HOUR = np.array([2, 1, 1, 1, 1, 2, 3, 4, 5, 6, 6, 6, 6, 6, 6, 6, 7, 8, 9, 9, 8, 6, 4, 3], float)
PLATFORMS, PLATFORM_P = ["ios", "android", "web"], [0.48, 0.37, 0.15]
CHANNELS, CHANNEL_P = ["organic", "ads", "referral", "partner"], [0.40, 0.35, 0.15, 0.10]
COUNTRIES = ["DE", "GB", "US", "FR", "IT", "ES", "NL", "PL", "RU", "KZ", "AE", "TR", "IN", "BR", "CN"]
COUNTRY_P = np.array([14, 12, 12, 9, 8, 8, 5, 5, 7, 3, 3, 4, 4, 3, 3], float)


def segment_probs(prod: pd.DataFrame) -> np.ndarray:
    """Вероятность сегмента покупателя для каждого тарифа (колонки в порядке SEGMENTS)."""
    p = np.tile([0.30, 0.40, 0.15, 0.15], (len(prod), 1))
    vol = prod["volume_gb"].fillna(999).to_numpy()
    big = prod["is_unlimited"].to_numpy() | (vol >= 20)
    small = vol <= 3
    p[small] = [0.50, 0.30, 0.15, 0.05]
    p[big] = [0.10, 0.30, 0.15, 0.45]
    p[prod["validity_days"].isna().to_numpy()] = [0.15, 0.25, 0.45, 0.15]
    p[prod["is_regional"].to_numpy()] = [0.10, 0.25, 0.50, 0.15]
    return p / p.sum(1, keepdims=True)


def random_times(rng, n, start, days):
    """n случайных моментов с недельной сезонностью, ростом тренда и суточным профилем."""
    d = np.arange(days)
    dates = pd.to_datetime(start) + pd.to_timedelta(d, unit="D")
    w = WEEKDAY[dates.weekday] * (1 + 0.10 * d / days)
    day = rng.choice(days, size=n, p=w / w.sum())
    hour = rng.choice(24, size=n, p=HOUR / HOUR.sum())
    sec = rng.integers(0, 3600, size=n)
    return pd.to_datetime(start, utc=True) + pd.to_timedelta(day * 86400 + hour * 3600 + sec, unit="s")


def generate(conn, params=PARAMS):
    rng = np.random.default_rng(params["seed"])
    start, days = params["start"], params["days"]

    with conn.cursor() as cur:
        cur.execute(f"""
        SELECT p.product_id, p.destination_id, p.volume_gb::float AS volume_gb, p.is_unlimited,
               p.validity_days, d.is_regional, p.base_cost_eur::float AS cost_eur,
               pr.price_eur::float AS price_eur, a.qty
        FROM catalog.products p
        JOIN catalog.destinations d USING (destination_id)
        JOIN raw.all_prices_reduced a ON a.row_num = p.source_row_num
        JOIN pricing.prices pr ON pr.product_id = p.product_id AND pr.scenario_id = '{params["scenario"]}'
        ORDER BY p.product_id""")
        prod = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    prod["qty"] = prod["qty"].fillna(0).astype(int)

    # --- 1. Заказы: ровно QTY штук на тариф -------------------------------------------
    idx = np.repeat(np.arange(len(prod)), prod["qty"].to_numpy())
    P = len(idx)
    seg_p = segment_probs(prod)[idx]
    order_seg = (seg_p.cumsum(1) > rng.random((P, 1))).argmax(1)

    # --- 2. Покупатели по сегментам: часть делает повторные заказы ----------------------
    next_user, buyer_of_order = 0, np.empty(P, dtype=np.int64)
    user_seg = []
    for s, name in enumerate(SEGMENTS):
        rows = np.flatnonzero(order_seg == s)
        n_buyers = max(1, round(len(rows) / params["segments"][name]["repeat"]))
        weight = rng.gamma(0.8, size=n_buyers)
        # каждый покупатель получает хотя бы один заказ, остальные — повторные
        extra = rng.choice(n_buyers, size=len(rows) - n_buyers, p=weight / weight.sum())
        pick = rng.permutation(np.concatenate([np.arange(n_buyers), extra]))
        buyer_of_order[rows] = next_user + pick
        user_seg += [s] * n_buyers
        next_user += n_buyers
    n_buyer_ids = next_user

    # --- 3. Посетители без покупок ----------------------------------------------------
    n_visitors = round(n_buyer_ids / params["buyer_share_of_visitors"]) - n_buyer_ids
    vis_p = np.array([params["segments"][s]["visitors"] for s in SEGMENTS])
    user_seg += list(rng.choice(len(SEGMENTS), size=n_visitors, p=vis_p / vis_p.sum()))
    user_seg = np.array(user_seg)
    n_users = len(user_seg)
    visit_w = rng.gamma(1.0, size=n_users)

    # --- 4. Сессии ----------------------------------------------------------------------
    A = round(P * (1 / params["checkout_conversion"] - 1))
    D = round((P + A) * (1 / params["destination_to_click"] - 1))
    B = round((P + A + D) * params["bounce_share"] / (1 - params["bounce_share"]))

    order_ts = random_times(rng, P, start, days)
    qty_w = prod["qty"].to_numpy() + 0.2
    dest_demand = prod.groupby("destination_id")["qty"].sum()
    dest_ids = dest_demand.index.to_numpy()
    dest_w = (dest_demand.to_numpy() + 1.0) ** 0.7

    def other_users(n, buyer_share):
        from_buyers = rng.random(n) < buyer_share
        u = rng.choice(n_users, size=n, p=visit_w / visit_w.sum())
        u[from_buyers] = rng.integers(0, n_buyer_ids, from_buyers.sum())
        return u

    sessions = []
    # 4a. сессии с покупкой
    sessions.append(pd.DataFrame({"kind": "purchase", "user": buyer_of_order, "ts": order_ts,
                                  "pi": idx, "dest": prod["destination_id"].to_numpy()[idx]}))
    # 4b. брошенный чекаут
    ai = rng.choice(len(prod), size=A, p=qty_w / qty_w.sum())
    sessions.append(pd.DataFrame({"kind": "abandon", "user": other_users(A, 0.5),
                                  "ts": random_times(rng, A, start, days), "pi": ai,
                                  "dest": prod["destination_id"].to_numpy()[ai]}))
    # 4c. только просмотр направления
    sessions.append(pd.DataFrame({"kind": "browse", "user": other_users(D, 0.2),
                                  "ts": random_times(rng, D, start, days), "pi": -1,
                                  "dest": rng.choice(dest_ids, size=D, p=dest_w / dest_w.sum())}))
    # 4d. отказ на каталоге
    sessions.append(pd.DataFrame({"kind": "bounce", "user": other_users(B, 0.1),
                                  "ts": random_times(rng, B, start, days), "pi": -1, "dest": -1}))
    s = pd.concat(sessions, ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True)
    s["session_id"] = np.arange(1, len(s) + 1)
    direct = rng.random(len(s)) < params["direct_landing_share"]
    s["has_catalog"] = (s["kind"] == "bounce") | ~direct

    # --- 5. События -------------------------------------------------------------------
    sec = lambda lo, hi, n: pd.to_timedelta(rng.integers(lo, hi, n), unit="s")
    n = len(s)
    t_buy = s["ts"] - sec(20, 180, n)
    t_dest = t_buy - sec(30, 300, n)
    t_cat = t_dest - sec(10, 240, n)
    # сессия не должна начинаться раньше периода: сдвигаем её целиком вперёд
    shift = (pd.Timestamp(start, tz="UTC") - pd.Series(np.where(s["has_catalog"], t_cat, t_dest))).clip(lower=pd.Timedelta(0))
    shift = pd.TimedeltaIndex(shift)
    s["ts"], t_buy, t_dest, t_cat = s["ts"] + shift, t_buy + shift, t_dest + shift, t_cat + shift
    prod_id = np.where(s["pi"] >= 0, prod["product_id"].to_numpy()[s["pi"].clip(0)], -1)
    price = np.where(s["pi"] >= 0, prod["price_eur"].to_numpy()[s["pi"].clip(0)], np.nan)
    base = {"user_id": s["user"], "session_id": s["session_id"]}

    parts = []
    m = s["has_catalog"]
    parts.append(pd.DataFrame({**base, "event_ts": np.where(s["kind"] == "bounce", s["ts"], t_cat),
                               "event_type": "catalog_view", "destination_id": -1, "product_id": -1,
                               "price_eur": np.nan})[m])
    m = s["kind"] != "bounce"
    parts.append(pd.DataFrame({**base, "event_ts": np.where(s["kind"] == "browse", s["ts"], t_dest),
                               "event_type": "destination_view", "destination_id": s["dest"],
                               "product_id": -1, "price_eur": np.nan})[m])
    m = s["kind"].isin(["purchase", "abandon"])
    parts.append(pd.DataFrame({**base, "event_ts": np.where(s["kind"] == "abandon", s["ts"], t_buy),
                               "event_type": "buy_click", "destination_id": s["dest"],
                               "product_id": prod_id, "price_eur": price})[m])
    m = s["kind"] == "purchase"
    parts.append(pd.DataFrame({**base, "event_ts": s["ts"], "event_type": "purchase",
                               "destination_id": s["dest"], "product_id": prod_id, "price_eur": price})[m])
    ev = pd.concat(parts, ignore_index=True).sort_values(["event_ts", "session_id"], kind="stable")
    ev["event_ts"] = pd.to_datetime(ev["event_ts"], utc=True)
    ev = ev.reset_index(drop=True)
    ev.insert(0, "event_id", np.arange(1, len(ev) + 1))
    for c in ["destination_id", "product_id"]:
        ev[c] = ev[c].astype("Int64").replace(-1, pd.NA)

    # --- 6. Пользователи: только те, у кого есть события --------------------------------
    first_seen = ev.groupby("user_id")["event_ts"].min()
    uid = first_seen.index.to_numpy()
    k = len(uid)
    returning = rng.random(k) < params["returning_user_share"]
    reg = pd.DatetimeIndex(first_seen.values) - pd.to_timedelta(rng.integers(0, 120, k), unit="s")
    early = pd.to_datetime(start, utc=True) - pd.to_timedelta(rng.integers(1, 365 * 86400, k), unit="s")
    reg = np.where(returning, early, reg)
    seg_u = user_seg[uid]
    plat = np.where(seg_u == SEGMENTS.index("business"),
                    rng.choice(PLATFORMS, size=k, p=[0.65, 0.25, 0.10]),
                    rng.choice(PLATFORMS, size=k, p=PLATFORM_P))
    users = pd.DataFrame({"user_id": uid + 1, "registered_at": pd.to_datetime(reg, utc=True),
                          "home_country": rng.choice(COUNTRIES, size=k, p=COUNTRY_P / COUNTRY_P.sum()),
                          "platform": plat, "acquisition_channel": rng.choice(CHANNELS, size=k, p=CHANNEL_P)})
    ev["user_id"] = ev["user_id"] + 1

    traits = pd.DataFrame({"user_id": uid + 1, "segment": np.array(SEGMENTS)[seg_u]})
    sens = np.empty(k)
    trips = np.empty(k)
    for i, name in enumerate(SEGMENTS):
        m = seg_u == i
        a, b = params["segments"][name]["beta"]
        sens[m] = rng.beta(a, b, m.sum())
        trips[m] = rng.gamma(2.0, params["segments"][name]["trips"] / 2.0, m.sum())
    traits["price_sensitivity"] = sens.round(2)
    traits["trips_per_year"] = np.clip(trips, 0.5, 99).round(1)

    # --- 7. Заказы ----------------------------------------------------------------------
    po = ev[ev["event_type"] == "purchase"].copy()
    po = po.merge(prod[["product_id", "cost_eur"]], on="product_id")
    po = po.sort_values("event_ts", kind="stable").reset_index(drop=True)
    orders = pd.DataFrame({
        "order_id": np.arange(1, len(po) + 1), "created_at": po["event_ts"], "user_id": po["user_id"],
        "session_id": po["session_id"], "product_id": po["product_id"], "scenario_id": params["scenario"],
        "price_eur": po["price_eur"].round(2), "cost_eur": po["cost_eur"],
        "status": np.where(rng.random(len(po)) < params["refund_rate"], "refunded", "paid")})

    # --- 8. Запись в базу ---------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute("TRUNCATE shop.events, shop.orders, sim.user_traits, shop.users")
        cur.execute("INSERT INTO sim.runs (kind, seed, params) VALUES ('history', %s, %s) RETURNING run_id",
                    (params["seed"], json.dumps(params)))
        run_id = cur.fetchone()[0]
    copy_df(conn, "shop.users", users)
    copy_df(conn, "sim.user_traits", traits)
    copy_df(conn, "shop.events", ev[["event_id", "event_ts", "user_id", "session_id", "event_type",
                                     "destination_id", "product_id", "price_eur"]])
    copy_df(conn, "shop.orders", orders)
    with conn.cursor() as cur:
        cur.execute("UPDATE sim.runs SET finished_at = now() WHERE run_id = %s", (run_id,))
        cur.execute("ANALYZE shop.users; ANALYZE shop.events; ANALYZE shop.orders;")
    conn.commit()
    return {"users": len(users), "sessions": len(s), "events": len(ev), "orders": len(orders)}
