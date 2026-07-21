#!/usr/bin/env python3
"""Generate RESULTS.md from run3 CSV trees."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN3 = ROOT / "run3"
SKUS = ("Standard_E16as_v6", "Standard_F16ams_v6", "Standard_F32ams_v6")
SKU_SHORT = {
    "Standard_E16as_v6": "E16",
    "Standard_F16ams_v6": "F16",
    "Standard_F32ams_v6": "F32",
}
VUS = (4, 8, 16, 32, 64)


def load_csv(path: Path) -> list[dict]:
    p = path / "results.csv"
    if not p.exists():
        return []
    return list(csv.DictReader(p.open()))


def ok_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not (r.get("error") or "").strip()]


def peak(rows: list[dict], metric: str) -> tuple[int, dict]:
    good = [r for r in ok_rows(rows) if r.get(metric)]
    if not good:
        return 0, {}
    best = max(good, key=lambda r: float(r[metric]))
    return int(float(best[metric])), best


def fmt(n: int | float | str) -> str:
    return f"{int(float(n)):,}"


def delta_pct(new: int, old: int) -> str:
    if not old:
        return "—"
    return f"{100 * (new - old) / old:+.1f}%"


def hammer(sku: str, storage: str, tune: str) -> list[dict]:
    return load_csv(RUN3 / sku / f"hammerdb_{storage}_{tune}")


def bench(sku: str, storage: str, tune: str) -> list[dict]:
    return load_csv(RUN3 / sku / f"benchbase_{storage}_{tune}")


def by_vu(rows: list[dict], metric: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for r in ok_rows(rows):
        if r.get(metric) and r.get("run_vus"):
            out[int(r["run_vus"])] = float(r[metric])
    return out


def hammer_ladder_table(storage: str, tune: str) -> str:
    lines = [
        "| SKU | vCPUs | VU | WH | VU/vCPU | NOPM | TPM | NOPM/VU | Build (s) |",
        "|-----|-------|----|----|---------|------|-----|---------|-----------|",
    ]
    for sku in SKUS:
        for r in ok_rows(hammer(sku, storage, tune)):
            vu = int(r["run_vus"])
            ncpu = int(r["ncpus"])
            lines.append(
                f"| {SKU_SHORT[sku]} | {ncpu} | {vu} | {r['warehouses']} | "
                f"{vu / ncpu:g} | {fmt(r['nopm'])} | {fmt(r['tpm'])} | "
                f"{r['nopm_per_vu']} | {r['build_seconds']} |"
            )
    return "\n".join(lines)


def bench_ladder_table(storage: str, tune: str, workload: str) -> str:
    lines = [
        "| SKU | vCPUs | VU | SF | TPM | TPS | p95 (ms) | Load (s) |",
        "|-----|-------|----|----|-----|-----|----------|----------|",
    ]
    for sku in SKUS:
        for r in ok_rows(bench(sku, storage, tune)):
            if r.get("workload") != workload:
                continue
            lines.append(
                f"| {SKU_SHORT[sku]} | {r['ncpus']} | {r['run_vus']} | {r['scalefactor']} | "
                f"{fmt(r['tpm'])} | {r['tps']} | {r.get('latency_p95_ms', '')} | "
                f"{r['load_seconds']} |"
            )
    return "\n".join(lines)


# Default Mermaid xyChart palette order (used for markdown legends;
# GitHub's Mermaid often still omits the native legend UI).
PLOT_COLORS = ("#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948")


def mermaid_lines(
    series: dict[str, dict[int, float]],
    title: str,
    ylabel: str,
    ymax: int | None = None,
) -> str:
    if not any(series.values()):
        return ""
    if ymax is None:
        mx = max((v for pts in series.values() for v in pts.values()), default=1)
        ymax = int(mx * 1.1) // 1000 * 1000 + 1000
    labels = ", ".join(f"vu{v}" for v in VUS)
    names = list(series.keys())
    palette = ", ".join(PLOT_COLORS[: max(len(names), 1)])
    lines = [
        "```mermaid",
        "---",
        "config:",
        "  xyChart:",
        "    showLegend: true",
        f'    plotColorPalette: "{palette}"',
        "---",
        "xychart-beta",
        f'    title "{title}"',
        f"    x-axis [{labels}]",
        f'    y-axis "{ylabel}" 0 --> {ymax}',
    ]
    for name, pts in series.items():
        vals = ", ".join(str(int(pts.get(v, 0))) for v in VUS)
        lines.append(f'    line "{name}" [{vals}]')
    lines.append("```")
    # Portable legend: GitHub / older Mermaid often ignore showLegend.
    legend = " · ".join(f"**{n}**" for n in names)
    lines.append("")
    lines.append(f"_Legend (line order = first→last color): {legend}_")
    return "\n".join(lines)


def mermaid_bars(pairs: list[tuple[str, int]], title: str, ylabel: str) -> str:
    if not pairs:
        return ""
    ymax = max(v for _, v in pairs) * 11 // 10
    ymax = ymax // 1000 * 1000 + 1000
    labels = ", ".join(n for n, _ in pairs)
    vals = ", ".join(str(v) for _, v in pairs)
    return "\n".join(
        [
            "```mermaid",
            "xychart-beta",
            f'    title "{title}"',
            f"    x-axis [{labels}]",
            f'    y-axis "{ylabel}" 0 --> {ymax}',
            f"    bar [{vals}]",
            "```",
            "",
            "_Each bar is labeled on the x-axis._",
        ]
    )


def sweep_section() -> list[str]:
    """Document the F32 vu32/tmpfs GUC sweep that calibrated pg_tune_gucs."""
    sweep_dir = ROOT / "guc_sweep_f32"
    csv_path = sweep_dir / "sweep.csv"
    summary_path = sweep_dir / "summary.json"
    vu4_path = sweep_dir / "vu4_summary.json"
    if not csv_path.exists():
        return []

    rows = list(csv.DictReader(csv_path.open()))
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    vu4 = json.loads(vu4_path.read_text()) if vu4_path.exists() else {}
    baseline_ref = int(summary.get("baseline_nopm") or 1_131_026)
    best = summary.get("best") or {}

    # Phase groupings for a readable table (primary axes of the sweep).
    phases: list[tuple[str, list[str]]] = [
        ("A/B baseline & first host-tune", ["A_baseline", "B_host_tune_tmpfs"]),
        ("C shared_buffers on tmpfs", [f"C_sb{n}" for n in (8, 16, 24, 32, 48)]),
        ("D io_method × shared_buffers", [
            "D_sb16_io_uring", "D_sb16_worker", "D_sb62_io_uring", "D_sb62_worker",
        ]),
        ("E/F WAL", ["E_walcomp_off", "F_walbuf_16MB", "F_walbuf_256MB"]),
        ("G parallel gather", ["G_pergather_0"]),
        ("H work_mem (+ gather=0)", ["H_workmem_8MB", "H_workmem_16MB", "H_workmem_64MB"]),
        ("I effective_io_concurrency", ["I_eic_128", "I_eic_200"]),
        ("J maintenance-heavy", ["J_maint_heavy"]),
    ]
    by_name = {r["name"]: r for r in rows if r.get("nopm")}

    out: list[str] = [
        "## GUC sweep (F32ams_v6, HammerDB vu32 / tmpfs)",
        "",
        "Calibration run that produced the OLTP defaults in "
        "[`pg_tune_gucs.py`](scripts/pg_tune_gucs.py). Host: Azure "
        "`Standard_F32ams_v6` (32 vCPU / 252 GiB), HammerDB TPROC-C **32 VU / "
        "160 WH**, Postgres data on **tmpfs**, privileged Debian `postgres:18`. "
        f"Reference baseline (archived run2-tmpfs F32 vu32): **{fmt(baseline_ref)} NOPM**. "
        "Script: [`guc_sweep_vu32.py`](scripts/guc_sweep_vu32.py).",
        "",
        "### Headline",
        "",
        "| Item | Value |",
        "|------|-------|",
        f"| Best config | `{best.get('name', '—')}` |",
        f"| Best NOPM | {fmt(best.get('nopm', 0))} "
        f"({delta_pct(int(best.get('nopm') or 0), baseline_ref)} vs ref) |",
        f"| Live A_baseline (same session) | "
        f"{fmt(by_name.get('A_baseline', {}).get('nopm', 0))} |",
        f"| Experiments | {summary.get('n_ok', len(rows))}/"
        f"{summary.get('n_total', len(rows))} ok |",
        "",
        "Winners folded into `--pg-tune-host` for OLTP/tmpfs:",
        "",
        "- `shared_buffers` ≈ **10% RAM** (cap **24 GiB**) — large buffers double-buffer vs tmpfs",
        "- `io_method=io_uring` (needs privileged + liburing image)",
        "- `work_mem=16MB`, `max_parallel_workers_per_gather=0`",
        "- `wal_buffers=16MB`, `wal_compression=lz4`, `jit=off`",
        "",
        "### Full matrix (Δ vs archived run2-tmpfs F32 vu32)",
        "",
        "| Phase | Config | SB | NOPM | Δ vs ref | Notes |",
        "|-------|--------|----|------|----------|-------|",
    ]

    notes_map = {
        "A_baseline": "run2-style GUCs (25% SB, no io_uring)",
        "B_host_tune_tmpfs": "early host-tune (still 62 GB SB)",
        "C_sb24": "best SB-only on tmpfs",
        "D_sb16_io_uring": "io_uring ≫ worker at SB=16",
        "D_sb16_worker": "worker AIO baseline",
        "F_walbuf_16MB": "16 MB WAL buffers beat 64/256",
        "G_pergather_0": "disable parallel gather for OLTP",
        "H_workmem_16MB": "**sweep champion**",
        "H_workmem_8MB": "close second",
        "I_eic_200": "eic=200 strong with gather=0",
        "J_maint_heavy": "8 GB maint mem not helpful at runtime",
    }

    for phase, names in phases:
        for name in names:
            r = by_name.get(name)
            if not r:
                continue
            nopm = int(r["nopm"])
            out.append(
                f"| {phase} | `{name}` | {r.get('shared_buffers_gb', '')} GB | "
                f"{fmt(nopm)} | {delta_pct(nopm, baseline_ref)} | "
                f"{notes_map.get(name, '')} |"
            )
            phase = ""  # only label first row of a phase group

    # Compact bar chart of key points
    key = [
        ("A_baseline", "baseline"),
        ("C_sb24", "sb24"),
        ("D_sb16_io_uring", "io_uring"),
        ("F_walbuf_16MB", "wal16"),
        ("G_pergather_0", "gather0"),
        ("H_workmem_16MB", "wm16"),
        ("I_eic_200", "eic200"),
    ]
    bars = [
        (label, int(by_name[name]["nopm"]))
        for name, label in key
        if name in by_name
    ]
    out.append("")
    out.append(mermaid_bars(bars, "F32 vu32/tmpfs GUC sweep — selected NOPM", "NOPM"))
    out.append("")

    # vu4 verify
    v4 = (vu4.get("vu4_verify") or {})
    if v4:
        out.extend(
            [
                "### vu4 regression check",
                "",
                "Same winning settings at **4 VU / 20 WH** vs archived run2-tmpfs "
                f"F32 vu4 ({fmt(vu4.get('published_vu4_nopm', 269933))} NOPM):",
                "",
                "| Config | NOPM | Δ vs archived vu4 |",
                "|--------|------|-------------------|",
            ]
        )
        for key_name, row in v4.items():
            if not isinstance(row, dict) or not row.get("nopm"):
                continue
            out.append(
                f"| `{row.get('name', key_name)}` | {fmt(row['nopm'])} | "
                f"{row.get('delta_vs_published_vu4_pct', '—')}% |"
            )
        out.append("")
        out.append(
            "No regression at low concurrency (≈ +4% vs archived vu4)."
        )
        out.append("")

    out.extend(
        [
            "### Takeaway",
            "",
            "On **tmpfs**, avoid oversized `shared_buffers`, prefer **io_uring**, "
            "keep OLTP `work_mem` modest, and turn **parallel gather off**. "
            "Those settings are what `--pg-tune-host` applies today for "
            "`storage=tmpfs` (disk still uses 25% `shared_buffers` + the same "
            "OLTP worker/WAL knobs).",
            "",
        ]
    )
    return out


def main() -> None:
    out: list[str] = [
        "# Database benchmark results — run3",
        "",
        "Baseline (25%/75% buffers, default I/O) vs host-tuned GUCs "
        "([`pg_tune_gucs.py`](scripts/pg_tune_gucs.py), `io_uring` when available). "
        "All containers `--privileged`. HammerDB TPROC-C + BenchBase wikipedia/ycsb. "
        "Disk = anonymous Docker volume on Premium_LRS; "
        "tmpfs = `max(16, 50% RAM)` on `/var/lib/postgresql`.",
        "",
        "> **Status:** run3 complete on E16 / F16 / F32 (8 suites × 3 SKUs, all rungs ok).",
        "",
        "## Setup",
        "",
        "| Item | Value |",
        "|------|-------|",
        "| Workloads | HammerDB TPROC-C; BenchBase wikipedia + ycsb |",
        "| VU / terminal ladder | 4, 8, 16, 32, 64 |",
        "| WH / schema sizing | 5 WH/VU (HammerDB); BenchBase SF matched to same footprint |",
        "| Rampup / duration | 2 min / 5 min |",
        "| Postgres | `postgres:18`, `synchronous_commit=off` |",
        "| Baseline GUCs | 25% `shared_buffers`, 75% `effective_cache_size` |",
        "| Tuned GUCs | `--pg-tune-host` (io_uring, OLTP sweep winners) |",
        "| Region | Azure NewZealandNorth |",
        "",
        "| SKU | vCPUs | RAM | baseline SB | tuned SB (disk) | tuned SB (tmpfs) |",
        "|-----|-------|-----|-------------|-----------------|------------------|",
        "| E16as_v6 | 16 | 126 GiB | 31 GB | 31 GB | 12 GB (10% cap) |",
        "| F16ams_v6 | 16 | 126 GiB | 31 GB | 31 GB | 12 GB (10% cap) |",
        "| F32ams_v6 | 32 | 252 GiB | 62 GB | 62 GB | 24 GB (10% cap) |",
        "",
    ]

    # --- Peak HammerDB ---
    out.append("## Peak HammerDB NOPM")
    out.append("")
    out.append("| SKU | Storage | Baseline peak | Tuned peak | Δ |")
    out.append("|-----|---------|---------------|------------|---|")
    peak_bars: list[tuple[str, int]] = []
    for sku in SKUS:
        for storage in ("disk", "tmpfs"):
            b_n, b_r = peak(hammer(sku, storage, "baseline"), "nopm")
            t_n, t_r = peak(hammer(sku, storage, "tuned"), "nopm")
            out.append(
                f"| {SKU_SHORT[sku]} | {storage} | "
                f"{fmt(b_n)} (vu{b_r.get('run_vus', '?')}) | "
                f"{fmt(t_n)} (vu{t_r.get('run_vus', '?')}) | {delta_pct(t_n, b_n)} |"
            )
            peak_bars.append((f"{SKU_SHORT[sku]} {storage} base", b_n))
            peak_bars.append((f"{SKU_SHORT[sku]} {storage} tune", t_n))
    out.append("")
    out.append(mermaid_bars(peak_bars, "Peak HammerDB NOPM by SKU / storage / tune", "NOPM"))
    out.append("")

    # --- Peak BenchBase ---
    out.append("## Peak BenchBase TPM")
    out.append("")
    out.append("| SKU | Storage | WL | Baseline peak | Tuned peak | Δ |")
    out.append("|-----|---------|----|---------------|------------|---|")
    for sku in SKUS:
        for storage in ("disk", "tmpfs"):
            for wl in ("wikipedia", "ycsb"):
                b_rows = [r for r in ok_rows(bench(sku, storage, "baseline")) if r.get("workload") == wl]
                t_rows = [r for r in ok_rows(bench(sku, storage, "tuned")) if r.get("workload") == wl]
                b_n, b_r = peak(b_rows, "tpm")
                t_n, t_r = peak(t_rows, "tpm")
                out.append(
                    f"| {SKU_SHORT[sku]} | {storage} | {wl} | "
                    f"{fmt(b_n)} (vu{b_r.get('run_vus', '?')}) | "
                    f"{fmt(t_n)} (vu{t_r.get('run_vus', '?')}) | {delta_pct(t_n, b_n)} |"
                )
    out.append("")

    # --- HammerDB detailed ---
    for storage in ("disk", "tmpfs"):
        out.append(f"## HammerDB — {storage}")
        out.append("")
        for tune in ("baseline", "tuned"):
            out.append(f"### {tune}")
            out.append("")
            out.append(hammer_ladder_table(storage, tune))
            out.append("")
            series = {
                SKU_SHORT[sku]: by_vu(hammer(sku, storage, tune), "nopm") for sku in SKUS
            }
            out.append(mermaid_lines(series, f"NOPM vs VU ({storage}, {tune})", "NOPM"))
            out.append("")

        # baseline vs tuned at each VU for F32 (most interesting)
        out.append(f"### Baseline vs tuned ({storage})")
        out.append("")
        out.append("| SKU | VU | Baseline NOPM | Tuned NOPM | Δ |")
        out.append("|-----|----|---------------|------------|---|")
        for sku in SKUS:
            b = by_vu(hammer(sku, storage, "baseline"), "nopm")
            t = by_vu(hammer(sku, storage, "tuned"), "nopm")
            for vu in VUS:
                if vu in b and vu in t:
                    out.append(
                        f"| {SKU_SHORT[sku]} | {vu} | {fmt(b[vu])} | {fmt(t[vu])} | "
                        f"{delta_pct(int(t[vu]), int(b[vu]))} |"
                    )
        out.append("")

    # --- BenchBase detailed ---
    for storage in ("disk", "tmpfs"):
        out.append(f"## BenchBase — {storage}")
        out.append("")
        for tune in ("baseline", "tuned"):
            out.append(f"### {tune}")
            out.append("")
            for wl in ("wikipedia", "ycsb"):
                out.append(f"#### {wl}")
                out.append("")
                out.append(bench_ladder_table(storage, tune, wl))
                out.append("")
                series = {}
                for sku in SKUS:
                    rows = [
                        r
                        for r in ok_rows(bench(sku, storage, tune))
                        if r.get("workload") == wl
                    ]
                    series[SKU_SHORT[sku]] = by_vu(rows, "tpm")
                out.append(
                    mermaid_lines(series, f"{wl} TPM vs VU ({storage}, {tune})", "TPM")
                )
                out.append("")

    # --- GUC sweep calibration ---
    out.extend(sweep_section())

    # --- Notes ---
    # Derive a couple of headline deltas for the notes section.
    f32_tmpfs_b, _ = peak(hammer("Standard_F32ams_v6", "tmpfs", "baseline"), "nopm")
    f32_tmpfs_t, _ = peak(hammer("Standard_F32ams_v6", "tmpfs", "tuned"), "nopm")
    f16_tmpfs_b, _ = peak(hammer("Standard_F16ams_v6", "tmpfs", "baseline"), "nopm")
    f16_tmpfs_t, _ = peak(hammer("Standard_F16ams_v6", "tmpfs", "tuned"), "nopm")

    out.extend(
        [
            "## Notes",
            "",
            "- Compare SKUs at the same **VU/vCPU** ratio (and each machine’s peak), "
            "not only at the same absolute VU.",
            "- Tuned GUCs come from [`pg_tune_gucs.py`](scripts/pg_tune_gucs.py): on tmpfs, "
            "`shared_buffers` ≈ 10% RAM (capped); `io_method=io_uring`; "
            "`work_mem=16MB`; `max_parallel_workers_per_gather=0`; `wal_buffers=16MB`.",
            "- Containers always use `--privileged` / `seccomp=unconfined` / "
            "`memlock=-1:-1` (needed for `io_uring`).",
            "- Outputs kept lean (`--skip-raw`): `results.csv` + `meta.json` only.",
            f"- **tmpfs + tuned** lifts HammerDB peak NOPM vs baseline tmpfs "
            f"(F16 {delta_pct(f16_tmpfs_t, f16_tmpfs_b)}, "
            f"F32 {delta_pct(f32_tmpfs_t, f32_tmpfs_b)}). Disk tuned is mixed / "
            "near-flat vs baseline on these SKUs.",
            "- On disk, absolute peaks stay around **vu16**; on tmpfs, F32 peaks at "
            "**vu32** (baseline and tuned).",
            "",
            "## Raw data",
            "",
            "| Path |",
            "|------|",
        ]
    )
    for sku in SKUS:
        for bench_name in ("hammerdb", "benchbase"):
            for storage in ("disk", "tmpfs"):
                for tune in ("baseline", "tuned"):
                    rel = f"run3/{sku}/{bench_name}_{storage}_{tune}/results.csv"
                    if (ROOT / rel).exists():
                        out.append(f"| [`{rel}`]({rel}) |")
    for sweep_rel in (
        "guc_sweep_f32/sweep.csv",
        "guc_sweep_f32/summary.json",
        "guc_sweep_f32/vu4_summary.json",
    ):
        if (ROOT / sweep_rel).exists():
            out.append(f"| [`{sweep_rel}`]({sweep_rel}) |")
    out.append("")

    (ROOT / "RESULTS.md").write_text("\n".join(out) + "\n")
    print(f"Wrote {ROOT / 'RESULTS.md'} ({len(out)} lines)")


if __name__ == "__main__":
    main()
