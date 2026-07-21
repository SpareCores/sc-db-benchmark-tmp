#!/usr/bin/env python3
"""HammerDB TPROC-C warehouse/VU sizing evaluation.

Follows https://www.hammerdb.com/docs/ch03s07.html :
  - without keying/thinking time, ~1 VU drives ~1 DB core
  - configure at least 4–5 warehouses per VU so home-warehouse
    selection stays evenly distributed (we use 5 WH/VU)

Postgres GUCs match sc-inspector multi-VM async profile for most
settings (synchronous_commit=off, WAL/SSD knobs), but shared_buffers
and effective_cache_size are fixed from host RAM (25% / 75%) across
all VU points so the ladder does not confound concurrency with cache size.
Pass --pg-tune-host to instead size GUCs from live topology via
pg_tune_gucs.py (physical vs logical CPUs, NUMA, PG18 io_workers, …).

Each VU point starts from an empty Postgres volume, buildschema
(ingest), then timed run with rampup (warmup) + measurement window.

Outputs under ./results/<run_id>/ :
  results.csv          — one row per VU configuration
  meta.json            — host / image / policy metadata
  raw/vuNN_build.log   — HammerDB buildschema stdout/stderr
  raw/vuNN_db_size.json — pg_database_size('tpcc') after buildschema
  raw/vuNN_run.log     — HammerDB timed-run stdout/stderr
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pg_tune_gucs import PgGucPlan, probe_host_topology, tune_pg_gucs

# --- defaults aligned with sc-inspector / HammerDB guidance ---------------
HDB_IMAGE = "tpcorg/hammerdb:postgres"
# Debian-based official image (has liburing / io_uring). Alpine builds often
# omit it — see https://github.com/docker-library/postgres/issues/1365
PG_IMAGE = "postgres:18"
NETWORK = "hammerdb-wh-eval-net"
PG_NAME = "pg-wh-eval"
HDB_NAME = "hammerdb-wh-eval"

# Benchmark hosts are not security-hardened: unlock io_uring, huge pages, etc.
DOCKER_PRIV_FLAGS = (
    "--privileged",
    "--security-opt",
    "seccomp=unconfined",
    "--ulimit",
    "memlock=-1:-1",
)

WH_PER_VU = 5  # HammerDB docs: 4–5 WH/VU minimum
BUILD_VU_CAP = 64
DEFAULT_VUS = (4, 8, 16, 32, 64)
DEFAULT_RAMPUP_MIN = 2
DEFAULT_DURATION_MIN = 5
SHARED_BUFFERS_FRAC = 0.25  # Postgres docs starting point for dedicated DB hosts
EFFECTIVE_CACHE_FRAC = 0.75

PG_USER = "postgres"
PG_PASSWORD = "postgres"
PG_ADMIN_DB = "postgres"
# postgres:18+ image VOLUME path (older tags used /var/lib/postgresql/data)
PG_DATA_DIR = "/var/lib/postgresql"


def sh(cmd: str, *, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=timeout,
    )


def ncpus() -> int:
    return int(subprocess.check_output(["nproc"], text=True).strip())


def mem_gib() -> float:
    kb = int(
        subprocess.check_output(
            ["awk", "/MemTotal/ {print $2}", "/proc/meminfo"], text=True
        ).strip()
    )
    return round(kb / 1024 / 1024, 1)


def pg_gucs(mem: float, vcpus: int) -> tuple[list[str], int, int]:
    """Async OLTP GUCs with RAM-fixed buffer cache (constant across VU points).

    Unlike sc-inspector postgres_multi.pg_gucs, shared_buffers is not capped to
    schema size — that would grow buffers with WH and confound the VU ladder.
    """
    sb_gb = max(1, int(mem * SHARED_BUFFERS_FRAC))
    ecs = max(sb_gb, int(mem * EFFECTIVE_CACHE_FRAC))
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
    args: list[str] = []
    for s in settings:
        args.extend(["-c", s])
    return args, sb_gb, ecs


def warehouses_for_vus(run_vus: int, wh_per_vu: int) -> int:
    return max(run_vus, run_vus * wh_per_vu)


def build_vus_for(run_vus: int, warehouses: int) -> int:
    """Parallel schema-build VUs — same absolute count on every SKU.

    Tied to the timed-run VU ladder (not host ncpus) so buildschema times
    are comparable across machines at the same warehouse / run_vus point.
    """
    return max(1, min(run_vus, warehouses, BUILD_VU_CAP))


def ensure_network() -> None:
    sh(f"docker network create {NETWORK} 2>/dev/null || true", check=False)


def cleanup_containers() -> None:
    sh(f"docker rm -f {PG_NAME} {HDB_NAME} 2>/dev/null || true", check=False)


def default_tmpfs_size_gib(mem: float) -> int:
    """Half of host RAM, at least 16 GiB — room for schema + WAL beside shared_buffers."""
    return max(16, int(mem * 0.5))


def start_postgres(
    mem: float,
    vcpus: int,
    *,
    tmpfs: bool = False,
    tmpfs_size_gib: int | None = None,
    tune_host: bool = False,
    guc_plan: PgGucPlan | None = None,
) -> tuple[int, int, dict[str, object], PgGucPlan | None]:
    cleanup_containers()
    # Drop anonymous volumes from prior rounds so the DB is truly empty.
    sh("docker volume prune -f >/dev/null 2>&1 || true", check=False)
    plan: PgGucPlan | None = None
    if tune_host:
        plan = guc_plan or tune_pg_gucs(
            mem_gib=mem,
            workload="oltp",
            storage="tmpfs" if tmpfs else "ssd",
            prefer_io_uring=True,
        )
        gucs = plan.docker_c_args
        sb_gb = plan.shared_buffers_gb
        ecs = plan.effective_cache_size_gb
    else:
        gucs, sb_gb, ecs = pg_gucs(mem, vcpus)
    storage: dict[str, object] = {
        "pg_data_storage": "anonymous_volume",
        "pg_data_dir": PG_DATA_DIR,
        "pg_tmpfs_size_gib": "",
    }
    cmd_parts = [
        "docker",
        "run",
        "-d",
        *DOCKER_PRIV_FLAGS,
        "--name",
        PG_NAME,
        "--network",
        NETWORK,
        "-e",
        f"POSTGRES_PASSWORD={PG_PASSWORD}",
        "-e",
        f"POSTGRES_USER={PG_USER}",
    ]
    if tmpfs:
        size_gib = tmpfs_size_gib if tmpfs_size_gib is not None else default_tmpfs_size_gib(mem)
        # Explicit tmpfs overrides the image VOLUME so data stays in RAM.
        cmd_parts.extend(
            ["--tmpfs", f"{PG_DATA_DIR}:rw,noexec,nosuid,size={size_gib}g"]
        )
        storage = {
            "pg_data_storage": "tmpfs",
            "pg_data_dir": PG_DATA_DIR,
            "pg_tmpfs_size_gib": size_gib,
        }
    cmd_parts.extend([PG_IMAGE, "postgres", *gucs])
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
    # Official postgres image: on a fresh volume the entrypoint initdb's a
    # temporary server that answers pg_isready, then fast-shutdowns and
    # starts the real postmaster. Require consecutive successful queries so
    # we do not hand callers a socket that is about to disappear.
    stable = 0
    deadline = time.time() + 180
    while time.time() < deadline:
        ready = sh(
            f"docker exec {PG_NAME} pg_isready -U {PG_USER}",
            check=False,
        )
        if ready.returncode != 0:
            stable = 0
            time.sleep(1)
            continue
        show = sh(
            f"docker exec -e PGPASSWORD={PG_PASSWORD} {PG_NAME} "
            f"psql -U {PG_USER} -d {PG_ADMIN_DB} -tAc 'SHOW synchronous_commit'",
            check=False,
        )
        if show.returncode == 0 and (show.stdout or "").strip():
            stable += 1
            if stable >= 3:
                return sb_gb, ecs, storage, plan
        else:
            stable = 0
        time.sleep(1)
    logs = sh(f"docker logs --tail 60 {PG_NAME}", check=False).stdout
    raise RuntimeError(f"postgres not stably ready:\n{logs}")


def hammerdb_run(tcl_body: str, log_path: Path, timeout: int) -> tuple[str, int]:
    tcl_path = log_path.with_suffix(".tcl")
    tcl_path.write_text(tcl_body)
    sh(f"docker rm -f {HDB_NAME} 2>/dev/null || true", check=False)
    priv = " ".join(DOCKER_PRIV_FLAGS)
    proc = sh(
        f"docker run --rm {priv} --name {HDB_NAME} --network {NETWORK} "
        f"-v {tcl_path}:/tmp/script.tcl:ro {HDB_IMAGE} "
        f"bash -lc 'cd /home/hammerdb && ./hammerdbcli auto /tmp/script.tcl'",
        check=False,
        timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(out)
    return out, proc.returncode


def build_schema(
    warehouses: int,
    build_vus: int,
    log_path: Path,
) -> float:
    partition = "true" if warehouses >= 200 else "false"
    tcl = f"""
dbset db pg
dbset bm TPC-C
vuset logtotemp 0
diset connection pg_host {PG_NAME}
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser {PG_USER}
diset tpcc pg_superuserpass {PG_PASSWORD}
diset tpcc pg_defaultdbase {PG_ADMIN_DB}
diset tpcc pg_storedprocs true
diset tpcc pg_partition {partition}
diset tpcc pg_count_ware {warehouses}
diset tpcc pg_num_vu {build_vus}
buildschema
"""
    t0 = time.time()
    out, rc = hammerdb_run(tcl, log_path, timeout=28800)
    elapsed = time.time() - t0
    if "TPCC SCHEMA COMPLETE" not in out:
        raise RuntimeError(f"buildschema failed rc={rc}: see {log_path}")
    return elapsed


def timed_run(
    run_vus: int,
    rampup_min: int,
    duration_min: int,
    log_path: Path,
) -> tuple[dict[str, int], float]:
    tcl = f"""
dbset db pg
dbset bm TPC-C
vuset logtotemp 1
vuset unique 1
diset connection pg_host {PG_NAME}
diset connection pg_port 5432
diset connection pg_sslmode disable
diset tpcc pg_superuser {PG_USER}
diset tpcc pg_superuserpass {PG_PASSWORD}
diset tpcc pg_defaultdbase tpcc
diset tpcc pg_user tpcc
diset tpcc pg_pass tpcc
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
    t0 = time.time()
    out, rc = hammerdb_run(tcl, log_path, timeout=7200)
    elapsed = time.time() - t0
    match = re.search(
        r"TEST RESULT : System achieved (\d+) NOPM from (\d+) PostgreSQL TPM",
        out,
    )
    if not match:
        raise RuntimeError(f"no TEST RESULT rc={rc}: see {log_path}")
    return {"nopm": int(match.group(1)), "tpm": int(match.group(2))}, elapsed


def verify_sync_commit() -> str:
    proc = sh(
        f"docker exec -e PGPASSWORD={PG_PASSWORD} {PG_NAME} "
        f"psql -U {PG_USER} -d {PG_ADMIN_DB} -tAc 'SHOW synchronous_commit'",
        check=False,
    )
    return (proc.stdout or "").strip() or "unknown"


def measure_tpcc_db_size(raw_path: Path) -> dict[str, object]:
    """Return tpcc database size after buildschema; also write raw/JSON snapshot."""
    sql = (
        "SELECT pg_database_size('tpcc') AS bytes, "
        "pg_size_pretty(pg_database_size('tpcc')) AS pretty"
    )
    proc = sh(
        f"docker exec -e PGPASSWORD={PG_PASSWORD} {PG_NAME} "
        f"psql -U {PG_USER} -d {PG_ADMIN_DB} -tAc \"{sql}\"",
        check=False,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or "|" not in out:
        detail = out or (proc.stderr or "").strip() or f"rc={proc.returncode}"
        raise RuntimeError(f"pg_database_size(tpcc) failed: {detail}")
    bytes_s, pretty = (part.strip() for part in out.split("|", 1))
    size_bytes = int(bytes_s)
    info = {
        "database": "tpcc",
        "size_bytes": size_bytes,
        "size_pretty": pretty,
        "size_gib": round(size_bytes / (1024**3), 4),
        "size_mib": round(size_bytes / (1024**2), 2),
    }
    raw_path.write_text(json.dumps(info, indent=2) + "\n")
    return info


CSV_FIELDS = [
    "run_id",
    "hostname",
    "timestamp_utc",
    "ncpus",
    "mem_gib",
    "run_vus",
    "warehouses",
    "wh_per_vu",
    "wh_per_vu_configured",
    "build_vus",
    "shared_buffers_gb",
    "effective_cache_size_gb",
    "synchronous_commit",
    "pg_data_storage",
    "pg_tmpfs_size_gib",
    "rampup_min",
    "duration_min",
    "pg_image",
    "hammerdb_image",
    "build_seconds",
    "db_size_bytes",
    "db_size_pretty",
    "db_size_gib",
    "run_seconds",
    "nopm",
    "tpm",
    "nopm_per_vu",
    "tpm_per_vu",
    "nopm_per_core",
    "tpm_per_core",
    "raw_build_log",
    "raw_db_size",
    "raw_run_log",
    "error",
]


def run_one(
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
    pg_tmpfs: bool = False,
    pg_tmpfs_size_gib: int | None = None,
    tune_host: bool = False,
    guc_plan: PgGucPlan | None = None,
    skip_raw: bool = False,
) -> dict:
    warehouses = warehouses_for_vus(run_vus, wh_per_vu)
    build_vus = build_vus_for(run_vus, warehouses)
    label = f"vu{run_vus:02d}"
    tmp_raw: tempfile.TemporaryDirectory[str] | None = None
    if skip_raw:
        tmp_raw = tempfile.TemporaryDirectory(prefix="wh-eval-raw-")
        raw_dir = Path(tmp_raw.name)
    else:
        raw_dir = out_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
    build_log = raw_dir / f"{label}_build.log"
    db_size_log = raw_dir / f"{label}_db_size.json"
    run_log = raw_dir / f"{label}_run.log"
    row = {
        "run_id": run_id,
        "hostname": hostname,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "ncpus": cpus,
        "mem_gib": mem,
        "run_vus": run_vus,
        "warehouses": warehouses,
        "wh_per_vu": round(warehouses / run_vus, 2),
        "wh_per_vu_configured": wh_per_vu,
        "build_vus": build_vus,
        "shared_buffers_gb": "",
        "effective_cache_size_gb": "",
        "synchronous_commit": "",
        "pg_data_storage": "tmpfs" if pg_tmpfs else "anonymous_volume",
        "pg_tmpfs_size_gib": "",
        "rampup_min": rampup_min,
        "duration_min": duration_min,
        "pg_image": PG_IMAGE,
        "hammerdb_image": HDB_IMAGE,
        "build_seconds": "",
        "db_size_bytes": "",
        "db_size_pretty": "",
        "db_size_gib": "",
        "run_seconds": "",
        "nopm": "",
        "tpm": "",
        "nopm_per_vu": "",
        "tpm_per_vu": "",
        "nopm_per_core": "",
        "tpm_per_core": "",
        "raw_build_log": "" if skip_raw else str(build_log.relative_to(out_dir)),
        "raw_db_size": "" if skip_raw else str(db_size_log.relative_to(out_dir)),
        "raw_run_log": "" if skip_raw else str(run_log.relative_to(out_dir)),
        "error": "",
    }
    print(
        f"\n=== {label}: {run_vus} VU / {warehouses} WH "
        f"(build_vus={build_vus}, rampup={rampup_min}m, duration={duration_min}m) ===",
        flush=True,
    )
    try:
        sb_gb, ecs, storage, used_plan = start_postgres(
            mem,
            cpus,
            tmpfs=pg_tmpfs,
            tmpfs_size_gib=pg_tmpfs_size_gib,
            tune_host=tune_host,
            guc_plan=guc_plan,
        )
        row["shared_buffers_gb"] = sb_gb
        row["effective_cache_size_gb"] = ecs
        row["pg_data_storage"] = storage["pg_data_storage"]
        row["pg_tmpfs_size_gib"] = storage["pg_tmpfs_size_gib"]
        sync = verify_sync_commit()
        row["synchronous_commit"] = sync
        storage_msg = storage["pg_data_storage"]
        if storage["pg_tmpfs_size_gib"]:
            storage_msg = f"tmpfs {storage['pg_tmpfs_size_gib']}GiB"
        tune_msg = "host-tuned" if used_plan else "baseline"
        print(
            f"postgres ready shared_buffers={sb_gb}GB sync_commit={sync} "
            f"data={storage_msg} gucs={tune_msg}",
            flush=True,
        )

        build_secs = build_schema(warehouses, build_vus, build_log)
        row["build_seconds"] = round(build_secs, 1)
        print(f"buildschema done in {build_secs:.0f}s", flush=True)

        db_size = measure_tpcc_db_size(db_size_log)
        row["db_size_bytes"] = db_size["size_bytes"]
        row["db_size_pretty"] = db_size["size_pretty"]
        row["db_size_gib"] = db_size["size_gib"]
        print(
            f"tpcc db size: {db_size['size_pretty']} "
            f"({db_size['size_bytes']} bytes, {db_size['size_gib']} GiB)",
            flush=True,
        )

        result, run_secs = timed_run(run_vus, rampup_min, duration_min, run_log)
        row["run_seconds"] = round(run_secs, 1)
        row["nopm"] = result["nopm"]
        row["tpm"] = result["tpm"]
        # HammerDB (no keying/thinking time): ~1 VU drives ~1 DB core, so
        # normalize by run_vus — not host ncpus (which understates under-subscribed runs).
        row["nopm_per_vu"] = round(result["nopm"] / run_vus, 1)
        row["tpm_per_vu"] = round(result["tpm"] / run_vus, 1)
        row["nopm_per_core"] = row["nopm_per_vu"]
        row["tpm_per_core"] = row["tpm_per_vu"]
        print(
            f"RESULT nopm={result['nopm']} tpm={result['tpm']} "
            f"({row['nopm_per_vu']} NOPM/VU, {row['tpm_per_vu']} TPM/VU; "
            f"VU≈core)",
            flush=True,
        )
    except Exception as exc:
        row["error"] = str(exc)
        print(f"FAIL {label}: {exc}", flush=True)
    finally:
        cleanup_containers()
    return row


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--vus",
        default=",".join(str(v) for v in DEFAULT_VUS),
        help=f"comma-separated VU counts (default: {','.join(map(str, DEFAULT_VUS))})",
    )
    p.add_argument(
        "--wh-per-vu",
        type=int,
        default=WH_PER_VU,
        help=f"warehouses per VU (HammerDB docs recommend 4–5; default {WH_PER_VU})",
    )
    p.add_argument("--rampup-min", type=int, default=DEFAULT_RAMPUP_MIN)
    p.add_argument("--duration-min", type=int, default=DEFAULT_DURATION_MIN)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="output directory (default: ./results/<run_id>)",
    )
    p.add_argument("--skip-pull", action="store_true", help="do not docker pull images")
    p.add_argument(
        "--pg-tmpfs",
        action="store_true",
        help=(
            f"mount Postgres data ({PG_DATA_DIR}) on tmpfs instead of an anonymous volume"
        ),
    )
    p.add_argument(
        "--pg-tmpfs-size-gib",
        type=int,
        default=None,
        help="tmpfs size in GiB when --pg-tmpfs is set (default: max(16, 50%% of host RAM))",
    )
    p.add_argument(
        "--pg-tune-host",
        action="store_true",
        help=(
            "size Postgres GUCs from live host topology (physical vs logical "
            "CPUs, NUMA, RAM, PG18 io_workers, …) via pg_tune_gucs.py; "
            "default is the existing sc-inspector async ladder profile"
        ),
    )
    p.add_argument(
        "--skip-raw",
        action="store_true",
        help="do not persist raw/ logs (results.csv + meta.json only)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    vu_list = [int(x.strip()) for x in args.vus.split(",") if x.strip()]
    if not vu_list:
        print("no VU counts given", file=sys.stderr)
        return 2
    if args.wh_per_vu < 4:
        print(
            "warning: HammerDB docs recommend >=4 warehouses/VU; "
            f"got {args.wh_per_vu}",
            file=sys.stderr,
        )
    if args.pg_tmpfs_size_gib is not None and not args.pg_tmpfs:
        print("warning: --pg-tmpfs-size-gib ignored without --pg-tmpfs", file=sys.stderr)
    if args.pg_tmpfs_size_gib is not None and args.pg_tmpfs_size_gib < 1:
        print("--pg-tmpfs-size-gib must be >= 1", file=sys.stderr)
        return 2

    cpus = ncpus()
    mem = mem_gib()
    hostname = socket.gethostname()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_{hostname}"
    out_dir = args.out_dir or (Path.cwd() / "results" / run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_raw:
        (out_dir / "raw").mkdir(exist_ok=True)

    tmpfs_size = (
        args.pg_tmpfs_size_gib
        if args.pg_tmpfs and args.pg_tmpfs_size_gib is not None
        else (default_tmpfs_size_gib(mem) if args.pg_tmpfs else None)
    )
    guc_plan: PgGucPlan | None = None
    if args.pg_tune_host:
        guc_plan = tune_pg_gucs(
            mem_gib=mem,
            workload="oltp",
            storage="tmpfs" if args.pg_tmpfs else "ssd",
            prefer_io_uring=True,
        )
        guc_policy = (
            "host-tuned via pg_tune_gucs.tune_pg_gucs "
            "(physical/logical CPUs, NUMA, RAM, PG18 io_workers; "
            "fixed across VU points)"
        )
    else:
        guc_policy = (
            "sc-inspector postgres_multi async profile, except "
            f"shared_buffers={int(SHARED_BUFFERS_FRAC*100)}% RAM and "
            f"effective_cache_size={int(EFFECTIVE_CACHE_FRAC*100)}% RAM "
            "(fixed across VU points)"
        )
    meta = {
        "run_id": run_id,
        "hostname": hostname,
        "ncpus": cpus,
        "mem_gib": mem,
        "pg_image": PG_IMAGE,
        "hammerdb_image": HDB_IMAGE,
        "wh_per_vu": args.wh_per_vu,
        "vus": vu_list,
        "rampup_min": args.rampup_min,
        "duration_min": args.duration_min,
        "pg_tmpfs": args.pg_tmpfs,
        "pg_tmpfs_size_gib": tmpfs_size if args.pg_tmpfs else None,
        "pg_tune_host": args.pg_tune_host,
        "skip_raw": args.skip_raw,
        "host_topology": probe_host_topology(mem_gib=mem).to_dict(),
        "pg_guc_plan": guc_plan.to_dict() if guc_plan else None,
        "policy": {
            "synchronous_commit": "off",
            "gucs": guc_policy,
            "pg_data_storage": (
                f"tmpfs {tmpfs_size}GiB on {PG_DATA_DIR}"
                if args.pg_tmpfs
                else f"anonymous docker volume ({PG_DATA_DIR})"
            ),
            "wh_sizing": (
                "https://www.hammerdb.com/docs/ch03s07.html "
                f"({args.wh_per_vu} warehouses per VU)"
            ),
            "build_vus": (
                "same as run_vus (capped by warehouses and "
                f"{BUILD_VU_CAP}), independent of host ncpus"
            ),
            "empty_db_each_round": True,
            "warmup": f"{args.rampup_min} min HammerDB rampup before measurement",
        },
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2), flush=True)

    ensure_network()
    if not args.skip_pull:
        print(f"pulling {PG_IMAGE} …", flush=True)
        sh(f"docker pull {PG_IMAGE}")
        print(f"pulling {HDB_IMAGE} …", flush=True)
        sh(f"docker pull {HDB_IMAGE}")

    csv_path = out_dir / "results.csv"
    rows: list[dict] = []
    for run_vus in vu_list:
        row = run_one(
            run_id=run_id,
            hostname=hostname,
            cpus=cpus,
            mem=mem,
            run_vus=run_vus,
            wh_per_vu=args.wh_per_vu,
            rampup_min=args.rampup_min,
            duration_min=args.duration_min,
            out_dir=out_dir,
            pg_tmpfs=args.pg_tmpfs,
            pg_tmpfs_size_gib=tmpfs_size,
            tune_host=args.pg_tune_host,
            guc_plan=guc_plan,
            skip_raw=args.skip_raw,
        )
        rows.append(row)
        write_csv(csv_path, rows)  # incremental flush

    meta["finished_utc"] = datetime.now(timezone.utc).isoformat()
    meta["rows"] = len(rows)
    meta["ok"] = sum(1 for r in rows if not r.get("error"))
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    cleanup_containers()
    print(f"\nWrote {csv_path}", flush=True)
    if not args.skip_raw:
        print(f"Raw logs under {out_dir / 'raw'}", flush=True)
    return 0 if all(not r.get("error") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
