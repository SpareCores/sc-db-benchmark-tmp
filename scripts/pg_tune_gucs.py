"""Hardware-aware PostgreSQL 18 GUC tuning for dedicated OLTP hosts.

Probes live system topology (logical vs physical CPUs / HyperThreading,
NUMA nodes, RAM, huge pages, io_uring availability) and produces a
``postgres -c …`` argument list sized for multi-CPU OLTP workloads
(HammerDB TPROC-C style: high concurrency, short transactions).

Formulas and knobs draw from:
  - Instaclustr "PostgreSQL tuning: 10 things…" (shared_buffers 25–40%,
    max_worker_processes ≈ cores, max_parallel_workers_per_gather ≈ 25–50%
    for general use — we keep per-gather low for OLTP)
  - PGTune (le0pard/pgtune) hardware formulas for OLTP
  - PostgresAI rough OLTP configuration tuning
  - PostgreSQL 18 docs / source: ``io_method``, ``io_workers`` (default 3,
    max 32), ``huge_pages``, parallel-worker GUCs

``io_method=io_uring`` needs a liburing-enabled build (Debian
``postgres:18``; Alpine often lacks the enum — docker-library/postgres#1365)
and a privileged container (``--privileged`` / seccomp unconfined).

Default profile without this module (sc-inspector async ladder) is
intentionally simpler; this module is opt-in for host-tuned experiments.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

StorageHint = Literal["ssd", "nvme", "tmpfs", "hdd", "unknown"]
WorkloadHint = Literal["oltp", "mixed", "analytics"]

# Keep headroom for postmaster, autovacuum, WAL writer, checkpointer, etc.
_MAX_WORKER_PROCESSES = 128
_MAX_IO_WORKERS = 32  # PG18 hard max for io_workers


@dataclass(frozen=True)
class HostTopology:
    """Live host properties used for GUC sizing."""

    logical_cpus: int
    physical_cpus: int
    threads_per_core: float
    sockets: int
    numa_nodes: int
    mem_gib: float
    hugepages_total: int
    hugepages_free: int
    hugepage_size_kib: int
    io_uring_sysctl_ok: bool
    cpu_model: str = ""

    @property
    def hyperthreading(self) -> bool:
        return self.logical_cpus > self.physical_cpus

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["hyperthreading"] = self.hyperthreading
        return d


@dataclass
class PgGucPlan:
    """Tuned GUCs plus the topology / rationale used to derive them."""

    settings: list[str]
    shared_buffers_gb: int
    effective_cache_size_gb: int
    topology: HostTopology
    rationale: dict[str, str] = field(default_factory=dict)
    docker_c_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.docker_c_args:
            args: list[str] = []
            for s in self.settings:
                args.extend(["-c", s])
            self.docker_c_args = args

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings": list(self.settings),
            "shared_buffers_gb": self.shared_buffers_gb,
            "effective_cache_size_gb": self.effective_cache_size_gb,
            "topology": self.topology.to_dict(),
            "rationale": dict(self.rationale),
        }


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    text = _read_text(Path("/proc/meminfo")) or ""
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _count_numa_nodes() -> int:
    node_dir = Path("/sys/devices/system/node")
    if not node_dir.is_dir():
        return 1
    nodes = [p for p in node_dir.iterdir() if re.fullmatch(r"node\d+", p.name)]
    return max(1, len(nodes))


def _cpu_topology() -> tuple[int, int, int, str]:
    """Return (logical, physical, sockets, model).

    Physical CPUs = unique ``thread_siblings_list`` groups (one per core,
    HyperThreading siblings share a group). Falls back to ``nproc`` / lscpu.
    """
    logical = os.cpu_count() or 1
    try:
        logical = int(
            subprocess.check_output(["nproc"], text=True, timeout=5).strip()
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    sibling_groups: set[str] = set()
    cpu_root = Path("/sys/devices/system/cpu")
    for path in cpu_root.glob("cpu[0-9]*/topology/thread_siblings_list"):
        text = _read_text(path)
        if text:
            sibling_groups.add(text)
    physical = len(sibling_groups) if sibling_groups else logical

    sockets = 1
    model = ""
    try:
        lscpu = subprocess.check_output(
            ["lscpu"], text=True, encoding="utf-8", errors="replace", timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        lscpu = ""
    for line in lscpu.splitlines():
        if line.startswith("Socket(s):"):
            try:
                sockets = max(1, int(line.split(":", 1)[1].strip()))
            except ValueError:
                pass
        elif line.startswith("Model name:"):
            model = line.split(":", 1)[1].strip()

    # Sanity: never report more physical than logical.
    physical = max(1, min(physical, logical))
    return logical, physical, sockets, model


def _io_uring_sysctl_ok() -> bool:
    """True when the kernel allows io_uring (sysctl not disabling it).

    Docker's default seccomp profile often still blocks io_uring even when
    this returns True — callers running in containers should prefer
    ``io_method=worker`` unless the container is granted io_uring.
    """
    path = Path("/proc/sys/kernel/io_uring_disabled")
    text = _read_text(path)
    if text is None:
        # Old kernels without the sysctl: treat as unavailable for PG18 AIO.
        return False
    try:
        return int(text) == 0
    except ValueError:
        return False


def probe_host_topology(mem_gib: float | None = None) -> HostTopology:
    """Inspect the live host and return topology used for GUC sizing."""
    logical, physical, sockets, model = _cpu_topology()
    info = _meminfo()
    mem_kib = info.get("MemTotal", 0)
    probed_mem = round(mem_kib / 1024 / 1024, 1) if mem_kib else 1.0
    return HostTopology(
        logical_cpus=logical,
        physical_cpus=physical,
        threads_per_core=round(logical / physical, 2),
        sockets=sockets,
        numa_nodes=_count_numa_nodes(),
        mem_gib=float(mem_gib) if mem_gib is not None else probed_mem,
        hugepages_total=info.get("HugePages_Total", 0),
        hugepages_free=info.get("HugePages_Free", 0),
        hugepage_size_kib=info.get("Hugepagesize", 2048),
        io_uring_sysctl_ok=_io_uring_sysctl_ok(),
        cpu_model=model,
    )


def _fmt_mb(mb: int) -> str:
    if mb >= 1024:
        return f"{max(1, mb // 1024)}GB"
    return f"{mb}MB"


def tune_pg_gucs(
    *,
    mem_gib: float | None = None,
    topology: HostTopology | None = None,
    workload: WorkloadHint = "oltp",
    storage: StorageHint = "ssd",
    max_connections: int = 400,
    # Prefer PG18 io_uring when the kernel allows it. Requires a Postgres
    # build with liburing (Debian ``postgres:18`` yes; Alpine often no —
    # see docker-library/postgres#1365) and a privileged container
    # (``--privileged`` / seccomp unconfined); otherwise setup fails with
    # "Operation not permitted".
    prefer_io_uring: bool = True,
    shared_buffers_frac: float | None = None,
    effective_cache_frac: float = 0.75,
) -> PgGucPlan:
    """Return PostgreSQL 18 GUCs sized from live host properties.

    OLTP defaults are calibrated from an exhaustive HammerDB vu32/tmpfs
    sweep on Azure Standard_F32ams_v6 (32 vCPU / 252 GiB), vs run2-tmpfs
    baseline NOPM=1_131_026:

      * shared_buffers ≈ 10% RAM on tmpfs (cap 24 GiB) — 25% double-buffers
        against tmpfs and lost ~1–2% vs a 24 GiB buffer
      * io_method=io_uring ≫ worker (+~3–4% absolute)
      * work_mem=16MB, max_parallel_workers_per_gather=0
      * wal_buffers=16MB, wal_compression=lz4, jit=off
      * Best single config ~+20% NOPM; vu4 check was +4% (no regression)

    Memory budget assumed for sizing: reserved OS ≈ 16 GiB, peak schema at
    ``vu = ncpus``, ``wh ≈ 4–5 × vu`` (~100 MB/WH) plus WAL, with tmpfs
    holding the datadir beside ``shared_buffers``.
    """
    topo = topology or probe_host_topology(mem_gib=mem_gib)
    mem = topo.mem_gib if mem_gib is None else float(mem_gib)
    rationale: dict[str, str] = {
        "workload": workload,
        "storage": storage,
        "sources": (
            "f32ams-v6-vu32-tmpfs-sweep; instaclustr-tuning-guide; "
            "pgtune-oltp; postgresai-oltp; postgresql-18-aio"
        ),
    }

    # --- Memory ------------------------------------------------------------
    # Disk/SSD: classic 25% shared_buffers. tmpfs: data is already in RAM, so
    # large shared_buffers double-buffer; empirical sweet spot ≈ 10% / ≤24 GiB.
    if shared_buffers_frac is None:
        shared_buffers_frac = 0.10 if storage == "tmpfs" else 0.25
    sb_gb = max(1, int(mem * shared_buffers_frac))
    if storage == "tmpfs":
        sb_cap = 24
        if sb_gb > sb_cap:
            rationale["shared_buffers_tmpfs_cap"] = (
                f"capped {sb_gb}GB → {sb_cap}GB (tmpfs sweep on F32ams_v6)"
            )
            sb_gb = sb_cap
    if topo.numa_nodes > 1:
        per_node_cap = max(1, int(mem / topo.numa_nodes * 0.40))
        if sb_gb > per_node_cap:
            rationale["shared_buffers_numa_cap"] = (
                f"capped {sb_gb}GB → {per_node_cap}GB "
                f"({topo.numa_nodes} NUMA nodes)"
            )
            sb_gb = per_node_cap
    ecs_gb = max(sb_gb, int(mem * effective_cache_frac))

    # Keep maintenance_work_mem modest for OLTP runtime (sweep winners used
    # 1GB); allow larger for analytics/index-heavy builds.
    if workload == "oltp":
        maint_mb = 1024
    else:
        maint_mb = min(8 * 1024, max(256, int(mem * 1024 / 16)))
    autovac_work_mb = min(2048, maint_mb)

    # --- CPU / workers (multi-core + HT aware) -----------------------------
    phys = topo.physical_cpus
    logical = topo.logical_cpus
    rationale["cpu"] = (
        f"logical={logical} physical={phys} "
        f"HT={'yes' if topo.hyperthreading else 'no'} "
        f"sockets={topo.sockets} numa={topo.numa_nodes}"
    )

    if workload == "analytics":
        parallel_per_gather = max(2, min(8, (phys + 1) // 2))
        parallel_workers = phys
        work_mem_mb = 64
    elif workload == "mixed":
        parallel_per_gather = min(4, max(2, phys // 4))
        parallel_workers = phys
        work_mem_mb = 32
    else:
        # OLTP sweep: per_gather=0 and work_mem=16MB were the largest wins
        # (~+10% and ~+20% stacked with io_uring / sb sizing).
        parallel_per_gather = 0
        parallel_workers = phys
        work_mem_mb = 16

    parallel_maint = min(4, max(2, (phys + 1) // 2)) if phys >= 4 else 1

    if phys >= 32:
        autovac_workers = 5
    elif phys >= 16:
        autovac_workers = 4
    else:
        autovac_workers = 3

    # PG18 AIO — io_uring won clearly over worker on privileged Debian image.
    if prefer_io_uring and topo.io_uring_sysctl_ok:
        io_method = "io_uring"
        io_workers = 3
        rationale["io_method"] = (
            "io_uring (F32 sweep: +3–4% vs worker; needs privileged "
            "container + liburing image)"
        )
    else:
        io_method = "worker"
        io_workers = min(_MAX_IO_WORKERS, max(3, phys // 4))
        why = (
            "prefer_io_uring=False"
            if not prefer_io_uring
            else "kernel io_uring_disabled!=0"
        )
        rationale["io_method"] = (
            f"worker ({why}); io_workers={io_workers} "
            f"(~25% of {phys} physical CPUs, cap {_MAX_IO_WORKERS})"
        )

    worker_budget = parallel_workers + autovac_workers + (
        io_workers if io_method == "worker" else 0
    ) + 4
    max_worker_processes = min(
        _MAX_WORKER_PROCESSES, max(logical, worker_budget, phys)
    )
    max_parallel_workers = min(parallel_workers, max_worker_processes)

    # --- I/O / planner costs ----------------------------------------------
    if storage == "tmpfs":
        random_page_cost = 1.0
        # Sweep: eic=200 and eic=1000 both strong; keep 1000 for RAM disk.
        effective_io = 1000
        rationale["storage_io"] = "tmpfs: random_page_cost=1.0, eic=1000"
    elif storage == "nvme":
        random_page_cost = 1.1
        effective_io = 1000
        rationale["storage_io"] = "nvme: effective_io_concurrency=1000"
    elif storage == "hdd":
        random_page_cost = 4.0
        effective_io = 2
        rationale["storage_io"] = "hdd: postgres defaults-ish"
    else:
        random_page_cost = 1.1
        effective_io = 200
        rationale["storage_io"] = "ssd: random_page_cost=1.1, eic=200"

    maintenance_io = max(16, effective_io // 2)
    huge_pages = "try" if sb_gb >= 2 else "off"

    # wal_buffers=16MB beat 64MB/256MB on the F32 tmpfs sweep (~+10% alone).
    wal_buffers = "16MB"
    max_wal = "16GB" if workload == "oltp" else "8GB"
    min_wal = "2GB" if workload == "oltp" else "1GB"

    settings = [
        f"shared_buffers={sb_gb}GB",
        f"effective_cache_size={ecs_gb}GB",
        f"work_mem={_fmt_mb(work_mem_mb)}",
        f"maintenance_work_mem={_fmt_mb(maint_mb)}",
        f"autovacuum_work_mem={_fmt_mb(autovac_work_mb)}",
        f"max_connections={max_connections}",
        f"max_worker_processes={max_worker_processes}",
        f"max_parallel_workers={max_parallel_workers}",
        f"max_parallel_workers_per_gather={parallel_per_gather}",
        f"max_parallel_maintenance_workers={parallel_maint}",
        f"autovacuum_max_workers={autovac_workers}",
        f"io_method={io_method}",
        f"huge_pages={huge_pages}",
        "synchronous_commit=off",
        f"wal_buffers={wal_buffers}",
        "wal_compression=lz4",
        f"max_wal_size={max_wal}",
        f"min_wal_size={min_wal}",
        "checkpoint_completion_target=0.9",
        "checkpoint_timeout=15min",
        f"random_page_cost={random_page_cost}",
        f"effective_io_concurrency={effective_io}",
        f"maintenance_io_concurrency={maintenance_io}",
        "jit=off",
        "listen_addresses=*",
    ]
    if io_method == "worker":
        idx = settings.index(f"io_method={io_method}") + 1
        settings.insert(idx, f"io_workers={io_workers}")

    if topo.numa_nodes > 1:
        rationale["numa"] = (
            f"{topo.numa_nodes} NUMA nodes detected; prefer OS interleaving "
            "(numactl --interleave=all) for the postmaster when running "
            "outside Docker. GUCs cannot bind memory to nodes."
        )

    rationale["memory"] = (
        f"shared_buffers={sb_gb}GB ({shared_buffers_frac:.0%} RAM"
        f"{', tmpfs-cap' if storage == 'tmpfs' else ''}), "
        f"effective_cache_size={ecs_gb}GB, work_mem={work_mem_mb}MB, "
        f"maintenance_work_mem={maint_mb}MB, wal_buffers={wal_buffers}"
    )
    rationale["parallel"] = (
        f"max_worker_processes={max_worker_processes}, "
        f"max_parallel_workers={max_parallel_workers} (physical CPUs), "
        f"per_gather={parallel_per_gather}, "
        f"maintenance={parallel_maint}, autovacuum={autovac_workers}"
    )
    rationale["empirical"] = (
        "F32ams_v6 vu32/tmpfs sweep: best +19.9% NOPM "
        "(sb≈16–24GB, io_uring, work_mem=16MB, per_gather=0, "
        "wal_buffers=16MB, lz4); vu4 verify +4.3–4.6% vs published"
    )

    return PgGucPlan(
        settings=settings,
        shared_buffers_gb=sb_gb,
        effective_cache_size_gb=ecs_gb,
        topology=topo,
        rationale=rationale,
    )


if __name__ == "__main__":
    plan = tune_pg_gucs()
    import json

    print(json.dumps(plan.to_dict(), indent=2))
    print("\n# docker -c args:")
    print(" ".join(plan.docker_c_args))
