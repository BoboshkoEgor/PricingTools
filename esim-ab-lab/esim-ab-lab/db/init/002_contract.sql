-- =====================================================================
-- CONTRACT — единственный вход для A/B-платформы.
--
-- Платформа никогда не читает shop.* или sim.* напрямую, только эти
-- представления. Чтобы подключить реальные логи компании, достаточно
-- пересоздать представления поверх её таблиц с теми же колонками.
-- Это «канал 1» (исходные данные). «Канал 2» (действия групп A и B)
-- приходит в contract.events через поля experiment_id и variant.
-- =====================================================================

CREATE OR REPLACE VIEW contract.users AS
SELECT user_id, registered_at, home_country, platform, acquisition_channel
FROM shop.users;

CREATE OR REPLACE VIEW contract.products AS
SELECT p.product_id, p.sku, p.name, d.code AS destination_code, d.name AS destination,
       d.market, d.is_regional, p.gb_bucket, p.volume_gb, p.is_unlimited,
       p.validity_days, p.base_cost_eur AS cost_eur
FROM catalog.products p
JOIN catalog.destinations d USING (destination_id);

CREATE OR REPLACE VIEW contract.events AS
SELECT event_id, event_ts, user_id, session_id, event_type,
       destination_id, product_id, price_eur, experiment_id, variant
FROM shop.events;

CREATE OR REPLACE VIEW contract.orders AS
SELECT order_id, created_at, user_id, session_id, product_id,
       price_eur AS revenue_eur, cost_eur, status
FROM shop.orders;
