-- Примеры запросов к базе. Открывайте в DBeaver, DataGrip или Adminer.

-- 1. Выручка и маржа по сценариям цен при исторических продажах (QTY).
--    Должно совпасть с листом Cases_summary.
SELECT pr.scenario_id,
       round(sum(a.qty * pr.price_eur), 1)                                   AS revenue_eur,
       round(sum(a.qty * (pr.price_eur - p.base_cost_eur)), 1)               AS gross_profit_eur,
       round(100 * sum(a.qty * (pr.price_eur - p.base_cost_eur))
                 / sum(a.qty * pr.price_eur), 1)                             AS margin_pct
FROM pricing.prices pr
JOIN catalog.products p USING (product_id)
JOIN raw.all_prices_reduced a ON a.row_num = p.source_row_num
GROUP BY 1 ORDER BY 2 DESC;

-- 2. Воронка по дням: сессии → направление → клик «Купить» → покупка
SELECT date_trunc('day', event_ts)::date AS day,
       count(DISTINCT session_id)                                                   AS sessions,
       count(DISTINCT session_id) FILTER (WHERE event_type = 'destination_view')    AS dest_views,
       count(DISTINCT session_id) FILTER (WHERE event_type = 'buy_click')           AS buy_clicks,
       count(DISTINCT session_id) FILTER (WHERE event_type = 'purchase')            AS purchases
FROM contract.events
GROUP BY 1 ORDER BY 1;

-- 3. Выручка, заказы и средний чек по рынкам
SELECT p.market,
       count(*)                             AS orders,
       round(sum(o.revenue_eur), 0)         AS revenue_eur,
       round(avg(o.revenue_eur), 2)         AS avg_check_eur,
       round(100 * sum(o.revenue_eur - o.cost_eur) / sum(o.revenue_eur), 1) AS margin_pct
FROM contract.orders o
JOIN contract.products p USING (product_id)
WHERE o.status = 'paid'
GROUP BY 1 ORDER BY 3 DESC;

-- 4. Какие пакеты покрывают Турцию (страна + региональные)
SELECT d.code, d.name, d.is_regional
FROM catalog.destination_countries c
JOIN catalog.destinations d USING (destination_id)
WHERE c.iso_code = 'TR';

-- 5. Где мы ТОП-1 по цене, в рынке или вне рынка (по ценам на витрине)
SELECT CASE WHEN pr.price_eur <= cs.best_eur - 0.01   THEN 'ТОП-1'
            WHEN pr.price_eur <= cs.median_eur - 0.01 THEN 'в рынке'
            ELSE 'вне рынка' END                     AS position,
       count(*)                                        AS skus,
       sum(a.qty)                                      AS sales
FROM pricing.prices pr
JOIN pricing.competitor_summary cs USING (product_id)
JOIN catalog.products p USING (product_id)
JOIN raw.all_prices_reduced a ON a.row_num = p.source_row_num
WHERE pr.scenario_id = (SELECT scenario_id FROM pricing.scenarios WHERE is_live)
GROUP BY 1 ORDER BY 3 DESC;

-- 6. Проблемы качества данных, найденные при загрузке
SELECT issue, action_taken FROM catalog.load_issues
WHERE issue NOT LIKE 'Нет SKU%'
UNION ALL
SELECT 'Нет SKU в файле: ' || count(*) || ' безлимитных тарифов', 'Сгенерированы SKU вида AU-UNL-10D'
FROM catalog.load_issues WHERE issue LIKE 'Нет SKU%';

-- 7. Скрытая «истина» симулятора: сегменты покупателей (в реальных данных этого нет)
SELECT t.segment,
       count(DISTINCT t.user_id)                        AS users,
       count(DISTINCT o.user_id)                        AS buyers,
       round(avg(t.price_sensitivity), 2)               AS avg_price_sensitivity,
       round(avg(o.revenue_eur), 2)                     AS avg_check_eur
FROM sim.user_traits t
LEFT JOIN contract.orders o USING (user_id)
GROUP BY 1 ORDER BY 2 DESC;
