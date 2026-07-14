# ICLAD 2026 — Team T16 "Moonshot" — NVIDIA RTL-Optimization Agent

**Team:** T16 Moonshot · Divya Kohli (UC Santa Cruz) · Cloud Track
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
# 1. official repo + our two Dockerfile fixes, then build
git clone --recurse-submodules <ICLAD26-NVIDIA-Problems>
cd ICLAD26-NVIDIA-Problems && git apply Dockerfile.patch
docker build -t iclad-dev:v1 .

# 2. ASAP7 techlib (ENV_PREPARATION.md)
mkdir -p techlib && git clone https://github.com/The-OpenROAD-Project/asap7sc7p5t_28.git techlib/asap7sc7p5t_28
( cd techlib/asap7sc7p5t_28/LIB/NLDM && for f in *.lib.7z; do 7z x -y "$f"; done )

# 3. place this agent dir at  async_fifo/verif_gate/  and install deps
pip install -r async_fifo/verif_gate/requirements.txt

# 4. run the gate on golden (sanity), any IP:
python3 async_fifo/verif_gate/run_gate.py \
    --config async_fifo/verif_gate/designs/prim_crc32.json \
    --candidate opentitan/hw/ip/prim/rtl --json verdict.json

# 5. run the autonomous optimizer (needs EXPRESS_MODE_KEY):
export EXPRESS_MODE_KEY=...          # Vertex AI Express key
python3 async_fifo/verif_gate/opt_agent.py \
    --config async_fifo/verif_gate/designs/prim_crc32.json \
    --backend vertex --model gemini-3-flash-preview \
    --objective area --iters 8
# -> writes best RTL + agent_summary.json (adopted changes, PPA, usage: calls/tokens)
```
Requires on PATH: `yosys`, `iverilog`/`vvp`, `sta`, `sv2v` (all in `iclad-dev:v1`);
`ASAP7_LIB_DIR` set to the techlib NLDM dir (defaults to the Docker `/workspace` path).

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
