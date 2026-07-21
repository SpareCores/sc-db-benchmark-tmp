# Database sizing benchmarks (Postgres 18)

Scripts under [`scripts/`](scripts/) measure how HammerDB TPROC-C and BenchBase
(wikipedia / ycsb) scale with concurrency on Azure AMD SKUs, and how much
host-aware Postgres GUC tuning helps vs a simple buffer-sized baseline.

## Scripts

| Script | Role |
|--------|------|
| [`scripts/run_wh_sizing_eval.py`](scripts/run_wh_sizing_eval.py) | HammerDB TPROC-C VU ladder |
| [`scripts/run_benchbase_sizing_eval.py`](scripts/run_benchbase_sizing_eval.py) | BenchBase wikipedia + ycsb ladder |
| [`scripts/pg_tune_gucs.py`](scripts/pg_tune_gucs.py) | Host-topology GUC planner (`--pg-tune-host`) |
| [`scripts/run_final_benchmark.sh`](scripts/run_final_benchmark.sh) | Full matrix: baseline/tuned × disk/tmpfs × both benches |
| [`scripts/copy.sh`](scripts/copy.sh) | rsync `/bench/run3/<sku>/` → `./run3/<sku>/` |
| [`scripts/generate_results_md.py`](scripts/generate_results_md.py) | Rebuild [`RESULTS.md`](RESULTS.md) from CSVs |
| [`scripts/run_multi_schema_eval.py`](scripts/run_multi_schema_eval.py) | Multi-database TPROC-C experiment |
| [`scripts/guc_sweep_vu32.py`](scripts/guc_sweep_vu32.py) | GUC sweep harness (F32 tmpfs) |

Default ladder: **4 / 8 / 16 / 32 / 64** VUs (or BenchBase terminals) at
**5 warehouses/VU**. Rampup 2 min, measurement 5 min.

## Method

Without keying/thinking time, HammerDB treats **~1 VU ≈ 1 DB core**. Each
rung:

1. Fresh empty Postgres (`docker volume prune` + new privileged container)
2. Schema load (`buildschema` / BenchBase create+load)
3. Timed run (rampup + measurement)

**Baseline GUCs** (default): sc-inspector async profile with
`shared_buffers` = 25% RAM, `effective_cache_size` = 75% RAM (fixed across
the ladder).

**Tuned GUCs** (`--pg-tune-host`): [`scripts/pg_tune_gucs.py`](scripts/pg_tune_gucs.py)
sizes from live topology (physical vs logical CPUs, NUMA, RAM). For OLTP it
also applies F32 tmpfs-sweep winners (`io_uring`, lower `shared_buffers` on
tmpfs, `work_mem=16MB`, `max_parallel_workers_per_gather=0`, …).

Storage:

- default: anonymous Docker volume on `/var/lib/postgresql`
- `--pg-tmpfs`: tmpfs sized `max(16, 50% RAM)` GiB

All containers run with `--privileged`, `seccomp=unconfined`, and
`memlock=-1:-1` (required for PG18 `io_uring` in Docker).

Images: `postgres:18`, `tpcorg/hammerdb:postgres`,
`benchbase.azurecr.io/benchbase-postgres:latest`.

### Metrics

- HammerDB: NOPM / TPM (+ per-VU). Prefer same **VU/vCPU** when comparing SKUs.
- BenchBase: TPM / TPS + latency percentiles; SF sized so schema ≈ HammerDB WH footprint.

## Run on a host

```bash
cd /bench/src   # after rsync of scripts/

# Single suite
python3 run_wh_sizing_eval.py --skip-raw
python3 run_wh_sizing_eval.py --pg-tmpfs --pg-tune-host --skip-raw
python3 run_benchbase_sizing_eval.py --pg-tmpfs --skip-raw

# Full matrix in screen (skips suites that already have complete CSVs)
SKU_NAME=Standard_F32ams_v6 screen -dmS run3 ./run_final_benchmark.sh
```

Useful flags: `--vus 4,8,16,32,64`, `--pg-tmpfs`, `--pg-tune-host`,
`--skip-raw` (CSV + meta only), `--skip-pull`.

Pull results (from this repo directory):

```bash
./scripts/copy.sh
python3 scripts/generate_results_md.py
```

## Outputs

Per suite under `/bench/run3/<sku>/<bench>_<disk|tmpfs>_<baseline|tuned>/`
(copied to [`run3/`](run3/)):

| path | content |
|------|---------|
| `results.csv` | one row per VU / workload |
| `meta.json` | host, images, GUC policy / plan |

With `--skip-raw`, HammerDB/BenchBase log trees are not persisted.

## Provisioning

```shell
sc-runner create azure --instance Standard_F32ams_v6 \
  --public-key "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEPMwX6HY8inovVAqUrAKvqY0zabNoWfmN/7UlNsBvZ4 info@sparecores.com" \
  --disk-size 200 --region newzealandnorth --disk-type Premium_LRS
```

Measured SKUs (NewZealandNorth): `Standard_E16as_v6`, `Standard_F16ams_v6`,
`Standard_F32ams_v6`. F16/F32 report **AMD EPYC 9V74**, 1 NUMA node.

## Result sets

Summary tables + Mermaid charts: **[RESULTS.md](RESULTS.md)**

| dir | notes |
|-----|--------|
| [`run3/`](run3/) | **Current.** Baseline vs `--pg-tune-host`, disk + tmpfs, HammerDB + BenchBase |
| [`guc_sweep_f32/`](guc_sweep_f32/) | F32 vu32/tmpfs GUC sweep that calibrated `pg_tune_gucs.py` (see RESULTS) |

### BenchBase SF ladder (matched to 5 WH/VU)

| terminals | WH-equiv | wikipedia SF | ycsb SF | ~schema GiB |
|-----------|----------|--------------|---------|-------------|
| 4 | 20 | 13 | 1676 | ~1.9 |
| 8 | 40 | 26 | 3351 | ~3.8 |
| 16 | 80 | 51 | 6702 | ~7.6 |
| 32 | 160 | 103 | 13405 | ~15.2 |
| 64 | 320 | 205 | 26810 | ~30.4 |
