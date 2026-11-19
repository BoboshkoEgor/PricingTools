-- =====================================================================
-- eSIM A/B Lab — схема базы данных
-- Выполняется автоматически при первом старте контейнера Postgres.
--
-- Слои:
--   raw       — файл xlsx как есть, без изменений (можно пересчитать всё с нуля)
--   catalog   — справочник: направления и тарифы
--   pricing   — цены по сценариям, цены конкурентов, итоги кейсов
--   shop      — «боевые» данные магазина: пользователи, события, заказы
--   sim       — скрытая «истина» симулятора; при реальных данных пустая
--   contract  — представления, через которые A/B-платформа читает данные
--   abp       — сама A/B-платформа (эксперименты, назначения, результаты) — следующий этап
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS pricing;
CREATE SCHEMA IF NOT EXISTS shop;
CREATE SCHEMA IF NOT EXISTS sim;
CREATE SCHEMA IF NOT EXISTS contract;
CREATE SCHEMA IF NOT EXISTS abp;

-- ---------------------------------------------------------------------
-- RAW: копия листов xlsx. Имена колонок приведены к snake_case.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.all_prices_reduced (
    row_num                 int PRIMARY KEY,     -- номер строки в листе (с 1)
    id                      bigint,
    sku                     text,
    product_name            text,
    volume                  numeric,
    unit                    text,
    validity_days           int,
    currency                text,
    coverage                text,
    region_name             text,
    top_rest                text,
    base_cost_usd           numeric,
    gb_bucket               text,
    market                  text,
    subregion               text,
    base_cost_eur           numeric,
    base_cost_non_expiring  text,                -- в файле смесь чисел и текста «сравнить не с чем»
    recommended_price_eur   numeric,
    comp1                   numeric,
    comp2                   numeric,
    comp3                   numeric,
    comp1_alt               numeric,             -- колонки _comp1.._comp3 из файла
    comp2_alt               numeric,
    comp3_alt               numeric,
    qty                     int,
    recommended_price_1     numeric,
    recommended_price_2     numeric,
    recommended_price_3     numeric,
    best_comp_clean         numeric,
    median_comp_clean       numeric,
    loaded_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.cases_summary (
    metric      text NOT NULL,
    scenario    text NOT NULL,                   -- Baseline / 1. Conservative / 2. Medium / 3. Aggressive
    value       numeric,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric, scenario)
);

-- ---------------------------------------------------------------------
-- CATALOG
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS catalog.destinations (
    destination_id  serial PRIMARY KEY,
    code            text NOT NULL UNIQUE,        -- ISO-код страны (TR) или код пакета (EUROPE, ASIA_26)
    name            text NOT NULL,               -- как в файле: Turkey, Europe
    is_regional     boolean NOT NULL,            -- региональный или глобальный пакет
    market          text NOT NULL,               -- Europe, Asia, ...
    subregion       text NOT NULL,
    tier            text CHECK (tier IN ('TOP', 'rest'))
);

-- Какие страны покрывает региональный пакет (для стран — сама страна)
CREATE TABLE IF NOT EXISTS catalog.destination_countries (
    destination_id  int  NOT NULL REFERENCES catalog.destinations,
    iso_code        text NOT NULL,
    PRIMARY KEY (destination_id, iso_code)
);

CREATE TABLE IF NOT EXISTS catalog.products (
    product_id              serial PRIMARY KEY,
    sku                     text NOT NULL UNIQUE,
    sku_generated           boolean NOT NULL DEFAULT false,  -- true, если SKU в файле не было
    source_row_num          int NOT NULL UNIQUE REFERENCES raw.all_prices_reduced (row_num),
    source_id               bigint,                          -- ID из файла (не уникален)
    name                    text NOT NULL,
    destination_id          int NOT NULL REFERENCES catalog.destinations,
    volume_gb               numeric,                         -- NULL для безлимита
    is_unlimited            boolean NOT NULL,
    validity_days           int,                             -- NULL = бессрочный тариф
    gb_bucket               text NOT NULL,                   -- 1-10GB / 10+GB / Unlimited
    base_cost_usd           numeric(12,4) NOT NULL,
    base_cost_eur           numeric(12,4) NOT NULL,
    noexp_analog_cost_eur   numeric(12,4)                    -- себестоимость бессрочного аналога, если есть
);

-- Журнал проблем качества данных, найденных при загрузке
CREATE TABLE IF NOT EXISTS catalog.load_issues (
    issue_id        serial PRIMARY KEY,
    source_row_num  int,
    issue           text NOT NULL,
    action_taken    text NOT NULL,
    logged_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- PRICING
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing.scenarios (
    scenario_id     text PRIMARY KEY,
    title           text NOT NULL,
    source_column   text NOT NULL,
    is_live         boolean NOT NULL DEFAULT false   -- цены, которые сейчас видит покупатель
);

CREATE TABLE IF NOT EXISTS pricing.prices (
    product_id      int  NOT NULL REFERENCES catalog.products,
    scenario_id     text NOT NULL REFERENCES pricing.scenarios,
    price_eur       numeric(12,2) NOT NULL CHECK (price_eur > 0),
    PRIMARY KEY (product_id, scenario_id)
);

CREATE TABLE IF NOT EXISTS pricing.competitor_prices (
    product_id      int  NOT NULL REFERENCES catalog.products,
    competitor      text NOT NULL,                   -- comp1 / comp2 / comp3
    price_eur       numeric(12,4) NOT NULL CHECK (price_eur > 0),
    PRIMARY KEY (product_id, competitor)
);

CREATE TABLE IF NOT EXISTS pricing.competitor_summary (
    product_id      int PRIMARY KEY REFERENCES catalog.products,
    best_eur        numeric(12,4),                   -- best_comp (clean) из файла
    median_eur      numeric(12,4)                    -- median_comp (clean) из файла
);

CREATE TABLE IF NOT EXISTS pricing.case_summary (
    metric          text NOT NULL,
    scenario_id     text NOT NULL,                   -- baseline / conservative / medium / aggressive
    value           numeric,
    PRIMARY KEY (metric, scenario_id)
);

-- ---------------------------------------------------------------------
-- SHOP: данные, которые оставил бы настоящий магазин
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shop.users (
    user_id             bigint PRIMARY KEY,
    registered_at       timestamptz NOT NULL,
    home_country        text,                        -- страна проживания покупателя
    platform            text NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
    acquisition_channel text NOT NULL
);

CREATE TABLE IF NOT EXISTS shop.events (
    event_id        bigint PRIMARY KEY,
    event_ts        timestamptz NOT NULL,
    user_id         bigint NOT NULL REFERENCES shop.users,
    session_id      bigint NOT NULL,
    event_type      text NOT NULL CHECK (event_type IN
                      ('catalog_view', 'destination_view', 'buy_click', 'purchase')),
    destination_id  int REFERENCES catalog.destinations,
    product_id      int REFERENCES catalog.products,
    price_eur       numeric(12,2),
    experiment_id   text,                            -- заполняет A/B-платформа (этап 2)
    variant         text
);
CREATE INDEX IF NOT EXISTS events_ts_idx       ON shop.events (event_ts);
CREATE INDEX IF NOT EXISTS events_user_idx     ON shop.events (user_id, event_ts);
CREATE INDEX IF NOT EXISTS events_exp_idx      ON shop.events (experiment_id, variant) WHERE experiment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS shop.orders (
    order_id        bigint PRIMARY KEY,
    created_at      timestamptz NOT NULL,
    user_id         bigint NOT NULL REFERENCES shop.users,
    session_id      bigint NOT NULL,
    product_id      int NOT NULL REFERENCES catalog.products,
    scenario_id     text NOT NULL REFERENCES pricing.scenarios,   -- по каким ценам продали
    price_eur       numeric(12,2) NOT NULL,
    cost_eur        numeric(12,4) NOT NULL,
    status          text NOT NULL CHECK (status IN ('paid', 'refunded'))
);
CREATE INDEX IF NOT EXISTS orders_created_idx ON shop.orders (created_at);
CREATE INDEX IF NOT EXISTS orders_user_idx    ON shop.orders (user_id);

-- ---------------------------------------------------------------------
-- SIM: то, чего у реального магазина нет. Нужно симулятору и LLM-персонам.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sim.runs (
    run_id          serial PRIMARY KEY,
    kind            text NOT NULL,                   -- history / experiment
    seed            int NOT NULL,
    params          jsonb NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

CREATE TABLE IF NOT EXISTS sim.user_traits (
    user_id             bigint PRIMARY KEY REFERENCES shop.users,
    segment             text NOT NULL,               -- budget / tourist / business / explorer
    price_sensitivity   numeric(4,2) NOT NULL,       -- 0 = цена не важна, 1 = решает только цена
    trips_per_year      numeric(4,1) NOT NULL
);
