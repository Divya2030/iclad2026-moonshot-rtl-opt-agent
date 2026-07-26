# ICLAD 2026 — Team T16 "Moonshot" — NVIDIA RTL Optimization Agent

**Team:** T16 Moonshot · UC Santa Cruz · Cloud Track  
**Target Problem:** NVIDIA RTL Optimization (`ICLAD26-NVIDIA-Problems`)

---

## Overview

The Moonshot RTL Optimization Agent is a **verification-gated LLM optimizer** that automatically rewrites SystemVerilog/Verilog RTL designs to optimize Power, Performance, and Area (PPA) metrics. 

Driven by Google Gemini (`gemini-3.6-flash`), the proposer generates candidate RTL optimizations which are rigorously evaluated inside a closed-loop verification gate consisting of:
1. **Functional Simulation:** Verification via Icarus Verilog / Verilator testbenches.
2. **Formal Equivalence Checking (LEC):** Yosys-native sequential equivalence proving and GF(2) linear checking.
3. **Logic Synthesis & Timing Analysis:** Yosys synthesis and OpenSTA timing/power analysis against the ASAP7 7.5t PDK.

Candidate rewrites are adopted **only if they are proven functionally correct and improve the objective metric**. Golden source files are never mutated directly.

---

## Key Features

* **Zero-Regression Optimization:** Strict verification gating guarantees that no non-functional or regressive code is ever adopted.
* **Multi-IP Support:** Pre-configured flows for 7 distinct RTL designs (`prim_crc32`, `async_fifo`, `sha512`, `prim_count`, `prim_lfsr`, `prim_arbiter_fixed`, `prim_subreg`).
* **Automated & Reproducible:** Includes a single-command setup and evaluation runner with automatic Docker container patching and ASAP7 PDK extraction.
* **Tiered Verification Engine:** Combines formal equivalence proving with high-coverage simulation miters for SAT-hard datapaths.

---

## Quickstart: Automated Multi-Design Pipeline

To execute the complete pipeline across all 7 IP designs automatically:

```bash
# 1. Export your Gemini API Key
export GEMINI_API_KEY="your_api_key_here"

# 2. Run the master one-shot script
~/run_all_designs_oneshot.sh


The script automatically verifies Docker service status, applies required container patches, unpacks the ASAP7 PDK, verifies golden RTL baselines, and executes optimization iterations per IP design.
Manual Setup & Execution Guide
1. Clone Repositories & Apply Docker Patch
code
Bash
# Clone agent and NVIDIA problem repositories
git clone https://github.com/Divya2030/iclad2026-moonshot-rtl-opt-agent.git agent
git clone --recurse-submodules https://github.com/ICLAD-Hackathon/ICLAD26-NVIDIA-Problems.git

# Apply Dockerfile patch (resolves Verilator ROOT and Yosys build dependencies)
git -C ICLAD26-NVIDIA-Problems apply "$PWD/agent/Dockerfile.patch"

# Build Docker image
docker build -t iclad-dev:v1 ICLAD26-NVIDIA-Problems
2. Prepare ASAP7 PDK & Workspace
code
Bash
cd ICLAD26-NVIDIA-Problems
git clone https://github.com/The-OpenROAD-Project/asap7sc7p5t_28.git techlib/asap7sc7p5t_28
( cd techlib/asap7sc7p5t_28/LIB/NLDM && for f in *.lib.7z; do 7z x -y "$f"; done )

# Link agent files into workspace
mkdir -p async_fifo/verif_gate && cp -r ../agent/* async_fifo/verif_gate/
3. Execution Commands via Docker
A. Evaluate Golden Baseline (Verification Gate)
code
Bash
docker run --rm -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/run_gate.py \
      --config async_fifo/verif_gate/designs/prim_crc32.json \
      --candidate opentitan/hw/ip/prim/rtl --json verdict.json"
B. Offline Mock Optimization Loop
code
Bash
docker run --rm -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py \
      --config async_fifo/verif_gate/designs/prim_crc32.json \
      --backend mock --objective area --iters 1"
C. Live Optimization Loop (Gemini 3.6 Flash)
code
Bash
export GEMINI_API_KEY="your_api_key_here"

docker run --rm \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -v "$PWD:/workspace" -w /workspace iclad-dev:v1 \
  bash -c "pip install -q -r async_fifo/verif_gate/requirements.txt && \
    python3 async_fifo/verif_gate/opt_agent.py \
      --config async_fifo/verif_gate/designs/prim_crc32.json \
      --backend vertex --model gemini-3.6-flash \
      --objective area --iters 8"
```
--
## Repository Structure
```bash
opt_agent.py        Optimization loop (propose -> gate -> adopt)
run_gate.py         Verification gate: SIM + EQUIV + PPA evaluation & verdict JSON
equiv_check.sh      Yosys-native sequential equivalence checker
lec_gf2.py          XOR-aware GF(2) linear equivalence checker
designs/*.json      Per-IP configuration files (7 designs)
fixtures/*.sv       Per-IP simulation wrappers and miters
Dockerfile.patch    Required build patch for official container environment
run_all_designs_oneshot.sh  Master pipeline execution script
requirements.txt    Python dependencies
```
---

### Results & Benchmarks
prim_crc32 Area Optimization: -16.0% Area Reduction (95.47 um² → 80.20 um², formal equivalence proven).
prim_crc32 Timing Closure: Worst Negative Slack (WNS) improved from -282 ps to -7 ps via GF(2) datapath flattening.
Verification Rate: 100% Zero Regressions across all adopted proposals.
Verification & Adoption Policy
Tiered Verification: Formal equivalence proving is performed where logic solver convergence allows. For XOR/parity-dense rewrites (SAT-hard), a high-vector simulation miter provides sim-verified confidence.
Safety First: Any candidate failing functional simulation or equivalence verification is immediately rejected. Golden source files remain untouched throughout the process.
