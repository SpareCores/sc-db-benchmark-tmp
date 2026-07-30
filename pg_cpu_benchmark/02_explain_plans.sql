-- Optional: explain plans for the mixed RO transaction (manual inspection).
-- Not used by pgbench. Shows index vs scan intent after 01_setup.sql.

SET jit = off;
SET work_mem = '64MB';
SET max_parallel_workers_per_gather = 0;

-- Indexed customer → orders
EXPLAIN (COSTS OFF)
SELECT o.order_id
FROM ro_cpu_order o
WHERE o.customer_id = 12345
  AND o.status IN ('paid', 'shipped', 'done')
ORDER BY o.ordered_at DESC
LIMIT 40;

-- Region filter (index) + join
EXPLAIN (COSTS OFF)
SELECT count(*)
FROM ro_cpu_customer c
JOIN ro_cpu_order o ON o.customer_id = c.customer_id
WHERE c.region = 'eu-west'
  AND (c.profile ->> 'plan') IN ('pro', 'ent')
  AND o.status <> 'cancel';

-- Contiguous order_id slice (prefer bitmap/seq on PK range; CPU expressions)
EXPLAIN (COSTS OFF)
SELECT count(*) FILTER (WHERE note ~ 'email=user[0-9]+@example\.com')
FROM ro_cpu_order
WHERE order_id BETWEEN 100000 AND 112000;
