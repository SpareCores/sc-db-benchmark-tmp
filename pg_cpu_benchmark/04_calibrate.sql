-- Warm shared_buffers + size check. Timing: use pgbench (more realistic than DO/).
--
--   psql ... -f 01_setup.sql
--   psql ... -f 04_calibrate.sql
--   pgbench -n -c 1 -j 1 -T 20 -f 03_run_benchmark.sql ...
-- Target: latency average ≈ 50–100 ms at -c 1 (cached). Tune Q3 slice width
-- (order_id + N in 03_run_benchmark.sql) if outside that band.

SET jit = off;

SELECT sum(customer_id) AS warm_customers FROM ro_cpu_customer;
SELECT sum(order_id) AS warm_orders FROM ro_cpu_order;
SELECT sum(product_id) AS warm_products FROM ro_cpu_product;
SELECT sum(qty) AS warm_items FROM ro_cpu_order_item;

SELECT
    relname,
    pg_size_pretty(pg_relation_size(oid)) AS heap,
    pg_size_pretty(pg_total_relation_size(oid)) AS total
FROM pg_class
WHERE relname LIKE 'ro_cpu_%' AND relkind = 'r'
ORDER BY pg_total_relation_size(oid) DESC;

SELECT pg_size_pretty(sum(pg_total_relation_size(oid))) AS all_ro_cpu_total
FROM pg_class
WHERE relname LIKE 'ro_cpu_%' AND relkind = 'r';
