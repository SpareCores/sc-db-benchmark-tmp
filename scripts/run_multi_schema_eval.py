#!/usr/bin/env python3
"""Multi-database HammerDB TPROC-C experiment (tmpfs-friendly).

HammerDB's PostgreSQL TPROC-C "schema" is one database (`pg_dbase`), not a
PostgreSQL namespace.  A single shared `tpcc` database concentrates warehouse /
district hot rows and catalog / buffer contention; on high-CPU hosts that
shows up as collapsing NOPM/VU past ~1 VU per core (see RESULTS.md).

This harness partitions the workload across independent databases so each
VU cluster hits its own warehouse set:

  total_vus=32, vus_per_schema=4  →  8 databases × 20 WH (5 WH/VU)

Timed runs launch one HammerDB client container per database in parallel.
NOPM is per-database (`sum(d_next_o_id) FROM district`) so we **sum** the
client results.  TPM comes from instance-wide `pg_stat_database`, so each
client reports roughly the same figure — we keep the **median**.

Also runs a single-database baseline on the same host/GUCs for comparison.

Does not modify run_wh_sizing_eval.py; imports shared helpers from it.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import socket
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import run_wh_sizing_eval as ev

RESULT_RE = re.compile(
    r"TEST RESULT : System achieved (\d+) NOPM from (\d+) PostgreSQL TPM"
)


def schema_names(n: int, prefix: str = "tpcc") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


def build_one_schema(
    *,
    db_name: str,
    user: str,
    password: str,
    warehouses: int,
    build_vus: int,
    log_path: Path,
) -> float:
    """buildschema into an independent database (reuses role if it exists)."""
    partition = "true" if warehouses >= 200 else "false"
    tcl = f"""
dbset db pg
dbset bm TPC-C
vuset logtotemp 0
diset connection pg_host {ev.PG_NAME}
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser {ev.PG_USER}
diset tpcc pg_superuserpass {ev.PG_PASSWORD}
diset tpcc pg_defaultdbase {ev.PG_ADMIN_DB}
diset tpcc pg_user {user}
diset tpcc pg_pass {password}
diset tpcc pg_dbase {db_name}
diset tpcc pg_storedprocs true
diset tpcc pg_partition {partition}
diset tpcc pg_count_ware {warehouses}
diset tpcc pg_num_vu {build_vus}
buildschema
"""
    t0 = time.time()
    out, rc = ev.hammerdb_run(tcl, log_path, timeout=28800)
    elapsed = time.time() - t0
    if "TPCC SCHEMA COMPLETE" not in out:
        raise RuntimeError(f"buildschema {db_name} failed rc={rc}: see {log_path}")
    return elapsed


def timed_run_one(
    *,
    db_name: str,
    user: str,
    password: str,
    run_vus: int,
    rampup_min: int,
    duration_min: int,
    log_path: Path,
    container_name: str,
) -> tuple[dict[str, int], float]:
    tcl = f"""
dbset db pg
dbset bm TPC-C
vuset logtotemp 1
vuset unique 1
diset connection pg_host {ev.PG_NAME}
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser {ev.PG_USER}
diset tpcc pg_superuserpass {ev.PG_PASSWORD}
diset tpcc pg_defaultdbase {db_name}
diset tpcc pg_user {user}
diset tpcc pg_pass {password}
diset tpcc pg_dbase {db_name}
diset tpcc pg_storedprocs true
diset tpcc pg_driver timed
diset tpcc pg_timeprofile true
diset tpcc pg_rampup {rampup_min}
diset tpcc pg_duration {duration_min}
loadscript
vuset vu {run_vus}
vucreate
set jobid [ vurun ]
vudestroy
puts SC_TIMING_JSON_START
job $jobid timing
puts SC_TIMING_JSON_END
"""
    tcl_path = log_path.with_suffix(".tcl")
    tcl_path.write_text(tcl)
    ev.sh(f"docker rm -f {container_name} 2>/dev/null || true", check=False)
    priv = " ".join(ev.DOCKER_PRIV_FLAGS)
    t0 = time.time()
    proc = ev.sh(
        f"docker run --rm {priv} --name {container_name} --network {ev.NETWORK} "
        f"-v {tcl_path}:/tmp/script.tcl:ro {ev.HDB_IMAGE} "
        f"bash -lc 'cd /home/hammerdb && ./hammerdbcli auto /tmp/script.tcl'",
        check=False,
        timeout=7200,
    )
    elapsed = time.time() - t0
    out = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(out)
    match = RESULT_RE.search(out)
    if not match:
        raise RuntimeError(f"no TEST RESULT for {db_name} rc={proc.returncode}: see {log_path}")
    return {"nopm": int(match.group(1)), "tpm": int(match.group(2))}, elapsed


def measure_db_sizes(db_names: list[str], raw_path: Path) -> dict[str, object]:
    sizes: dict[str, dict[str, object]] = {}
    total = 0
    for db in db_names:
        sql = (
            f"SELECT pg_database_size('{db}') AS bytes, "
            f"pg_size_pretty(pg_database_size('{db}')) AS pretty"
        )
        proc = ev.sh(
            f"docker exec -e PGPASSWORD={ev.PG_PASSWORD} {ev.PG_NAME} "
            f"psql -U {ev.PG_USER} -d {ev.PG_ADMIN_DB} -tAc \"{sql}\"",
            check=False,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or "|" not in out:
            raise RuntimeError(f"pg_database_size({db}) failed: {out or proc.stderr}")
        bytes_s, pretty = (part.strip() for part in out.split("|", 1))
        size_bytes = int(bytes_s)
        total += size_bytes
        sizes[db] = {
            "size_bytes": size_bytes,
            "size_pretty": pretty,
            "size_gib": round(size_bytes / (1024**3), 4),
        }
    info: dict[str, object] = {
        "databases": sizes,
        "total_size_bytes": total,
        "total_size_pretty": f"{total / (1024**3):.2f} GB",
        "total_size_gib": round(total / (1024**3), 4),
    }
    raw_path.write_text(json.dumps(info, indent=2) + "\n")
    return info


def bump_max_connections(settings: list[str], needed: int) -> list[str]:
    """Ensure max_connections covers parallel clients + monitors."""
    floor = max(400, needed)
    out: list[str] = []
    seen = False
    for a in settings:
        if a.startswith("max_connections="):
            cur = int(a.split("=", 1)[1])
            out.append(f"max_connections={max(cur, floor)}")
            seen = True
        else:
            out.append(a)
    if not seen:
        out.append(f"max_connections={floor}")
    return out


def docker_c_from_settings(settings: list[str]) -> list[str]:
    args: list[str] = []
    for s in settings:
        args.extend(["-c", s])
    return args


def start_postgres_with_gucs(
    mem: float,
    vcpus: int,
    *,
    tmpfs: bool,
    tmpfs_size_gib: int | None,
    max_conn: int,
) -> tuple[int, int, dict[str, object]]:
    """Like ev.start_postgres but raises max_connections for multi-client runs."""
    ev.cleanup_containers()
    ev.sh("docker volume prune -f >/dev/null 2>&1 || true", check=False)
    # Rebuild settings from the same formulas as ev.pg_gucs (RAM-fixed buffers).
    sb_gb = max(1, int(mem * ev.SHARED_BUFFERS_FRAC))
    ecs = max(sb_gb, int(mem * ev.EFFECTIVE_CACHE_FRAC))
    mpw = min(max(1, vcpus), 128)
    settings = [
        f"shared_buffers={sb_gb}GB",
        f"effective_cache_size={ecs}GB",
        "max_connections=400",
        f"max_parallel_workers={mpw}",
        f"max_worker_processes={mpw}",
        "max_parallel_workers_per_gather=2",
        "synchronous_commit=off",
        "wal_buffers=64MB",
        "max_wal_size=8GB",
        "min_wal_size=1GB",
        "checkpoint_completion_target=0.9",
        "random_page_cost=1.1",
        "effective_io_concurrency=128",
        "maintenance_work_mem=1GB",
        "listen_addresses=*",
    ]
    settings = bump_max_connections(settings, max_conn)
    docker_c = docker_c_from_settings(settings)

    storage: dict[str, object] = {
        "pg_data_storage": "anonymous_volume",
        "pg_data_dir": ev.PG_DATA_DIR,
        "pg_tmpfs_size_gib": "",
    }
    cmd_parts = [
        "docker",
        "run",
        "-d",
        *ev.DOCKER_PRIV_FLAGS,
        "--name",
        ev.PG_NAME,
        "--network",
        ev.NETWORK,
        "-e",
        f"POSTGRES_PASSWORD={ev.PG_PASSWORD}",
        "-e",
        f"POSTGRES_USER={ev.PG_USER}",
    ]
    if tmpfs:
        size_gib = (
            tmpfs_size_gib
            if tmpfs_size_gib is not None
            else ev.default_tmpfs_size_gib(mem)
        )
        cmd_parts.extend(
            ["--tmpfs", f"{ev.PG_DATA_DIR}:rw,noexec,nosuid,size={size_gib}g"]
        )
        storage = {
            "pg_data_storage": "tmpfs",
            "pg_data_dir": ev.PG_DATA_DIR,
            "pg_tmpfs_size_gib": size_gib,
        }
    cmd_parts.extend([ev.PG_IMAGE, "postgres", *docker_c])
    proc = subprocess.run(
        cmd_parts,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"postgres start failed: {proc.stderr or proc.stdout}")
    for _ in range(90):
        ready = ev.sh(
            f"docker exec {ev.PG_NAME} pg_isready -U {ev.PG_USER}",
            check=False,
        )
        if ready.returncode == 0:
            return sb_gb, ecs, storage
        time.sleep(2)
    logs = ev.sh(f"docker logs --tail 40 {ev.PG_NAME}", check=False).stdout
    raise RuntimeError(f"postgres not ready:\n{logs}")


CSV_FIELDS = [
    "run_id",
    "hostname",
    "timestamp_utc",
    "mode",
    "ncpus",
    "mem_gib",
    "run_vus",
    "n_schemas",
    "vus_per_schema",
    "warehouses_per_schema",
    "warehouses_total",
    "wh_per_vu",
    "build_vus_per_schema",
    "shared_buffers_gb",
    "effective_cache_size_gb",
    "synchronous_commit",
    "pg_data_storage",
    "pg_tmpfs_size_gib",
    "rampup_min",
    "duration_min",
    "build_seconds",
    "db_size_bytes",
    "db_size_pretty",
    "db_size_gib",
    "run_seconds",
    "nopm",
    "tpm",
    "tpm_source",
    "nopm_per_schema",
    "nopm_per_vu",
    "tpm_per_vu",
    "schema_nopms",
    "schema_tpms",
    "raw_dir",
    "error",
]


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def run_baseline(
    *,
    run_id: str,
    hostname: str,
    cpus: int,
    mem: float,
    run_vus: int,
    wh_per_vu: int,
    rampup_min: int,
    duration_min: int,
    out_dir: Path,
    pg_tmpfs: bool,
    pg_tmpfs_size_gib: int | None,
) -> dict:
    """Single-database timed run (same shape as run_wh_sizing_eval)."""
    label = f"baseline_vu{run_vus:02d}"
    raw_dir = out_dir / "raw" / label
    raw_dir.mkdir(parents=True, exist_ok=True)
    warehouses = ev.warehouses_for_vus(run_vus, wh_per_vu)
    build_vus = ev.build_vus_for(run_vus, warehouses)
    row: dict = {
        "run_id": run_id,
        "hostname": hostname,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "single_schema",
        "ncpus": cpus,
        "mem_gib": mem,
        "run_vus": run_vus,
        "n_schemas": 1,
        "vus_per_schema": run_vus,
        "warehouses_per_schema": warehouses,
        "warehouses_total": warehouses,
        "wh_per_vu": wh_per_vu,
        "build_vus_per_schema": build_vus,
        "shared_buffers_gb": "",
        "effective_cache_size_gb": "",
        "synchronous_commit": "",
        "pg_data_storage": "",
        "pg_tmpfs_size_gib": "",
        "rampup_min": rampup_min,
        "duration_min": duration_min,
        "build_seconds": "",
        "db_size_bytes": "",
        "db_size_pretty": "",
        "db_size_gib": "",
        "run_seconds": "",
        "nopm": "",
        "tpm": "",
        "tpm_source": "hammerdb",
        "nopm_per_schema": "",
        "nopm_per_vu": "",
        "tpm_per_vu": "",
        "schema_nopms": "",
        "schema_tpms": "",
        "raw_dir": str(raw_dir.relative_to(out_dir)),
        "error": "",
    }
    print(
        f"\n=== BASELINE: {run_vus} VU / {warehouses} WH single tpcc "
        f"(build_vus={build_vus}) ===",
        flush=True,
    )
    try:
        sb_gb, ecs, storage = start_postgres_with_gucs(
            mem,
            cpus,
            tmpfs=pg_tmpfs,
            tmpfs_size_gib=pg_tmpfs_size_gib,
            max_conn=400,
        )
        row["shared_buffers_gb"] = sb_gb
        row["effective_cache_size_gb"] = ecs
        row["pg_data_storage"] = storage["pg_data_storage"]
        row["pg_tmpfs_size_gib"] = storage["pg_tmpfs_size_gib"]
        row["synchronous_commit"] = ev.verify_sync_commit()
        print(
            f"postgres ready shared_buffers={sb_gb}GB "
            f"data={storage['pg_data_storage']}",
            flush=True,
        )

        build_secs = build_one_schema(
            db_name="tpcc",
            user="tpcc",
            password="tpcc",
            warehouses=warehouses,
            build_vus=build_vus,
            log_path=raw_dir / "build.log",
        )
        row["build_seconds"] = round(build_secs, 1)
        print(f"buildschema done in {build_secs:.0f}s", flush=True)

        sizes = measure_db_sizes(["tpcc"], raw_dir / "db_size.json")
        row["db_size_bytes"] = sizes["total_size_bytes"]
        row["db_size_pretty"] = sizes["total_size_pretty"]
        row["db_size_gib"] = sizes["total_size_gib"]

        result, run_secs = timed_run_one(
            db_name="tpcc",
            user="tpcc",
            password="tpcc",
            run_vus=run_vus,
            rampup_min=rampup_min,
            duration_min=duration_min,
            log_path=raw_dir / "run.log",
            container_name=f"{ev.HDB_NAME}-baseline",
        )
        row["run_seconds"] = round(run_secs, 1)
        row["nopm"] = result["nopm"]
        row["tpm"] = result["tpm"]
        row["nopm_per_schema"] = result["nopm"]
        row["nopm_per_vu"] = round(result["nopm"] / run_vus, 1)
        row["tpm_per_vu"] = round(result["tpm"] / run_vus, 1)
        row["schema_nopms"] = str([result["nopm"]])
        row["schema_tpms"] = str([result["tpm"]])
        print(
            f"BASELINE RESULT nopm={result['nopm']} tpm={result['tpm']} "
            f"({row['nopm_per_vu']} NOPM/VU)",
            flush=True,
        )
    except Exception as exc:
        row["error"] = str(exc)
        print(f"FAIL baseline: {exc}", flush=True)
    finally:
        ev.cleanup_containers()
        ev.sh(f"docker rm -f {ev.HDB_NAME}-baseline 2>/dev/null || true", check=False)
    return row


def run_multi(
    *,
    run_id: str,
    hostname: str,
    cpus: int,
    mem: float,
    run_vus: int,
    vus_per_schema: int,
    wh_per_vu: int,
    rampup_min: int,
    duration_min: int,
    out_dir: Path,
    pg_tmpfs: bool,
    pg_tmpfs_size_gib: int | None,
    db_prefix: str,
) -> dict:
    if run_vus % vus_per_schema != 0:
        raise ValueError(
            f"run_vus={run_vus} must be divisible by vus_per_schema={vus_per_schema}"
        )
    n_schemas = run_vus // vus_per_schema
    wh_per_schema = ev.warehouses_for_vus(vus_per_schema, wh_per_vu)
    build_vus = ev.build_vus_for(vus_per_schema, wh_per_schema)
    dbs = schema_names(n_schemas, db_prefix)
    label = f"multi_vu{run_vus:02d}_x{n_schemas}"
    raw_dir = out_dir / "raw" / label
    raw_dir.mkdir(parents=True, exist_ok=True)

    row: dict = {
        "run_id": run_id,
        "hostname": hostname,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "multi_schema",
        "ncpus": cpus,
        "mem_gib": mem,
        "run_vus": run_vus,
        "n_schemas": n_schemas,
        "vus_per_schema": vus_per_schema,
        "warehouses_per_schema": wh_per_schema,
        "warehouses_total": wh_per_schema * n_schemas,
        "wh_per_vu": wh_per_vu,
        "build_vus_per_schema": build_vus,
        "shared_buffers_gb": "",
        "effective_cache_size_gb": "",
        "synchronous_commit": "",
        "pg_data_storage": "",
        "pg_tmpfs_size_gib": "",
        "rampup_min": rampup_min,
        "duration_min": duration_min,
        "build_seconds": "",
        "db_size_bytes": "",
        "db_size_pretty": "",
        "db_size_gib": "",
        "run_seconds": "",
        "nopm": "",
        "tpm": "",
        "tpm_source": "median_of_clients",
        "nopm_per_schema": "",
        "nopm_per_vu": "",
        "tpm_per_vu": "",
        "schema_nopms": "",
        "schema_tpms": "",
        "raw_dir": str(raw_dir.relative_to(out_dir)),
        "error": "",
    }
    print(
        f"\n=== MULTI: {run_vus} VU across {n_schemas} DBs "
        f"({vus_per_schema} VU / {wh_per_schema} WH each) ===",
        flush=True,
    )
    try:
        # Each VU + monitor + some slack; 8 clients × (4 VU + 2) ≈ 48, keep headroom.
        max_conn = max(400, n_schemas * (vus_per_schema + 8) + 50)
        sb_gb, ecs, storage = start_postgres_with_gucs(
            mem,
            cpus,
            tmpfs=pg_tmpfs,
            tmpfs_size_gib=pg_tmpfs_size_gib,
            max_conn=max_conn,
        )
        row["shared_buffers_gb"] = sb_gb
        row["effective_cache_size_gb"] = ecs
        row["pg_data_storage"] = storage["pg_data_storage"]
        row["pg_tmpfs_size_gib"] = storage["pg_tmpfs_size_gib"]
        row["synchronous_commit"] = ev.verify_sync_commit()
        print(
            f"postgres ready shared_buffers={sb_gb}GB max_connections>={max_conn} "
            f"data={storage['pg_data_storage']}",
            flush=True,
        )

        # Shared role `tpcc` across all databases (HammerDB reuses existing role).
        build_secs_total = 0.0
        for db in dbs:
            blog = raw_dir / f"{db}_build.log"
            print(f"building {db}: {wh_per_schema} WH build_vus={build_vus} …", flush=True)
            secs = build_one_schema(
                db_name=db,
                user="tpcc",
                password="tpcc",
                warehouses=wh_per_schema,
                build_vus=build_vus,
                log_path=blog,
            )
            build_secs_total += secs
            print(f"  {db} done in {secs:.0f}s", flush=True)
        row["build_seconds"] = round(build_secs_total, 1)

        sizes = measure_db_sizes(dbs, raw_dir / "db_size.json")
        row["db_size_bytes"] = sizes["total_size_bytes"]
        row["db_size_pretty"] = sizes["total_size_pretty"]
        row["db_size_gib"] = sizes["total_size_gib"]
        print(f"total schema size: {sizes['total_size_pretty']}", flush=True)

        print(f"starting {n_schemas} parallel timed runs …", flush=True)
        t0 = time.time()
        per_schema: dict[str, dict] = {}
        errors: list[str] = []

        def _one(db: str) -> tuple[str, dict[str, int], float]:
            result, secs = timed_run_one(
                db_name=db,
                user="tpcc",
                password="tpcc",
                run_vus=vus_per_schema,
                rampup_min=rampup_min,
                duration_min=duration_min,
                log_path=raw_dir / f"{db}_run.log",
                container_name=f"{ev.HDB_NAME}-{db}",
            )
            return db, result, secs

        with ThreadPoolExecutor(max_workers=n_schemas) as pool:
            futs = [pool.submit(_one, db) for db in dbs]
            for fut in as_completed(futs):
                try:
                    db, result, secs = fut.result()
                    per_schema[db] = {**result, "run_seconds": secs}
                    print(
                        f"  {db}: nopm={result['nopm']} tpm={result['tpm']} "
                        f"({secs:.0f}s)",
                        flush=True,
                    )
                except Exception as exc:
                    errors.append(str(exc))
                    print(f"  FAIL: {exc}", flush=True)

        row["run_seconds"] = round(time.time() - t0, 1)
        if errors or len(per_schema) != n_schemas:
            raise RuntimeError(
                f"multi-run incomplete ({len(per_schema)}/{n_schemas}): "
                + "; ".join(errors)
            )

        nopms = [per_schema[db]["nopm"] for db in dbs]
        tpms = [per_schema[db]["tpm"] for db in dbs]
        nopm_sum = sum(nopms)
        tpm_med = int(statistics.median(tpms))
        row["nopm"] = nopm_sum
        row["tpm"] = tpm_med
        row["nopm_per_schema"] = round(nopm_sum / n_schemas, 1)
        row["nopm_per_vu"] = round(nopm_sum / run_vus, 1)
        row["tpm_per_vu"] = round(tpm_med / run_vus, 1)
        row["schema_nopms"] = json.dumps(dict(zip(dbs, nopms)))
        row["schema_tpms"] = json.dumps(dict(zip(dbs, tpms)))
        print(
            f"MULTI RESULT nopm_sum={nopm_sum} tpm_median={tpm_med} "
            f"({row['nopm_per_vu']} NOPM/VU) per-schema nopm={nopms}",
            flush=True,
        )
    except Exception as exc:
        row["error"] = str(exc)
        print(f"FAIL multi: {exc}", flush=True)
    finally:
        for db in dbs:
            ev.sh(f"docker rm -f {ev.HDB_NAME}-{db} 2>/dev/null || true", check=False)
        ev.cleanup_containers()
    return row


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--vus", type=int, default=32, help="total virtual users (default 32)")
    p.add_argument(
        "--vus-per-schema",
        type=int,
        default=4,
        help="VUs (and thus one HammerDB client) per database (default 4)",
    )
    p.add_argument("--wh-per-vu", type=int, default=ev.WH_PER_VU)
    p.add_argument("--rampup-min", type=int, default=ev.DEFAULT_RAMPUP_MIN)
    p.add_argument("--duration-min", type=int, default=ev.DEFAULT_DURATION_MIN)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--skip-pull", action="store_true")
    p.add_argument("--pg-tmpfs", action="store_true")
    p.add_argument("--pg-tmpfs-size-gib", type=int, default=None)
    p.add_argument(
        "--skip-baseline",
        action="store_true",
        help="only run the multi-schema configuration",
    )
    p.add_argument(
        "--skip-multi",
        action="store_true",
        help="only run the single-schema baseline",
    )
    p.add_argument("--db-prefix", default="tpcc", help="database name prefix (tpcc0..)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.vus < 1 or args.vus_per_schema < 1:
        print("vus and vus-per-schema must be >= 1", file=sys.stderr)
        return 2
    if args.vus % args.vus_per_schema != 0:
        print(
            f"--vus ({args.vus}) must be divisible by "
            f"--vus-per-schema ({args.vus_per_schema})",
            file=sys.stderr,
        )
        return 2
    if args.skip_baseline and args.skip_multi:
        print("nothing to run", file=sys.stderr)
        return 2

    cpus = ev.ncpus()
    mem = ev.mem_gib()
    hostname = socket.gethostname()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_{hostname}"
    out_dir = args.out_dir or (Path.cwd() / "results" / f"multi_{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw").mkdir(exist_ok=True)

    tmpfs_size = (
        args.pg_tmpfs_size_gib
        if args.pg_tmpfs and args.pg_tmpfs_size_gib is not None
        else (ev.default_tmpfs_size_gib(mem) if args.pg_tmpfs else None)
    )
    n_schemas = args.vus // args.vus_per_schema
    meta = {
        "run_id": run_id,
        "hostname": hostname,
        "ncpus": cpus,
        "mem_gib": mem,
        "experiment": "multi_schema_vs_baseline",
        "vus": args.vus,
        "vus_per_schema": args.vus_per_schema,
        "n_schemas": n_schemas,
        "wh_per_vu": args.wh_per_vu,
        "rampup_min": args.rampup_min,
        "duration_min": args.duration_min,
        "pg_tmpfs": args.pg_tmpfs,
        "pg_tmpfs_size_gib": tmpfs_size if args.pg_tmpfs else None,
        "pg_image": ev.PG_IMAGE,
        "hammerdb_image": ev.HDB_IMAGE,
        "policy": {
            "note": (
                "HammerDB TPROC-C 'schema' == one PostgreSQL database. "
                "Multi mode builds N databases and runs N HammerDB clients "
                "in parallel (vus_per_schema each). NOPM is summed; TPM is "
                "the median of client-reported instance-wide TPM."
            ),
            "baseline_ref_run2_tmpfs_F16_vu32": {
                "nopm": 644_697,
                "tpm": 1_482_628,
                "source": "RESULTS.md run2-tmpfs Standard_F16ams_v6 vu32",
            },
        },
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2), flush=True)

    ev.ensure_network()
    if not args.skip_pull:
        print(f"pulling {ev.PG_IMAGE} …", flush=True)
        ev.sh(f"docker pull {ev.PG_IMAGE}")
        print(f"pulling {ev.HDB_IMAGE} …", flush=True)
        ev.sh(f"docker pull {ev.HDB_IMAGE}")

    csv_path = out_dir / "results.csv"
    rows: list[dict] = []

    if not args.skip_baseline:
        rows.append(
            run_baseline(
                run_id=run_id,
                hostname=hostname,
                cpus=cpus,
                mem=mem,
                run_vus=args.vus,
                wh_per_vu=args.wh_per_vu,
                rampup_min=args.rampup_min,
                duration_min=args.duration_min,
                out_dir=out_dir,
                pg_tmpfs=args.pg_tmpfs,
                pg_tmpfs_size_gib=tmpfs_size,
            )
        )
        write_csv(csv_path, rows)

    if not args.skip_multi:
        rows.append(
            run_multi(
                run_id=run_id,
                hostname=hostname,
                cpus=cpus,
                mem=mem,
                run_vus=args.vus,
                vus_per_schema=args.vus_per_schema,
                wh_per_vu=args.wh_per_vu,
                rampup_min=args.rampup_min,
                duration_min=args.duration_min,
                out_dir=out_dir,
                pg_tmpfs=args.pg_tmpfs,
                pg_tmpfs_size_gib=tmpfs_size,
                db_prefix=args.db_prefix,
            )
        )
        write_csv(csv_path, rows)

    meta["finished_utc"] = datetime.now(timezone.utc).isoformat()
    meta["rows"] = len(rows)
    meta["ok"] = sum(1 for r in rows if not r.get("error"))
    # Quick comparison block when both modes present.
    by_mode = {r["mode"]: r for r in rows if not r.get("error") and r.get("nopm") != ""}
    if "single_schema" in by_mode and "multi_schema" in by_mode:
        b = by_mode["single_schema"]
        m = by_mode["multi_schema"]
        bn, mn = int(b["nopm"]), int(m["nopm"])
        meta["comparison"] = {
            "baseline_nopm": bn,
            "multi_nopm": mn,
            "multi_vs_baseline_pct": round(100.0 * (mn - bn) / bn, 2) if bn else None,
            "archived_f16_tmpfs_vu32_nopm": 644_697,
            "multi_vs_archived_pct": round(100.0 * (mn - 644_697) / 644_697, 2),
            "baseline_vs_archived_pct": round(100.0 * (bn - 644_697) / 644_697, 2),
        }
        print("\n=== COMPARISON ===", flush=True)
        print(json.dumps(meta["comparison"], indent=2), flush=True)
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"\nWrote {csv_path}", flush=True)
    return 0 if all(not r.get("error") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
