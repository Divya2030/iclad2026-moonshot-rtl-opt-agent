# ICLAD 2026 — Team T16 "Moonshot" — NVIDIA RTL Optimization Agent

**Team:** T16 Moonshot · UC Santa Cruz · Cloud Track
**Target Problem:** NVIDIA RTL Optimization (`ICLAD26-NVIDIA-Problems`)

---

## Overview

The Moonshot RTL Optimization Agent is a **verification-gated LLM optimizer** that automatically rewrites SystemVerilog/Verilog RTL designs to optimize Power, Performance, and Area (PPA) metrics.

Driven by Google Gemini (`gemini-3.6-flash`), the proposer generates candidate RTL optimizations which are rigorously evaluated inside a closed-loop verification gate consisting of:

1. **Functional Simulation:** Verification via Icarus Verilog testbenches.
2. **Formal Equivalence Checking (LEC):** Yosys-native sequential equivalence proving and GF(2) linear checking.
3. **Logic Synthesis & Timing Analysis:** Yosys synthesis and OpenSTA timing/power analysis against the ASAP7 7.5t PDK.

Candidate rewrites are adopted **only if they are proven functionally correct AND improve the objective metric without regressing timing**. Golden source files are never mutated directly.

---

## Verified Result: prim_crc32 (live 8-iteration run)

Command: `--backend vertex --model gemini-3.6-flash --objective area --iters 8`

Across multiple live runs, the agent consistently discovers and adopts the same class of correctness-preserving rewrite: replacing the 256-entry CRC32 lookup table with a GF(2)-linear XOR formulation of the CRC basis constants. Because Gemini's exact phrasing of the rewrite (masked-XOR vs. sequential-accumulate form) varies call to call, the specific adopted area differs slightly between runs (observed range: **80.2–91.0 um² from a 95.46984 um² baseline, a 4–16% area reduction**), but every adopted candidate is independently re-verified by the gate — formal or simulation-miter equivalence proof, functional simulation pass, and no timing regression — before being accepted.

Every run writes:
- `opt_results_prim_crc32/agent_summary.json` — full iteration history, baseline/final values, token usage
- `opt_results_prim_crc32/BEST_ITERATION.json` — a definitive pointer to which iteration produced the adopted result, avoiding any ambiguity from shared/overwritten synthesis scratch files
- `opt_results_prim_crc32/best_snapshots/iter_NN/` — an immutable snapshot of the exact adopted RTL and verdict JSON for that iteration

Typical run behavior: iteration 0 adopts a real improvement; most subsequent iterations are correctly **REJECTED** by the gate (`timing_regressed`, `no_objective_improvement`, or `sim_failed`), demonstrating the gate enforces real constraints rather than rubber-stamping every proposal.

---

## Verified Baseline: sha512

- **Baseline:** 3939.12234 um² (35.36% sequential / 64.64% combinational), functional simulation passed, formal equivalence verified.
- sha512 is a substantially larger design than prim_crc32, dominated by register/pipeline state rather than combinational logic. Live optimization attempts correctly reject most proposals (`no_objective_improvement`, `sim_failed`, `equivalence_not_verified`) since reducing register-bound area requires deeper architectural restructuring than the CRC32 case. No incorrect candidate has ever been adopted — the gate holds the line even when no improving rewrite is found.

---

## Key Features

* **Zero-Regression Optimization:** The gate rejects any candidate that regresses timing relative to the current best, even if area improves.
* **Formal Equivalence Proving:** Every adopted candidate is formally proven equivalent to golden RTL (or sim-miter verified, for SAT-hard XOR/parity datapaths) before being scored.
* **Cumulative Search:** Each iteration's candidate is built on top of the currently adopted RTL, not re-derived from golden each time — so the LLM sees and builds on its own prior wins.
* **Live LLM-Driven Search:** Runs against Gemini via the Gemini Developer API (`GEMINI_API_KEY`) or Vertex AI Express (`EXPRESS_MODE_KEY`).
* **Auditable Results:** Every run produces `agent_summary.json`, `BEST_ITERATION.json`, and immutable per-iteration snapshots — no result requires re-reading a shared, overwritable scratch file to interpret.
* **Config-Driven Multi-IP Support:** `designs/*.json` configs exist for 7 RTL blocks (`prim_crc32`, `async_fifo`, `sha512`, `prim_count`, `prim_lfsr`, `prim_arbiter_fixed`, `prim_subreg`). `prim_crc32` is fully verified end-to-end with a live model; `sha512`'s baseline is verified and passes the gate. The remaining configs are provided but do not yet pass the simulation/equivalence gate in this environment — see Known Limitations.

---

## Quickstart: Verified Single-Design Run

```bash
export GEMINI_API_KEY="your_api_key_here"

docker run --rm \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py \
      --config async_fifo/verif_gate/designs/prim_crc32.json \
      --backend vertex --model gemini-3.6-flash \
      --objective area --iters 8 --out opt_results_prim_crc32"
```

After the run, check the authoritative result:
```bash
cat opt_results_prim_crc32/BEST_ITERATION.json
cat opt_results_prim_crc32/agent_summary.json
```

Baseline-only gate check (no LLM calls required):

```bash
docker run --rm -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/run_gate.py \
      --config async_fifo/verif_gate/designs/prim_crc32.json \
      --candidate opentitan/hw/ip/prim/rtl --json verdict.json"
```

---

## Run All 7 Designs Manually (Docker)

`prim_crc32` (full optimization) and `sha512` (baseline gate only) are confirmed passing; the other five currently fail at the simulation/equivalence stage in this environment (see Known Limitations).

```bash
export GEMINI_API_KEY="your_api_key_here"

# prim_crc32 — VERIFIED, full 8-iteration run
docker run --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py --config async_fifo/verif_gate/designs/prim_crc32.json \
      --backend vertex --model gemini-3.6-flash --objective area --iters 8 --out opt_results_prim_crc32"

# sha512 — baseline verified, live optimization attempted
docker run --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py --config async_fifo/verif_gate/designs/sha512.json \
      --backend vertex --model gemini-3.6-flash --objective area --iters 8 --out opt_results_sha512"

# async_fifo
docker run --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py --config async_fifo/verif_gate/designs/async_fifo.json \
      --backend vertex --model gemini-3.6-flash --objective area --iters 8 --out opt_results_async_fifo"

# prim_count
docker run --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py --config async_fifo/verif_gate/designs/prim_count.json \
      --backend vertex --model gemini-3.6-flash --objective area --iters 8 --out opt_results_prim_count"

# prim_lfsr
docker run --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py --config async_fifo/verif_gate/designs/prim_lfsr.json \
      --backend vertex --model gemini-3.6-flash --objective area --iters 8 --out opt_results_prim_lfsr"

# prim_arbiter_fixed
docker run --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py --config async_fifo/verif_gate/designs/prim_arbiter_fixed.json \
      --backend vertex --model gemini-3.6-flash --objective area --iters 8 --out opt_results_prim_arbiter_fixed"

# prim_subreg
docker run --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py --config async_fifo/verif_gate/designs/prim_subreg.json \
      --backend vertex --model gemini-3.6-flash --objective area --iters 8 --out opt_results_prim_subreg"
```

---

## Run All Designs — One-Shot Script

```bash
export GEMINI_API_KEY="your_api_key_here"
./run_all_designs_oneshot.sh
```

---

## Repository Structure
opt_agent.py Optimization loop (propose -> gate -> adopt); cumulative search
with per-iteration best-snapshot tracking
run_gate.py Verification gate: SIM + EQUIV + PPA evaluation & verdict JSON
equiv_check.sh Yosys-native sequential equivalence checker
lec_gf2.py XOR-aware GF(2) linear equivalence checker
gen_crc_flat.py Symbolic GF(2) datapath flattening for CRC-style designs
designs/.json Per-IP configuration files (7 designs; see Known Limitations)
fixtures/.sv Per-IP simulation wrappers and miters
Dockerfile.patch Required build patch for official container environment
requirements.txt Python dependencies (google-genai)
run_all_designs_oneshot.sh Master pipeline execution script


Each optimization run also writes, under its `--out` directory:

agent_summary.json full run history, baseline/final values, token usage
BEST_ITERATION.json authoritative pointer to the adopted iteration
best_snapshots/iter_NN/ immutable RTL + verdict snapshot for each adopted iteration


---

## Known Limitations

- Only `prim_crc32` has completed live 8-iteration optimization runs with adopted, gate-verified improvements. The exact adopted area varies slightly run to run (observed 80.2–91.0 um² from a 95.47 um² baseline) because Gemini's proposed rewrite phrasing is not identical every call — each result is independently re-verified, not cached or hardcoded.
- `sha512` has a confirmed passing baseline (functional sim + formal equivalence); live optimization runs correctly reject most or all candidates given the design's register-dominated bottleneck, without ever adopting an unverified result.
- `async_fifo`, `prim_count`, `prim_lfsr`, `prim_arbiter_fixed`, and `prim_subreg` currently fail at the simulation or equivalence stage of the gate (`sim_failed` / `equivalence_not_verified`) due to testbench/harness mismatches in this environment, not due to the optimization logic itself. These configs are included to show the intended multi-IP design of the system, but are not claimed as verified results.
- `run_all_designs_oneshot.sh` is a batch runner across all 7 configs; expect it to succeed on `prim_crc32` and `sha512`'s baseline, and report gate rejections on the others until their testbench wiring is fixed.

## Verification & Adoption Policy

- A candidate is adopted **only if**: (1) simulation passes, (2) formal or simulation-miter equivalence is verified, (3) timing does not regress vs. the current best, and (4) the objective metric strictly improves.
- Golden RTL is never overwritten; all candidates are generated in scratch working directories, and each adopted iteration is additionally frozen in `best_snapshots/` so results cannot be confused with stale scratch data from a later or different run.

