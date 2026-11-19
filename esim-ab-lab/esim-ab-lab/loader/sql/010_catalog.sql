-- =====================================================================
-- raw → catalog, pricing. Полная пересборка: можно запускать повторно.
-- Внимание: очищает и shop/sim, потому что они ссылаются на каталог.
-- =====================================================================
BEGIN;

TRUNCATE shop.events, shop.orders, sim.user_traits, shop.users,
         pricing.prices, pricing.competitor_prices, pricing.competitor_summary,
         pricing.case_summary, pricing.scenarios,
         catalog.products, catalog.destination_countries, catalog.destinations,
         catalog.load_issues
RESTART IDENTITY CASCADE;

-- 1. Рабочая таблица: строка файла + код направления из SKU -------------
CREATE TEMP TABLE r ON COMMIT DROP AS
SELECT a.*,
       a.subregion IN ('Regional Packages', 'Global Packages')           AS is_regional,
       NULLIF(regexp_replace(a.sku, '-\d+-GB-[A-Z0-9]+$', ''), a.sku)     AS sku_prefix
FROM raw.all_prices_reduced a;

-- Канонический код для каждого названия в файле.
-- Страны: самый частый префикс SKU, иначе самый частый COVERAGE.
-- Пакеты: самый частый префикс SKU (у пакетов COVERAGE — список стран).
CREATE TEMP TABLE name_code ON COMMIT DROP AS
SELECT region_name,
       COALESCE(mode() WITHIN GROUP (ORDER BY sku_prefix),
                CASE WHEN bool_and(NOT is_regional) THEN mode() WITHIN GROUP (ORDER BY coverage) END) AS code
FROM r
GROUP BY region_name;

ALTER TABLE r ADD COLUMN code text;
UPDATE r SET code = nc.code FROM name_code nc WHERE nc.region_name = r.region_name;

-- Проблемы качества: COVERAGE страны не совпадает с её кодом
INSERT INTO catalog.load_issues (source_row_num, issue, action_taken)
SELECT row_num,
       format('Страна %s: COVERAGE=%s, а SKU указывает на %s', region_name, coverage, code),
       'Использован код из SKU'
FROM r
WHERE NOT is_regional AND coverage IS DISTINCT FROM code;

INSERT INTO catalog.load_issues (source_row_num, issue, action_taken)
SELECT row_num, format('Пакет %s без кода (нет SKU)', region_name), 'Строка пропущена'
FROM r WHERE code IS NULL;

-- 2. Направления ----------------------------------------------------------
INSERT INTO catalog.destinations (code, name, is_regional, market, subregion, tier)
SELECT code,
       mode() WITHIN GROUP (ORDER BY region_name),
       bool_or(is_regional),
       mode() WITHIN GROUP (ORDER BY market),
       mode() WITHIN GROUP (ORDER BY subregion),
       CASE WHEN bool_or(top_rest = 'TOP') THEN 'TOP' WHEN bool_or(top_rest = 'rest') THEN 'rest' END
FROM r
WHERE code IS NOT NULL
GROUP BY code;

-- Названия, которые в файле записаны по-разному для одного кода
INSERT INTO catalog.load_issues (source_row_num, issue, action_taken)
SELECT NULL, format('Код %s записан под названиями: %s', code, string_agg(DISTINCT region_name, ', ')),
       'Взято самое частое название'
FROM r WHERE code IS NOT NULL
GROUP BY code HAVING count(DISTINCT region_name) > 1;

-- 3. Покрытие стран -----------------------------------------------------------
INSERT INTO catalog.destination_countries (destination_id, iso_code)
SELECT DISTINCT d.destination_id, trim(c)
FROM r
JOIN catalog.destinations d USING (code)
CROSS JOIN LATERAL unnest(string_to_array(r.coverage, ',')) AS c
WHERE r.is_regional AND r.coverage IS NOT NULL
UNION
SELECT destination_id, code FROM catalog.destinations WHERE NOT is_regional;

-- 4. Тарифы ---------------------------------------------------------------
CREATE TEMP TABLE p ON COMMIT DROP AS
SELECT r.*,
       d.destination_id,
       COALESCE(r.sku,
                CASE WHEN r.gb_bucket = 'Unlimited'
                     THEN r.code || '-UNL-' || r.validity_days || 'D'
                     ELSE r.code || '-' || trim_scale(r.volume) || '-GB-' ||
                          CASE WHEN r.validity_days = -1 THEN 'NOEXP' ELSE r.validity_days::text END
                END) AS sku_final
FROM r JOIN catalog.destinations d USING (code);

INSERT INTO catalog.products
      (sku, sku_generated, source_row_num, source_id, name, destination_id, volume_gb, is_unlimited,
       validity_days, gb_bucket, base_cost_usd, base_cost_eur, noexp_analog_cost_eur)
SELECT CASE WHEN count(*) OVER (PARTITION BY sku_final) > 1 THEN sku_final || '-R' || row_num ELSE sku_final END,
       sku IS NULL,
       row_num, id, product_name, destination_id,
       volume, gb_bucket = 'Unlimited',
       NULLIF(validity_days, -1),
       gb_bucket, base_cost_usd, base_cost_eur,
       CASE WHEN base_cost_non_expiring ~ '^[0-9]+(\.[0-9]+)?$' THEN base_cost_non_expiring::numeric END
FROM p
ORDER BY row_num;

INSERT INTO catalog.load_issues (source_row_num, issue, action_taken)
SELECT source_row_num, format('Нет SKU в файле: %s', name), format('Сгенерирован SKU %s', sku)
FROM catalog.products WHERE sku_generated;

-- 5. Цены по сценариям ------------------------------------------------------
INSERT INTO pricing.scenarios (scenario_id, title, source_column, is_live) VALUES
  ('recommended',  'Итоговая рекомендованная цена (кандидат в вариант B)', 'Recommended price, euro', false),
  ('conservative', 'Сценарий 1 · консервативный',                       'recommended_price 1',     false),
  ('medium',       'Сценарий 2 · средний',                              'recommended_price 2',     false),
  ('aggressive',   'Сценарий 3 · агрессивный',                          'recommended_price 3',     false);

INSERT INTO pricing.prices (product_id, scenario_id, price_eur)
SELECT pr.product_id, s.scenario_id, s.price
FROM catalog.products pr
JOIN raw.all_prices_reduced a ON a.row_num = pr.source_row_num
CROSS JOIN LATERAL (VALUES ('recommended',  a.recommended_price_eur),
                           ('conservative', a.recommended_price_1),
                           ('medium',       a.recommended_price_2),
                           ('aggressive',   a.recommended_price_3)) AS s(scenario_id, price)
WHERE s.price > 0;

-- 6. Конкуренты ------------------------------------------------------------
INSERT INTO pricing.competitor_prices (product_id, competitor, price_eur)
SELECT pr.product_id, c.competitor, c.price
FROM catalog.products pr
JOIN raw.all_prices_reduced a ON a.row_num = pr.source_row_num
CROSS JOIN LATERAL (VALUES ('comp1', a.comp1), ('comp2', a.comp2), ('comp3', a.comp3)) AS c(competitor, price)
WHERE c.price > 0;

INSERT INTO pricing.competitor_summary (product_id, best_eur, median_eur)
SELECT pr.product_id, a.best_comp_clean, a.median_comp_clean
FROM catalog.products pr
JOIN raw.all_prices_reduced a ON a.row_num = pr.source_row_num
WHERE a.best_comp_clean IS NOT NULL OR a.median_comp_clean IS NOT NULL;

-- 7. Итоги кейсов -----------------------------------------------------------
INSERT INTO pricing.case_summary (metric, scenario_id, value)
SELECT metric,
       CASE scenario WHEN 'Baseline' THEN 'baseline' WHEN '1. Conservative' THEN 'conservative'
                     WHEN '2. Medium' THEN 'medium'  WHEN '3. Aggressive' THEN 'aggressive' ELSE scenario END,
       value
FROM raw.cases_summary;

COMMIT;
