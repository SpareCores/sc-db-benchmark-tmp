
dbset db pg
dbset bm TPC-C
vuset logtotemp 1
vuset unique 1
diset connection pg_host pg-wh-eval
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser postgres
diset tpcc pg_superuserpass postgres
diset tpcc pg_defaultdbase tpcc
diset tpcc pg_user tpcc
diset tpcc pg_pass tpcc
diset tpcc pg_storedprocs true
diset tpcc pg_driver timed
diset tpcc pg_timeprofile true
diset tpcc pg_rampup 2
diset tpcc pg_duration 5
loadscript
vuset vu 32
vucreate
set jobid [ vurun ]
vudestroy
puts SC_TIMING_JSON_START
job $jobid timing
puts SC_TIMING_JSON_END
