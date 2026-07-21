#!/usr/bin/env python3
"""Exhaustive-ish GUC sweep for HammerDB vu32 on tmpfs (F32-class hosts).

Memory budget (dedicated host):
  reserved_os ≈ 16 GiB
  schema(vu=vcpus, wh=4*vu) ≈ vcpus * 4 * 0.1 GiB  (≈12.8 GiB at 32)
  with WH_PER_VU=5 (this harness): schema ≈ 15 GiB at vu32
  tmpfs must hold schema + WAL (max_wal_size) + headroom
  shared_buffers + tmpfs + reserved_os <= mem_gib

Baseline (run2-tmpfs Standard_F32ams_v6 vu32): NOPM=1131026 TPM=2600764
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import run_wh_sizing_eval as ev
from pg_tune_gucs import PgGucPlan, probe_host_topology, tune_pg_gucs

BASELINE_NOPM = 1_131_026
BASELINE_TPM = 2_600_764

# Keep enough tmpfs for vu32 (15 GiB DB) + WAL 16 GiB + ~2× headroom.
MIN_TMPFS_FOR_VU32 = 48
RESERVED_OS_GIB = 16


def memory_ok(mem_gib: float, shared_buffers_gb: int, tmpfs_gib: int) -> tuple[bool, str]:
    used = shared_buffers_gb + tmpfs_gib + RESERVED_OS_GIB
    ok = used <= mem_gib
    return ok, f"sb={shared_buffers_gb}+tmpfs={tmpfs_gib}+os={RESERVED_OS_GIB}={used} vs mem={mem_gib}"


def tmpfs_for(mem_gib: float, shared_buffers_gb: int) -> int:
    """Largest tmpfs that still leaves room for shared_buffers + OS, ≥ MIN_TMPFS."""
    room = int(mem_gib) - shared_buffers_gb - RESERVED_OS_GIB
    return max(MIN_TMPFS_FOR_VU32, min(room, int(mem_gib * 0.5)))


def settings_to_plan(settings: list[str], topo) -> PgGucPlan:
    sb = 1
    ecs = 1
    for s in settings:
        if s.startswith("shared_buffers="):
            val = s.split("=", 1)[1]
            if val.upper().endswith("GB"):
                sb = int(float(val[:-2]))
        if s.startswith("effective_cache_size="):
            val = s.split("=", 1)[1]
            if val.upper().endswith("GB"):
                ecs = int(float(val[:-2]))
    return PgGucPlan(
        settings=settings,
        shared_buffers_gb=sb,
        effective_cache_size_gb=ecs,
        topology=topo,
        rationale={"sweep": "custom"},
    )


def baseline_settings(mem_gib: float, vcpus: int) -> list[str]:
    """Match run2-tmpfs baseline GUCs (pre --pg-tune-host)."""
    sb = max(1, int(mem_gib * 0.25))
    ecs = max(sb, int(mem_gib * 0.75))
    mpw = min(max(1, vcpus), 128)
    return [
        f"shared_buffers={sb}GB",
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


def with_overrides(base: list[str], overrides: dict[str, str]) -> list[str]:
    """Replace or append GUC key=value entries."""
    keys = {k.split("=", 1)[0] for k in overrides}
    out = [s for s in base if s.split("=", 1)[0] not in keys]
    for k, v in overrides.items():
        out.append(f"{k}={v}")
    # drop io_workers unless io_method=worker
    method = next((s.split("=", 1)[1] for s in out if s.startswith("io_method=")), None)
    if method != "worker":
        out = [s for s in out if not s.startswith("io_workers=")]
    return out


def build_experiment_matrix(mem_gib: float, vcpus: int, topo) -> list[dict]:
    """Axis sweeps + a few combos. Each entry: name, overrides, optional sb force."""
    base = baseline_settings(mem_gib, vcpus)
    tuned = tune_pg_gucs(
        mem_gib=mem_gib,
        topology=topo,
        workload="oltp",
        storage="tmpfs",
        prefer_io_uring=True,
    ).settings

    experiments: list[dict] = []

    def add(name: str, settings: list[str]) -> None:
        sb = next(
            (int(float(s.split("=")[1][:-2])) for s in settings if s.startswith("shared_buffers=") and s.endswith("GB")),
            1,
        )
        experiments.append({"name": name, "settings": settings, "shared_buffers_gb": sb})

    # --- Phase 0: anchors -------------------------------------------------
    add("A_baseline", base)
    add("B_host_tune_tmpfs", tuned)

    # --- Phase 1: shared_buffers on tmpfs (avoid double-buffering) --------
    for sb in (8, 16, 24, 32, 48, 62):
        add(
            f"C_sb{sb}",
            with_overrides(
                base,
                {
                    "shared_buffers": f"{sb}GB",
                    "effective_cache_size": f"{max(sb, int(mem_gib * 0.75))}GB",
                },
            ),
        )

    # --- Phase 2: I/O method / WAL (on promising mid SB=16 + baseline sb) -
    for sb in (16, 62):
        for io in ("io_uring", "worker"):
            ov = {
                "shared_buffers": f"{sb}GB",
                "effective_cache_size": f"{max(sb, int(mem_gib * 0.75))}GB",
                "io_method": io,
                "random_page_cost": "1.0",
                "effective_io_concurrency": "1000",
                "jit": "off",
                "wal_compression": "lz4",
                "max_wal_size": "16GB",
                "min_wal_size": "2GB",
            }
            if io == "worker":
                ov["io_workers"] = str(min(32, max(3, vcpus // 4)))
            add(f"D_sb{sb}_{io}", with_overrides(base, ov))

    # --- Phase 3: WAL / compression / buffers at sb=16 io_uring -----------
    core = {
        "shared_buffers": "16GB",
        "effective_cache_size": f"{int(mem_gib * 0.75)}GB",
        "io_method": "io_uring",
        "random_page_cost": "1.0",
        "effective_io_concurrency": "1000",
        "jit": "off",
        "max_wal_size": "16GB",
        "min_wal_size": "2GB",
    }
    for wal_c in ("off", "lz4"):
        add(f"E_walcomp_{wal_c}", with_overrides(base, {**core, "wal_compression": wal_c}))
    for wb in ("16MB", "64MB", "256MB"):
        add(f"F_walbuf_{wb}", with_overrides(base, {**core, "wal_compression": "lz4", "wal_buffers": wb}))

    # --- Phase 4: parallel / work_mem -------------------------------------
    for pg in ("0", "2"):
        add(
            f"G_pergather_{pg}",
            with_overrides(
                base,
                {**core, "wal_compression": "lz4", "max_parallel_workers_per_gather": pg},
            ),
        )
    for wm in ("8MB", "16MB", "64MB"):
        add(
            f"H_workmem_{wm}",
            with_overrides(
                base,
                {
                    **core,
                    "wal_compression": "lz4",
                    "work_mem": wm,
                    "max_parallel_workers_per_gather": "0",
                },
            ),
        )

    # --- Phase 5: eic / rpc -----------------------------------------------
    for eic in ("128", "200", "1000"):
        add(
            f"I_eic_{eic}",
            with_overrides(
                base,
                {
                    **core,
                    "wal_compression": "lz4",
                    "effective_io_concurrency": eic,
                    "max_parallel_workers_per_gather": "0",
                },
            ),
        )

    # --- Phase 6: larger maintenance / autovac (build-friendly) -----------
    add(
        "J_maint_heavy",
        with_overrides(
            base,
            {
                **core,
                "wal_compression": "lz4",
                "maintenance_work_mem": "8GB",
                "max_parallel_maintenance_workers": "4",
                "autovacuum_max_workers": "5",
                "max_worker_processes": str(vcpus + 12),
                "max_parallel_workers": str(vcpus),
                "max_parallel_workers_per_gather": "0",
            },
        ),
    )

    # Deduplicate by settings tuple while keeping order.
    seen: set[tuple[str, ...]] = set()
    unique: list[dict] = []
    for exp in experiments:
        key = tuple(sorted(exp["settings"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(exp)
    return unique


def run_one_config(
    *,
    name: str,
    settings: list[str],
    mem: float,
    cpus: int,
    run_vus: int,
    out_root: Path,
    topo,
) -> dict:
    sb = next(
        (int(float(s.split("=")[1][:-2])) for s in settings if s.startswith("shared_buffers=") and s.endswith("GB")),
        1,
    )
    tmpfs_gib = tmpfs_for(mem, sb)
    ok, msg = memory_ok(mem, sb, tmpfs_gib)
    label_dir = out_root / name
    label_dir.mkdir(parents=True, exist_ok=True)
    (label_dir / "settings.json").write_text(
        json.dumps(
            {
                "name": name,
                "settings": settings,
                "shared_buffers_gb": sb,
                "tmpfs_gib": tmpfs_gib,
                "memory_check": msg,
                "memory_ok": ok,
            },
            indent=2,
        )
        + "\n"
    )
    if not ok:
        return {
            "name": name,
            "run_vus": run_vus,
            "nopm": "",
            "tpm": "",
            "build_seconds": "",
            "shared_buffers_gb": sb,
            "tmpfs_gib": tmpfs_gib,
            "error": f"memory budget: {msg}",
            "delta_nopm_pct": "",
        }

    plan = settings_to_plan(settings, topo)
    row = ev.run_one(
        run_id=out_root.name,
        hostname=socket.gethostname(),
        cpus=cpus,
        mem=mem,
        run_vus=run_vus,
        wh_per_vu=ev.WH_PER_VU,
        rampup_min=ev.DEFAULT_RAMPUP_MIN,
        duration_min=ev.DEFAULT_DURATION_MIN,
        out_dir=label_dir,
        pg_tmpfs=True,
        pg_tmpfs_size_gib=tmpfs_gib,
        tune_host=True,
        guc_plan=plan,
    )
    nopm = row.get("nopm") or ""
    tpm = row.get("tpm") or ""
    delta = ""
    if nopm != "":
        delta = round(100.0 * (int(nopm) - BASELINE_NOPM) / BASELINE_NOPM, 2)
    result = {
        "name": name,
        "run_vus": run_vus,
        "nopm": nopm,
        "tpm": tpm,
        "build_seconds": row.get("build_seconds", ""),
        "shared_buffers_gb": sb,
        "tmpfs_gib": tmpfs_gib,
        "error": row.get("error", ""),
        "delta_nopm_pct": delta,
        "settings": " ".join(settings),
    }
    (label_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--only", default="", help="comma-separated experiment name prefixes to run")
    ap.add_argument("--skip-pull", action="store_true")
    ap.add_argument("--vus", type=int, default=32)
    ap.add_argument("--verify-vu4-best", action="store_true",
                    help="after sweep, re-run best config at vu4")
    args = ap.parse_args()

    cpus = ev.ncpus()
    mem = ev.mem_gib()
    topo = probe_host_topology(mem_gib=mem)
    out_dir = args.out_dir or (
        Path("/bench/guc_sweep")
        / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_vu{args.vus}")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    experiments = build_experiment_matrix(mem, cpus, topo)
    if args.only:
        prefixes = [p.strip() for p in args.only.split(",") if p.strip()]
        experiments = [e for e in experiments if any(e["name"].startswith(p) for p in prefixes)]

    meta = {
        "host": socket.gethostname(),
        "ncpus": cpus,
        "mem_gib": mem,
        "topology": topo.to_dict(),
        "baseline_nopm": BASELINE_NOPM,
        "baseline_tpm": BASELINE_TPM,
        "experiments": [e["name"] for e in experiments],
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2), flush=True)

    ev.ensure_network()
    if not args.skip_pull:
        ev.sh(f"docker pull {ev.PG_IMAGE}")
        ev.sh(f"docker pull {ev.HDB_IMAGE}")

    csv_path = out_dir / "sweep.csv"
    fields = [
        "name",
        "run_vus",
        "nopm",
        "tpm",
        "build_seconds",
        "shared_buffers_gb",
        "tmpfs_gib",
        "delta_nopm_pct",
        "error",
        "settings",
    ]
    rows: list[dict] = []

    for i, exp in enumerate(experiments, 1):
        print(f"\n######## [{i}/{len(experiments)}] {exp['name']} ########", flush=True)
        t0 = time.time()
        row = run_one_config(
            name=exp["name"],
            settings=exp["settings"],
            mem=mem,
            cpus=cpus,
            run_vus=args.vus,
            out_root=out_dir,
            topo=topo,
        )
        row["elapsed_wall_s"] = round(time.time() - t0, 1)
        rows.append(row)
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields + ["elapsed_wall_s"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields + ["elapsed_wall_s"]})
        print(
            f"==> {exp['name']}: nopm={row.get('nopm')} "
            f"delta={row.get('delta_nopm_pct')}% err={row.get('error')!r}",
            flush=True,
        )

    # Pick best by NOPM among successful runs.
    ok_rows = [r for r in rows if r.get("nopm") not in ("", None) and not r.get("error")]
    best = max(ok_rows, key=lambda r: int(r["nopm"])) if ok_rows else None
    summary = {
        "best": best,
        "baseline_nopm": BASELINE_NOPM,
        "n_ok": len(ok_rows),
        "n_total": len(rows),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)

    if args.verify_vu4_best and best:
        print("\n######## verify vu4 with best settings ########", flush=True)
        best_settings = best["settings"].split()
        vu4 = run_one_config(
            name=f"VERIFY_vu4__{best['name']}",
            settings=best_settings,
            mem=mem,
            cpus=cpus,
            run_vus=4,
            out_root=out_dir,
            topo=topo,
        )
        (out_dir / "verify_vu4.json").write_text(json.dumps(vu4, indent=2) + "\n")
        print(json.dumps(vu4, indent=2), flush=True)

    ev.cleanup_containers()
    print(f"\nWrote {csv_path}", flush=True)
    return 0 if best and not best.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
