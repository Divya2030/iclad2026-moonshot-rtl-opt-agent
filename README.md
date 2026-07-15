# ICLAD 2026 — Team T16 "Moonshot" — NVIDIA RTL-Optimization Agent

**Team:** T16 Moonshot · UC Santa Cruz · Cloud Track
**Problem:** NVIDIA RTL optimization (`ICLAD26-NVIDIA-Problems`)

## What the agent is
A **verification-gated LLM optimizer**. A Vertex AI (Gemini) proposer rewrites RTL
to improve PPA; every candidate is put through a gate — **simulation + formal
equivalence + synthesis/STA** — and is adopted **only if it is functionally
correct AND improves the objective**. The golden RTL is never mutated. This
targets the hidden-testcase judging: the agent optimizes aggressively but cannot
ship a functional regression.

Aligns with the problem's stated flow ("build an agent on Vertex AI to interact
with the container env to improve PPA; we extract token/API usage"). LLM calls
and tokens are recorded per run.

## Files (this directory = the agent)
```
opt_agent.py        optimization loop (propose -> gate -> adopt), Vertex + offline backends
run_gate.py         the gate: SIM + EQUIV + PPA, emits verdict JSON (+ synthesis bottleneck)
equiv_check.sh      Yosys-native sequential equivalence (bounded; tiered)
lec_gf2.py          XOR-aware GF(2) linear equivalence checker (for parity/CRC datapaths)
designs/*.json      per-IP config (async_fifo, sha512, prim_crc32)
fixtures/*.sv       per-IP sim wrappers / miters
requirements.txt    Python deps (google-genai)
```

## Environment
Use the official `iclad-dev:v1` Docker image. **Two upstream Dockerfile fixes are
required** (submitted as `Dockerfile.patch`); without them the image does not build:
1. Yosys `make config-clang` was removed at HEAD → pin `v0.50`, build `CONFIG=gcc`.
2. `ENV VERILATOR_ROOT=/usr/local/share/verilator` misinstalls Verilator → remove it.

ASAP7 techlib per `ENV_PREPARATION.md`; `sv2v` on `PATH` (in the image).

## Reproduce
```bash
# 0. clone THIS agent repo (contains the agent + Dockerfile.patch)
git clone https://github.com/Divya2030/iclad2026-moonshot-rtl-opt-agent.git agent

# 1. official problem repo + our two Dockerfile fixes, then build the image
git clone --recurse-submodules \
    https://github.com/ICLAD-Hackathon/ICLAD26-NVIDIA-Problems.git
git -C ICLAD26-NVIDIA-Problems apply "$PWD/agent/Dockerfile.patch"
docker build -t iclad-dev:v1 ICLAD26-NVIDIA-Problems

# 2. ASAP7 techlib (see ENV_PREPARATION.md)
cd ICLAD26-NVIDIA-Problems
git clone https://github.com/The-OpenROAD-Project/asap7sc7p5t_28.git techlib/asap7sc7p5t_28
( cd techlib/asap7sc7p5t_28/LIB/NLDM && for f in *.lib.7z; do 7z x -y "$f"; done )

# 3. drop the agent into async_fifo/verif_gate/ (it resolves designs relative
#    to the NVIDIA-Problems root) and install deps
mkdir -p async_fifo/verif_gate && cp -r ../agent/* async_fifo/verif_gate/
pip install -r async_fifo/verif_gate/requirements.txt

# --- run everything below inside the container (or a host with the tools) ---
# docker run --rm -it -v "$PWD:/workspace" -w /workspace iclad-dev:v1

# 4. sanity: gate the golden RTL of any IP
python3 async_fifo/verif_gate/run_gate.py \
    --config async_fifo/verif_gate/designs/prim_crc32.json \
    --candidate opentitan/hw/ip/prim/rtl --json verdict.json

# 5. autonomous optimizer (needs a Vertex AI Express key)
export EXPRESS_MODE_KEY=...
python3 async_fifo/verif_gate/opt_agent.py \
    --config async_fifo/verif_gate/designs/prim_crc32.json \
    --backend vertex --model gemini-3-flash-preview \
    --objective area --iters 8
# -> writes best RTL + agent_summary.json (adopted changes, PPA, usage: calls/tokens)
```
Requires on PATH: `yosys`, `iverilog`/`vvp`, `sta`, `sv2v` (all in `iclad-dev:v1`);
`ASAP7_LIB_DIR` defaults to the Docker `/workspace/techlib/...` path (override for host runs).
Offline check without a key: add `--backend mock` to step 5.

## Adding an IP
Drop a `designs/<ip>.json` (top module, RTL files, sim mode, synth flow, equiv);
no code change. Validated on **async_fifo**, **sha512**, **prim_crc32**
(Verilog + SystemVerilog; SVUT / Icarus-TB / verilator-style pre-DV sim; Yosys
`run_syn.sh` and `syn.tcl`+sv2v flows).

## Results summary
See `PRESENTATION.md`. Gated, correctness-preserving wins include prim_crc32
area −3.19% (equiv-proven) and timing WNS −282 → −9 ps (sim-verified via the
tiered miter). Autonomous Vertex-run numbers: see `agent_summary.json` from step 5.

## Notes / honesty
- Equivalence is **tiered**: formal proof where it converges; for XOR/parity-heavy
  rewrites (SAT-hard) a high-vector **simulation miter** provides `sim-verified`
  confidence (explicitly weaker than `proven`); a mismatch is `refuted`/rejected.
- No functionally-incorrect rewrite is ever adopted, at any confidence tier.
