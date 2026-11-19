"""Полная пересборка базы: схема → xlsx → каталог → Baseline → синтетическая история → проверки.

Запуск:  python loader/run_all.py --xlsx data/Pricing_analyst_task_PY_3cases.xlsx
База:    переменная DATABASE_URL (по умолчанию postgresql://esim:esim@localhost:5433/esim)
"""
import argparse
import time
from pathlib import Path

import baseline
import generate_history
import load_raw
from db import ROOT, connect, run_sql_file

CHECKS = [
    ("Тарифов в каталоге", "SELECT count(*) FROM catalog.products", None),
    ("Направлений", "SELECT count(*) FROM catalog.destinations", None),
    ("Проблем качества данных", "SELECT count(*) FROM catalog.load_issues", None),
    ("Заказов", "SELECT count(*) FROM shop.orders", "SELECT sum(qty) FROM raw.all_prices_reduced"),
    ("Тарифов, где заказов ≠ QTY", """
        SELECT count(*) FROM catalog.products p
        JOIN raw.all_prices_reduced a ON a.row_num = p.source_row_num
        LEFT JOIN (SELECT product_id, count(*) n FROM shop.orders GROUP BY 1) o USING (product_id)
        WHERE coalesce(o.n, 0) <> coalesce(a.qty, 0)""", "SELECT 0"),
    ("Выручка заказов, €", "SELECT round(sum(price_eur), 1) FROM shop.orders",
     "SELECT round(value, 1) FROM pricing.case_summary WHERE metric = 'revenue, €' AND scenario_id = "
     "CASE (SELECT DISTINCT scenario_id FROM shop.orders) WHEN 'recommended' THEN 'medium' ELSE "
     "(SELECT DISTINCT scenario_id FROM shop.orders) END"),
    ("Себестоимость, €", "SELECT round(sum(cost_eur), 1) FROM shop.orders",
     "SELECT round(sum(qty * base_cost_eur), 1) FROM raw.all_prices_reduced"),
    ("Пользователей", "SELECT count(*) FROM shop.users", None),
    ("Событий", "SELECT count(*) FROM shop.events", None),
    ("Конверсия сессии в покупку", """
        SELECT round(100.0 * count(DISTINCT session_id) FILTER (WHERE event_type = 'purchase')
                     / count(DISTINCT session_id), 2) || '%' FROM shop.events""", None),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", type=Path, default=ROOT / "data" / "Pricing_analyst_task_PY_3cases.xlsx")
    ap.add_argument("--seed", type=int, default=generate_history.PARAMS["seed"])
    ap.add_argument("--history-prices", choices=["baseline", "recommended", "conservative", "medium", "aggressive"], default=generate_history.PARAMS["scenario"],
                    help="по каким ценам построить историю продаж")
    args = ap.parse_args()
    if not args.xlsx.exists():
        raise SystemExit(f"Файл не найден: {args.xlsx}. Положите xlsx в папку data/")

    t0 = time.time()
    conn = connect()
    print("1/5 Схема базы")
    for f in sorted((ROOT / "db" / "init").glob("*.sql")):
        run_sql_file(conn, f)
    print("2/5 Загрузка xlsx в raw:", load_raw.load(conn, args.xlsx), "строк")
    print("3/5 Каталог и цены")
    run_sql_file(conn, ROOT / "loader" / "sql" / "010_catalog.sql")
    print("4/5 Baseline (логика ноутбука), сверка с Cases_summary")
    ok_base = baseline.run(conn)
    print("5/5 Синтетическая история магазина по ценам:", args.history_prices)
    params = dict(generate_history.PARAMS, seed=args.seed, scenario=args.history_prices)
    stats = generate_history.generate(conn, params)
    print("    ", ", ".join(f"{k}: {v:,}".replace(",", " ") for k, v in stats.items()))

    print("\nПроверки")
    ok = ok_base
    with conn.cursor() as cur:
        for name, q, expected in CHECKS:
            cur.execute(q)
            got = cur.fetchone()[0]
            line = f"  {name:<32} {got}"
            if expected:
                cur.execute(expected)
                exp = cur.fetchone()[0]
                good = str(got) == str(exp)
                ok &= good
                line += f"   {'✓ совпадает с файлом' if good else f'✗ ожидалось {exp}'}"
            print(line)
    conn.close()
    print(f"\nГотово за {time.time() - t0:.0f} с" + ("" if ok else " — есть расхождения, см. выше"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
