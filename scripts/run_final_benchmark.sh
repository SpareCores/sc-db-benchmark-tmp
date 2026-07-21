#!/bin/bash
# Run the full run3 matrix on one host (baseline + tuned, disk + tmpfs, hammerdb + benchbase).
# Usage: SKU_NAME=Standard_F32ams_v6 ./run_final_benchmark.sh
# Skips suites that already have a complete results.csv (all rungs ok).
# Continues the matrix even if a suite returns non-zero.
set -uo pipefail

: "${SKU_NAME:?set SKU_NAME (e.g. Standard_F32ams_v6)}"
cd "$(dirname "$0")"
OUT="/bench/run3/${SKU_NAME}"
LOG="/bench/run3_${SKU_NAME}.log"
COMMON="--skip-raw --skip-pull"
FAILED=0

suite_complete() {
  local dir=$1 expected=$2
  python3 - "$dir" "$expected" <<'PY'
import csv, sys
from pathlib import Path
d, expected = Path(sys.argv[1]), int(sys.argv[2])
p = d / "results.csv"
if not p.exists():
    raise SystemExit(1)
rows = list(csv.DictReader(p.open()))
ok = [r for r in rows if not (r.get("error") or "").strip()]
raise SystemExit(0 if len(ok) >= expected else 1)
PY
}

run_suite() {
  local bench=$1 storage=$2 tune=$3
  local extra="" dir="${OUT}/${bench}_${storage}_${tune}"
  local expected=5
  [[ "$bench" == benchbase ]] && expected=10
  [[ "$storage" == tmpfs ]] && extra="--pg-tmpfs"
  [[ "$tune" == tuned ]] && extra="$extra --pg-tune-host"

  if suite_complete "$dir" "$expected"; then
    echo "===== $(date -u +%FT%TZ) SKIP ${bench} ${storage} ${tune} (complete) =====" | tee -a "$LOG"
    return 0
  fi

  echo "===== $(date -u +%FT%TZ) START ${bench} ${storage} ${tune} -> ${dir} =====" | tee -a "$LOG"
  # Wipe partial/failed output so we do not append to a broken CSV.
  rm -rf "$dir"
  mkdir -p "$dir"
  local rc=0
  if [[ "$bench" == hammerdb ]]; then
    python3 -u run_wh_sizing_eval.py $extra --out-dir "$dir" $COMMON 2>&1 | tee -a "$LOG" || rc=${PIPESTATUS[0]}
  else
    python3 -u run_benchbase_sizing_eval.py $extra --out-dir "$dir" $COMMON 2>&1 | tee -a "$LOG" || rc=${PIPESTATUS[0]}
  fi
  if [[ $rc -ne 0 ]]; then
    echo "===== $(date -u +%FT%TZ) FAIL ${bench} ${storage} ${tune} rc=${rc} =====" | tee -a "$LOG"
    FAILED=$((FAILED + 1))
  else
    echo "===== $(date -u +%FT%TZ) DONE ${bench} ${storage} ${tune} =====" | tee -a "$LOG"
  fi
}

mkdir -p "$OUT"
{
  echo "run3 final benchmark on ${SKU_NAME} started $(date -u +%FT%TZ)"
} | tee -a "$LOG"

for tune in baseline tuned; do
  for storage in disk tmpfs; do
    run_suite hammerdb "$storage" "$tune"
    run_suite benchbase "$storage" "$tune"
  done
done

echo "ALL DONE $(date -u +%FT%TZ) failed_suites=${FAILED}" | tee -a "$LOG"
exit 0
