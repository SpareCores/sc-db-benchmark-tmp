
dbset db pg
dbset bm TPC-C
vuset logtotemp 0
diset connection pg_host pg-wh-eval
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser postgres
diset tpcc pg_superuserpass postgres
diset tpcc pg_defaultdbase postgres
diset tpcc pg_storedprocs true
diset tpcc pg_partition false
diset tpcc pg_count_ware 40
diset tpcc pg_num_vu 8
buildschema
