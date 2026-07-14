#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# equiv_check.sh — Yosys-native sequential equivalence: golden RTL vs revised.
#
# Generic (design-agnostic): pass the top module and its file list explicitly.
#
# Usage:
#   equiv_check.sh <golden_dir> <revised_dir> <top> <comma,separated,files> [defines]
#     <defines>  optional, space-separated Verilog defines, e.g. "DSIZE=32 ASIZE=4"
#
# Both dirs must contain every file in the comma-separated list (basenames).
#
# Exit 0  => PROVEN equivalent (equiv_status -assert passed).
# Exit !=0 => NOT proven (truly inequivalent OR the inductive proof did not
#             converge). Caller MUST treat non-zero as "do not trust", never a pass.
#
# Uses only stock-Yosys passes (bundled SAT + ABC) — no SymbiYosys / SMT solver,
# so it runs in the ICLAD eval Docker as-is. For large multi-cycle designs the
# inductive proof can be slow or fail to converge; wrap the call in `timeout`
# and fall back to the simulation gate when it does not close.
# ---------------------------------------------------------------------------
set -euo pipefail

GOLDEN_DIR="${1:?need golden rtl dir}"
REVISED_DIR="${2:?need revised rtl dir}"
TOP="${3:?need top module}"
FILES_CSV="${4:?need comma-separated file list}"
DEFINES="${5:-}"

IFS=',' read -r -a FILES <<< "$FILES_CSV"

DEF=""
for d in $DEFINES; do DEF+=" -D$d"; done

gold_files=""; gate_files=""
for f in "${FILES[@]}"; do
    [[ -f "$GOLDEN_DIR/$f"  ]] || { echo "[equiv] missing golden  $GOLDEN_DIR/$f"  >&2; exit 2; }
    [[ -f "$REVISED_DIR/$f" ]] || { echo "[equiv] missing revised $REVISED_DIR/$f" >&2; exit 2; }
    gold_files+=" $GOLDEN_DIR/$f"
    gate_files+=" $REVISED_DIR/$f"
done

# Per-design prep: elaborate, flatten, lower memory to logic, sync async resets.
# read_verilog's SystemVerilog flag is `-sv` (NOT `-sv2012`).
read_prep() {  # $1 = file list
    echo "read_verilog -sv $DEF $1"
    echo "hierarchy -check -top $TOP"
    echo "prep -flatten -top $TOP"
    echo "memory_map"
    echo "async2sync"
    echo "opt -full"
}

# Run in SCRIPT mode so an ERROR aborts with non-zero exit (piping to stdin
# would put Yosys in interactive mode where errors do not abort).
YS_FILE="$(mktemp --suffix=.ys)"
trap 'rm -f "$YS_FILE"' EXIT
{
    echo "# ---- golden ----"
    read_prep "$gold_files"
    echo "design -stash gold"
    echo "# ---- revised ----"
    read_prep "$gate_files"
    echo "design -stash gate"
    echo "# ---- miter + prove ----"
    echo "design -copy-from gold -as gold $TOP"
    echo "design -copy-from gate -as gate $TOP"
    echo "equiv_make gold gate equiv"
    echo "hierarchy -top equiv"
    echo "prep -flatten -top equiv"
    echo "opt -full"
    echo "equiv_simple  -seq 10"
    echo "equiv_induct  -seq 50"
    echo "equiv_status  -assert"
} > "$YS_FILE"

# Bound the (potentially SAT-hard, e.g. XOR/parity) proof. Exit codes:
#   0   -> PROVEN equivalent
#   124 -> INCONCLUSIVE (timed out; caller may fall back to a sim miter)
#   *   -> NOT proven (equiv_status -assert failed / error)
timeout "${EQUIV_TIMEOUT:-300}" yosys -ql equiv_check.log "$YS_FILE"
rc=$?
if [ "$rc" -eq 0 ]; then
    echo "[equiv] PROVEN equivalent (top=$TOP)"
elif [ "$rc" -eq 124 ]; then
    echo "[equiv] INCONCLUSIVE: proof exceeded EQUIV_TIMEOUT=${EQUIV_TIMEOUT:-300}s (top=$TOP)"
else
    echo "[equiv] NOT proven (top=$TOP, yosys rc=$rc)"
fi
exit "$rc"
