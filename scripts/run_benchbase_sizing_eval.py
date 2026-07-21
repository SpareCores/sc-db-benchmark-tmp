#!/usr/bin/env python3
"""BenchBase Wikipedia / YCSB concurrency sizing evaluation.

Mirrors run_wh_sizing_eval.py: same Postgres GUCs / tmpfs options, same
VU ladder (default 4 / 8 / 16 / 32 / 64), fresh empty DB each rung, then
load + timed run with warmup + measurement.

Scale factors are chosen so the working-set size at each rung matches the
HammerDB TPROC-C warehouse footprint for the same VU count (5 WH/VU):

  warehouses = run_vus * wh_per_vu
  target_gib ≈ warehouses * 0.095
  wikipedia_sf = round(target_gib / (14.803/100))   # measured PG18
  ycsb_sf      = round(target_gib / (1.189/1024))   # ~1 KiB/row * 1000

Terminals = run_vus (same concurrency steps as HammerDB). BenchBase's
inspector soft cap (SF//5) is not applied here so the ladder stays aligned;
Wikipedia SF is below 5×VU at the small end — see meta.json.

Workload mixes match sc-images benchmark-benchbase-postgres:
  wikipedia: AddWatchList,RemoveWatchList,UpdatePage,GetPageAnonymous,
             GetPageAuthenticated weights 1,1,7,90,1
  ycsb:      ReadRecord,UpdateRecord weights 50,50

Outputs under ./results/<run_id>/ :
  results.csv
  meta.json
  raw/<workload>_vuNN_{load,run}.log
  raw/<workload>_vuNN_db_size.json
  raw/<workload>_vuNN_config.xml
  raw/<workload>_vuNN_summary.json
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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.dom import minidom

import run_wh_sizing_eval as ev
from pg_tune_gucs import PgGucPlan, probe_host_topology, tune_pg_gucs

# --- images / containers -------------------------------------------------
BB_IMAGE = "benchbase.azurecr.io/benchbase-postgres:latest"
NETWORK = "benchbase-sf-eval-net"
PG_NAME = "pg-bb-eval"
BB_NAME = "benchbase-sf-eval"
BENCH_DB = "benchbase"
BB_WORKDIR = "/benchbase/profiles/postgres"
BB_JAR = f"{BB_WORKDIR}/benchbase.jar"
BB_JAVA = "/opt/java/openjdk/bin/java"

# Schema-size constants from sc-inspector benchmark_tiers.py (PG18 measured).
WH_SIZE_GIB = 0.095
WIKIPEDIA_GIB_PER_SF = 14.803 / 100
YCSB_ROW_KIB = 1.189
WH_PER_VU = 5
UNITS_PER_VU_MIN = 5  # inspector soft cap; documented only

DEFAULT_VUS = (4, 8, 16, 32, 64)
DEFAULT_WORKLOADS = ("wikipedia", "ycsb")
DEFAULT_RAMPUP_MIN = 2
DEFAULT_DURATION_MIN = 5

WORKLOADS: dict[str, dict] = {
    "wikipedia": {
        "txn_types": (
            "AddWatchList",
            "RemoveWatchList",
            "UpdatePage",
            "GetPageAnonymous",
            "GetPageAuthenticated",
        ),
        "weights": "1,1,7,90,1",
    },
    "ycsb": {
        "txn_types": ("ReadRecord", "UpdateRecord"),
        "weights": "50,50",
    },
}


def warehouses_for_vus(run_vus: int, wh_per_vu: int) -> int:
    return max(run_vus, run_vus * wh_per_vu)


def target_schema_gib(warehouses: int) -> float:
    return warehouses * WH_SIZE_GIB


def scalefactor_for(workload: str, warehouses: int) -> int:
    """SF so BenchBase schema ≈ HammerDB WH working set at this rung."""
    target = target_schema_gib(warehouses)
    if workload == "wikipedia":
        return max(1, int(round(target / WIKIPEDIA_GIB_PER_SF)))
    if workload == "ycsb":
        # YCSB SF unit = RECORD_COUNT (1000) rows ≈ YCSB_ROW_KIB KiB each.
        gib_per_sf = (1000 * YCSB_ROW_KIB) / (1024 * 1024)
        return max(1, int(round(target / gib_per_sf)))
    raise ValueError(workload)


def expected_schema_gib(workload: str, scalefactor: int) -> float:
    if workload == "wikipedia":
        return scalefactor * WIKIPEDIA_GIB_PER_SF
    if workload == "ycsb":
        return (scalefactor * 1000) * YCSB_ROW_KIB / (1024 * 1024)
    raise ValueError(workload)


def bind_eval_names() -> None:
    """Point shared postgres helpers at this harness's container names."""
    ev.NETWORK = NETWORK
    ev.PG_NAME = PG_NAME
    ev.HDB_NAME = BB_NAME


def ensure_bench_db() -> None:
    """Create the BenchBase database, retrying through brief postmaster flaps."""
    sql = f"SELECT 1 FROM pg_database WHERE datname = '{BENCH_DB}'"
    last_detail = ""
    for attempt in range(1, 11):
        exists = ev.sh(
            f"docker exec -e PGPASSWORD={ev.PG_PASSWORD} {PG_NAME} "
            f"psql -U {ev.PG_USER} -d {ev.PG_ADMIN_DB} -tAc \"{sql}\"",
            check=False,
        )
        if (exists.stdout or "").strip() == "1":
            return
        proc = ev.sh(
            f"docker exec -e PGPASSWORD={ev.PG_PASSWORD} {PG_NAME} "
            f"psql -U {ev.PG_USER} -d {ev.PG_ADMIN_DB} "
            f"-c \"CREATE DATABASE {BENCH_DB}\"",
            check=False,
        )
        if proc.returncode == 0:
            return
        last_detail = (proc.stderr or proc.stdout or "").strip()
        time.sleep(min(2.0, 0.3 * attempt))
    raise RuntimeError(f"CREATE DATABASE {BENCH_DB} failed: {last_detail}")


def write_config(
    path: Path,
    *,
    workload: str,
    scalefactor: int,
    terminals: int,
    warmup_sec: int,
    duration_sec: int,
) -> None:
    spec = WORKLOADS[workload]
    root = ET.Element("parameters")
    url = (
        f"jdbc:postgresql://{PG_NAME}:5432/{BENCH_DB}"
        f"?sslmode=disable&ApplicationName={workload}&reWriteBatchedInserts=true"
    )
    fields = {
        "type": "POSTGRES",
        "driver": "org.postgresql.Driver",
        "url": url,
        "username": ev.PG_USER,
        "password": ev.PG_PASSWORD,
        "reconnectOnConnectionFailure": "true",
        "isolation": "TRANSACTION_READ_COMMITTED",
        "batchsize": "128",
        "scalefactor": str(scalefactor),
        "terminals": str(terminals),
    }
    for key, value in fields.items():
        elem = ET.SubElement(root, key)
        elem.text = value

    works = ET.SubElement(root, "works")
    work = ET.SubElement(works, "work")
    for key, value in (
        ("warmup", str(warmup_sec)),
        ("time", str(duration_sec)),
        ("rate", "unlimited"),
        ("weights", spec["weights"]),
    ):
        elem = ET.SubElement(work, key)
        elem.text = value

    tx_types = ET.SubElement(root, "transactiontypes")
    for name in spec["txn_types"]:
        tx_type = ET.SubElement(tx_types, "transactiontype")
        tx_name = ET.SubElement(tx_type, "name")
        tx_name.text = name

    xml_text = minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(
        indent="    "
    )
    path.write_text(xml_text, encoding="utf-8")


def benchbase_run(
    *,
    config_path: Path,
    results_dir: Path,
    workload: str,
    extra_args: list[str],
    log_path: Path,
    timeout: int,
) -> tuple[str, int]:
    results_dir.mkdir(parents=True, exist_ok=True)
    # Image USER is containeruser; host-created dirs are often root/ubuntu-owned.
    results_dir.chmod(0o777)
    ev.sh(f"docker rm -f {BB_NAME} 2>/dev/null || true", check=False)
    priv = " ".join(ev.DOCKER_PRIV_FLAGS)
    # Mount config + results; run jar from the image profile directory.
    # Force root so result files are writable regardless of mount ownership.
    cmd = (
        f"docker run --rm {priv} --name {BB_NAME} --network {NETWORK} "
        f"-u 0:0 -w {BB_WORKDIR} "
        f"-v {config_path}:/tmp/bb_config.xml:ro "
        f"-v {results_dir}:/tmp/bb_results "
        f"--entrypoint {BB_JAVA} {BB_IMAGE} "
        f"-jar {BB_JAR} -b {workload} -c /tmp/bb_config.xml "
        f"-d /tmp/bb_results {' '.join(extra_args)}"
    )
    proc = ev.sh(cmd, check=False, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(out)
    return out, proc.returncode


def latest_summary(results_dir: Path, workload: str) -> tuple[dict, Path] | None:
    files = sorted(
        results_dir.glob(f"{workload}_*.summary.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8")), files[-1]


# BenchBase prints this even when writing result files fails afterward.
_THROUGHPUT_RE = re.compile(
    r"=\s*([0-9.]+)\s+requests/sec\s+\(throughput\).*?"
    r"([0-9.]+)\s+requests/sec\s+\(goodput\)",
    re.IGNORECASE,
)


def parse_throughput_from_log(out: str) -> dict | None:
    match = _THROUGHPUT_RE.search(out)
    if not match:
        return None
    tps = float(match.group(1))
    goodput = float(match.group(2))
    return {
        "tps": round(tps, 2),
        "tpm": int(round(tps * 60)),
        "goodput_tps": round(goodput, 2),
        "measured_requests": "",
    }


def load_schema(
    *,
    workload: str,
    scalefactor: int,
    config_path: Path,
    results_dir: Path,
    log_path: Path,
) -> float:
    write_config(
        config_path,
        workload=workload,
        scalefactor=scalefactor,
        terminals=1,
        warmup_sec=0,
        duration_sec=10,
    )
    t0 = time.time()
    out, rc = benchbase_run(
        config_path=config_path,
        results_dir=results_dir,
        workload=workload,
        extra_args=["--create=true", "--load=true", "--execute=false"],
        log_path=log_path,
        timeout=28800,
    )
    elapsed = time.time() - t0
    # BenchBase prints "Data loaded" / "Finished" on success; exceptions otherwise.
    if rc != 0 or ("Exception" in out and "Data loaded" not in out and "Finished" not in out):
        raise RuntimeError(f"benchbase load failed rc={rc}: see {log_path}")
    return elapsed


def timed_run(
    *,
    workload: str,
    scalefactor: int,
    terminals: int,
    rampup_min: int,
    duration_min: int,
    config_path: Path,
    results_dir: Path,
    log_path: Path,
    summary_copy: Path,
) -> tuple[dict, float]:
    write_config(
        config_path,
        workload=workload,
        scalefactor=scalefactor,
        terminals=terminals,
        warmup_sec=rampup_min * 60,
        duration_sec=duration_min * 60,
    )
    # Clear prior summaries so we pick the fresh one.
    for old in results_dir.glob(f"{workload}_*.summary.json"):
        old.unlink(missing_ok=True)

    t0 = time.time()
    out, rc = benchbase_run(
        config_path=config_path,
        results_dir=results_dir,
        workload=workload,
        extra_args=["--create=false", "--load=false", "--execute=true"],
        log_path=log_path,
        timeout=(rampup_min + duration_min) * 60 + 900,
    )
    elapsed = time.time() - t0
    parsed = latest_summary(results_dir, workload)
    if parsed is not None:
        summary, src = parsed
        shutil.copy2(src, summary_copy)
        tps = float(summary.get("Throughput (requests/second)", 0))
        result: dict = {
            "tps": round(tps, 2),
            "tpm": int(round(tps * 60)),
            "measured_requests": summary.get("Measured Requests", ""),
            "goodput_tps": round(
                float(summary.get("Goodput (requests/second)", 0) or 0), 2
            ),
        }
        dist = summary.get("Latency Distribution")
        if isinstance(dist, dict):

            def us_to_ms(key: str) -> float | None:
                if key not in dist:
                    return None
                return round(float(dist[key]) / 1000.0, 3)

            latency = {
                "p50_ms": us_to_ms("Median Latency (microseconds)"),
                "p95_ms": us_to_ms("95th Percentile Latency (microseconds)"),
                "p99_ms": us_to_ms("99th Percentile Latency (microseconds)"),
                "avg_ms": us_to_ms("Average Latency (microseconds)"),
            }
            result.update({k: v for k, v in latency.items() if v is not None})
        return result, elapsed

    # Fallback: run finished but result files were not writable (old bug).
    fallback = parse_throughput_from_log(out)
    if fallback is not None:
        summary_copy.write_text(
            json.dumps({"source": "run_log_fallback", **fallback}, indent=2) + "\n"
        )
        if rc != 0:
            print(
                f"warning: benchbase rc={rc} but throughput parsed from log "
                f"(no summary JSON; see {log_path})",
                flush=True,
            )
        return fallback, elapsed

    raise RuntimeError(f"benchbase execute failed rc={rc}: see {log_path}")


def measure_db_size(raw_path: Path) -> dict[str, object]:
    sql = (
        f"SELECT pg_database_size('{BENCH_DB}') AS bytes, "
        f"pg_size_pretty(pg_database_size('{BENCH_DB}')) AS pretty"
    )
    proc = ev.sh(
        f"docker exec -e PGPASSWORD={ev.PG_PASSWORD} {PG_NAME} "
        f"psql -U {ev.PG_USER} -d {ev.PG_ADMIN_DB} -tAc \"{sql}\"",
        check=False,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or "|" not in out:
        detail = out or (proc.stderr or "").strip() or f"rc={proc.returncode}"
        raise RuntimeError(f"pg_database_size({BENCH_DB}) failed: {detail}")
    bytes_s, pretty = (part.strip() for part in out.split("|", 1))
    size_bytes = int(bytes_s)
    info = {
        "database": BENCH_DB,
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
    "workload",
    "run_vus",
    "terminals",
    "warehouses_equiv",
    "wh_per_vu",
    "scalefactor",
    "sf_per_vu",
    "expected_schema_gib",
    "units_per_vu_min",
    "sf_covers_vu_min",
    "shared_buffers_gb",
    "effective_cache_size_gb",
    "synchronous_commit",
    "pg_data_storage",
    "pg_tmpfs_size_gib",
    "rampup_min",
    "duration_min",
    "pg_image",
    "benchbase_image",
    "load_seconds",
    "db_size_bytes",
    "db_size_pretty",
    "db_size_gib",
    "run_seconds",
    "tps",
    "tpm",
    "tpm_per_vu",
    "tpm_per_core",
    "goodput_tps",
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "latency_avg_ms",
    "raw_load_log",
    "raw_db_size",
    "raw_run_log",
    "raw_config",
    "raw_summary",
    "error",
]


def run_one(
    *,
    run_id: str,
    hostname: str,
    cpus: int,
    mem: float,
    workload: str,
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
    scalefactor = scalefactor_for(workload, warehouses)
    expected_gib = round(expected_schema_gib(workload, scalefactor), 4)
    label = f"{workload}_vu{run_vus:02d}"
    tmp_raw: tempfile.TemporaryDirectory[str] | None = None
    if skip_raw:
        tmp_raw = tempfile.TemporaryDirectory(prefix="bb-eval-raw-")
        raw_dir = Path(tmp_raw.name)
    else:
        raw_dir = out_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
    results_dir = raw_dir / f"{label}_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    load_log = raw_dir / f"{label}_load.log"
    db_size_log = raw_dir / f"{label}_db_size.json"
    run_log = raw_dir / f"{label}_run.log"
    config_path = raw_dir / f"{label}_config.xml"
    summary_path = raw_dir / f"{label}_summary.json"
    sf_min = UNITS_PER_VU_MIN * run_vus
    row = {
        "run_id": run_id,
        "hostname": hostname,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "ncpus": cpus,
        "mem_gib": mem,
        "workload": workload,
        "run_vus": run_vus,
        "terminals": run_vus,
        "warehouses_equiv": warehouses,
        "wh_per_vu": wh_per_vu,
        "scalefactor": scalefactor,
        "sf_per_vu": round(scalefactor / run_vus, 2),
        "expected_schema_gib": expected_gib,
        "units_per_vu_min": UNITS_PER_VU_MIN,
        "sf_covers_vu_min": scalefactor >= sf_min,
        "shared_buffers_gb": "",
        "effective_cache_size_gb": "",
        "synchronous_commit": "",
        "pg_data_storage": "tmpfs" if pg_tmpfs else "anonymous_volume",
        "pg_tmpfs_size_gib": "",
        "rampup_min": rampup_min,
        "duration_min": duration_min,
        "pg_image": ev.PG_IMAGE,
        "benchbase_image": BB_IMAGE,
        "load_seconds": "",
        "db_size_bytes": "",
        "db_size_pretty": "",
        "db_size_gib": "",
        "run_seconds": "",
        "tps": "",
        "tpm": "",
        "tpm_per_vu": "",
        "tpm_per_core": "",
        "goodput_tps": "",
        "latency_p50_ms": "",
        "latency_p95_ms": "",
        "latency_p99_ms": "",
        "latency_avg_ms": "",
        "raw_load_log": "" if skip_raw else str(load_log.relative_to(out_dir)),
        "raw_db_size": "" if skip_raw else str(db_size_log.relative_to(out_dir)),
        "raw_run_log": "" if skip_raw else str(run_log.relative_to(out_dir)),
        "raw_config": "" if skip_raw else str(config_path.relative_to(out_dir)),
        "raw_summary": "" if skip_raw else str(summary_path.relative_to(out_dir)),
        "error": "",
    }
    print(
        f"\n=== {label}: {run_vus} terminals / SF={scalefactor} "
        f"(WH-equiv={warehouses}, ~{expected_gib} GiB; "
        f"warmup={rampup_min}m, duration={duration_min}m) ===",
        flush=True,
    )
    if scalefactor < sf_min:
        print(
            f"note: SF={scalefactor} < {UNITS_PER_VU_MIN}×VU={sf_min} "
            f"(inspector soft cap); keeping terminals={run_vus} to match "
            f"HammerDB ladder / working-set sizing",
            flush=True,
        )
    try:
        sb_gb, ecs, storage, used_plan = ev.start_postgres(
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
        sync = ev.verify_sync_commit()
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

        ensure_bench_db()
        load_secs = load_schema(
            workload=workload,
            scalefactor=scalefactor,
            config_path=config_path,
            results_dir=results_dir,
            log_path=load_log,
        )
        row["load_seconds"] = round(load_secs, 1)
        print(f"load done in {load_secs:.0f}s", flush=True)

        db_size = measure_db_size(db_size_log)
        row["db_size_bytes"] = db_size["size_bytes"]
        row["db_size_pretty"] = db_size["size_pretty"]
        row["db_size_gib"] = db_size["size_gib"]
        print(
            f"{BENCH_DB} db size: {db_size['size_pretty']} "
            f"({db_size['size_bytes']} bytes, {db_size['size_gib']} GiB)",
            flush=True,
        )

        result, run_secs = timed_run(
            workload=workload,
            scalefactor=scalefactor,
            terminals=run_vus,
            rampup_min=rampup_min,
            duration_min=duration_min,
            config_path=config_path,
            results_dir=results_dir,
            log_path=run_log,
            summary_copy=summary_path,
        )
        row["run_seconds"] = round(run_secs, 1)
        row["tps"] = result["tps"]
        row["tpm"] = result["tpm"]
        row["tpm_per_vu"] = round(result["tpm"] / run_vus, 1)
        row["tpm_per_core"] = row["tpm_per_vu"]
        row["goodput_tps"] = result.get("goodput_tps", "")
        row["latency_p50_ms"] = result.get("p50_ms", "")
        row["latency_p95_ms"] = result.get("p95_ms", "")
        row["latency_p99_ms"] = result.get("p99_ms", "")
        row["latency_avg_ms"] = result.get("avg_ms", "")
        print(
            f"RESULT tpm={result['tpm']} tps={result['tps']} "
            f"({row['tpm_per_vu']} TPM/VU)",
            flush=True,
        )
    except Exception as exc:
        row["error"] = str(exc)
        print(f"FAIL {label}: {exc}", flush=True)
    finally:
        ev.cleanup_containers()
    return row


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--vus",
        default=",".join(str(v) for v in DEFAULT_VUS),
        help=f"comma-separated terminal counts (default: {','.join(map(str, DEFAULT_VUS))})",
    )
    p.add_argument(
        "--workloads",
        default=",".join(DEFAULT_WORKLOADS),
        help=f"comma-separated workloads (default: {','.join(DEFAULT_WORKLOADS)})",
    )
    p.add_argument(
        "--wh-per-vu",
        type=int,
        default=WH_PER_VU,
        help=(
            f"HammerDB warehouses/VU used to size the working set "
            f"(default {WH_PER_VU}); SF = f(warehouses)"
        ),
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
        help=f"mount Postgres data ({ev.PG_DATA_DIR}) on tmpfs instead of an anonymous volume",
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
            "size Postgres GUCs from live host topology via pg_tune_gucs.py; "
            "default is the sc-inspector async ladder profile"
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
    bind_eval_names()

    vu_list = [int(x.strip()) for x in args.vus.split(",") if x.strip()]
    workloads = [x.strip().lower() for x in args.workloads.split(",") if x.strip()]
    if not vu_list:
        print("no VU counts given", file=sys.stderr)
        return 2
    if not workloads:
        print("no workloads given", file=sys.stderr)
        return 2
    unknown = [w for w in workloads if w not in WORKLOADS]
    if unknown:
        print(
            f"unknown workload(s) {unknown}; choose from {sorted(WORKLOADS)}",
            file=sys.stderr,
        )
        return 2
    if args.wh_per_vu < 4:
        print(
            f"warning: HammerDB docs recommend >=4 warehouses/VU; got {args.wh_per_vu}",
            file=sys.stderr,
        )
    if args.pg_tmpfs_size_gib is not None and not args.pg_tmpfs:
        print("warning: --pg-tmpfs-size-gib ignored without --pg-tmpfs", file=sys.stderr)
    if args.pg_tmpfs_size_gib is not None and args.pg_tmpfs_size_gib < 1:
        print("--pg-tmpfs-size-gib must be >= 1", file=sys.stderr)
        return 2

    cpus = ev.ncpus()
    mem = ev.mem_gib()
    hostname = socket.gethostname()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_{hostname}_bb"
    out_dir = args.out_dir or (Path.cwd() / "results" / run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_raw:
        (out_dir / "raw").mkdir(exist_ok=True)

    tmpfs_size = (
        args.pg_tmpfs_size_gib
        if args.pg_tmpfs and args.pg_tmpfs_size_gib is not None
        else (ev.default_tmpfs_size_gib(mem) if args.pg_tmpfs else None)
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
            f"shared_buffers={int(ev.SHARED_BUFFERS_FRAC*100)}% RAM and "
            f"effective_cache_size={int(ev.EFFECTIVE_CACHE_FRAC*100)}% RAM "
            "(fixed across VU points)"
        )

    sf_ladder = {
        wl: {
            str(vu): {
                "warehouses_equiv": warehouses_for_vus(vu, args.wh_per_vu),
                "scalefactor": scalefactor_for(wl, warehouses_for_vus(vu, args.wh_per_vu)),
                "expected_schema_gib": round(
                    expected_schema_gib(
                        wl, scalefactor_for(wl, warehouses_for_vus(vu, args.wh_per_vu))
                    ),
                    4,
                ),
            }
            for vu in vu_list
        }
        for wl in workloads
    }

    meta = {
        "run_id": run_id,
        "hostname": hostname,
        "ncpus": cpus,
        "mem_gib": mem,
        "pg_image": ev.PG_IMAGE,
        "benchbase_image": BB_IMAGE,
        "wh_per_vu": args.wh_per_vu,
        "vus": vu_list,
        "workloads": workloads,
        "rampup_min": args.rampup_min,
        "duration_min": args.duration_min,
        "pg_tmpfs": args.pg_tmpfs,
        "pg_tmpfs_size_gib": tmpfs_size if args.pg_tmpfs else None,
        "pg_tune_host": args.pg_tune_host,
        "skip_raw": args.skip_raw,
        "host_topology": probe_host_topology(mem_gib=mem).to_dict(),
        "pg_guc_plan": guc_plan.to_dict() if guc_plan else None,
        "scale_ladder": sf_ladder,
        "policy": {
            "synchronous_commit": "off",
            "gucs": guc_policy,
            "pg_data_storage": (
                f"tmpfs {tmpfs_size}GiB on {ev.PG_DATA_DIR}"
                if args.pg_tmpfs
                else f"anonymous docker volume ({ev.PG_DATA_DIR})"
            ),
            "working_set": (
                f"SF sized to match HammerDB {args.wh_per_vu} WH/VU schema "
                f"(WH_SIZE_GIB={WH_SIZE_GIB}, "
                f"WIKIPEDIA_GIB_PER_SF={WIKIPEDIA_GIB_PER_SF}, "
                f"YCSB_ROW_KIB={YCSB_ROW_KIB})"
            ),
            "terminals": "same as run_vus (HammerDB VU ladder)",
            "units_per_vu_min": (
                f"inspector soft cap is SF//{UNITS_PER_VU_MIN}; "
                "this harness does not clamp terminals to that cap"
            ),
            "empty_db_each_round": True,
            "warmup": f"{args.rampup_min} min BenchBase <warmup> before measurement",
            "mix": {k: {"weights": v["weights"], "txn_types": list(v["txn_types"])} for k, v in WORKLOADS.items()},
        },
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2), flush=True)

    ev.ensure_network()
    if not args.skip_pull:
        print(f"pulling {ev.PG_IMAGE} …", flush=True)
        ev.sh(f"docker pull {ev.PG_IMAGE}")
        print(f"pulling {BB_IMAGE} …", flush=True)
        ev.sh(f"docker pull {BB_IMAGE}")

    csv_path = out_dir / "results.csv"
    rows: list[dict] = []
    for workload in workloads:
        for run_vus in vu_list:
            row = run_one(
                run_id=run_id,
                hostname=hostname,
                cpus=cpus,
                mem=mem,
                workload=workload,
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
            write_csv(csv_path, rows)

    meta["finished_utc"] = datetime.now(timezone.utc).isoformat()
    meta["rows"] = len(rows)
    meta["ok"] = sum(1 for r in rows if not r.get("error"))
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    ev.cleanup_containers()
    print(f"\nWrote {csv_path}", flush=True)
    if not args.skip_raw:
        print(f"Raw logs under {out_dir / 'raw'}", flush=True)
    return 0 if all(not r.get("error") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
