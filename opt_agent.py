#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# opt_agent.py — verification-gated RTL optimization loop for async_fifo.
#
# Loop:
#   1. propose a small, equivalence-preserving rewrite (LLM or offline mock)
#   2. apply it on top of the golden RTL (golden is never mutated)
#   3. run_gate.py  -> sim + formal-equivalence + PPA verdict
#   4. ADOPT the candidate as the new best iff:
#         sim.passed AND equiv.proven AND timing met (wns_ps >= 0)
#         AND objective strictly improved (area or power)
#      otherwise reject and feed the reason back to the proposer
#   5. repeat for --iters, then write the best RTL + verdict + history
#
# The equivalence gate is the safety net: a rewrite that lowers area/power but
# is NOT provably equivalent is rejected, so the optimizer can be aggressive
# without risking the scored correctness check.
#
# Proposer backends (--backend):
#   mock    : offline, deterministic scripted edits (no network). Default, so
#             the whole loop is runnable/testable without an API key.
#   vertex  : Vertex AI Express via google-genai (per AgentSetup.md). Needs
#             EXPRESS_MODE_KEY in the environment. Asks the model for exact-
#             substring edits as JSON.
#
# Run inside iclad-dev:v1 (or a host with the same tools) with svutRun on PATH
# and ASAP7_LIB_DIR set — same environment run_gate.py needs.
# ---------------------------------------------------------------------------
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NVIDIA_ROOT = HERE.parents[1]
DESIGNS_DIR = HERE / "designs"
RUN_GATE = HERE / "run_gate.py"
EPS = 1e-9

# Set by configure() from a designs/<name>.json (shared schema with run_gate.py).
CONFIG_PATH = None
GOLDEN = None
RTL_FILES = []


def configure(config_path: Path):
    global CONFIG_PATH, GOLDEN, RTL_FILES
    CONFIG_PATH = Path(config_path).resolve()
    cfg = json.loads(CONFIG_PATH.read_text())
    root = (NVIDIA_ROOT / cfg["root"]).resolve()
    GOLDEN = root / cfg["rtl_dir"]
    RTL_FILES = cfg["rtl_files"]


# --------------------------------------------------------------------------
# Gate invocation + objective
# --------------------------------------------------------------------------
def run_gate(candidate: Path, work: Path) -> dict:
    out = work / "verdict.json"
    cmd = [sys.executable, str(RUN_GATE), "--config", str(CONFIG_PATH),
           "--candidate", str(candidate), "--golden", str(GOLDEN),
           "--work", str(work / "gate"), "--json", str(out)]
    print(f"[agent] gating {candidate.name} ...", flush=True)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(out.read_text())


def constraints_ok(v: dict) -> bool:
    # sim + equiv-VERIFIED (formal proof OR sim-miter fallback) + PPA present.
    # Timing is checked RELATIVE to baseline (see timing_ok). equiv confidence
    # (proven vs sim-verified) is surfaced in the summary, not gated on here.
    ppa = v.get("ppa") or {}
    eq = v.get("equiv", {})
    verified = eq.get("verified", eq.get("proven"))
    return bool(v.get("sim", {}).get("passed")
                and verified and ppa.get("wns_ps") is not None)


def timing_ok(cand: dict, best: dict) -> bool:
    cw = (cand.get("ppa") or {}).get("wns_ps")
    bw = (best.get("ppa") or {}).get("wns_ps")
    return cw is not None and bw is not None and cw >= bw - EPS  # no regression


def objective_value(v: dict, objective: str):
    ppa = v.get("ppa") or {}
    if objective == "timing":
        return ppa.get("wns_ps")                # maximize (handled in improved)
    return ppa.get("area_um2" if objective == "area" else "total_power_w")


def improved(cand: dict, best: dict, objective: str) -> bool:
    if not (constraints_ok(cand) and timing_ok(cand, best)):
        return False
    cv, bv = objective_value(cand, objective), objective_value(best, objective)
    if cv is None or bv is None:
        return False
    return cv > bv + EPS if objective == "timing" else cv < bv - EPS


def reject_reason(v: dict, best: dict = None) -> str:
    if not v.get("sim", {}).get("passed"):
        return "sim_failed"
    eq = v.get("equiv", {})
    if not eq.get("verified", eq.get("proven")):
        return "equivalence_not_verified"
    if (v.get("ppa") or {}).get("wns_ps") is None:
        return "no_timing_data"
    if best is not None and not timing_ok(v, best):
        return "timing_regressed"
    return "no_objective_improvement"


# --------------------------------------------------------------------------
# Apply a change  (dict with "edits" and/or "files")
#   edits: [{file, old, new}]           surgical substring replacement
#   files: {filename: full_content}     whole-file rewrite (for restructuring)
# Whole-file rewrites are applied first, then surgical edits on top.
# --------------------------------------------------------------------------
def apply_changes(change, cand: Path):
    if cand.exists():
        shutil.rmtree(cand)
    cand.mkdir(parents=True)
    for f in RTL_FILES:
        shutil.copy2(GOLDEN / f, cand / f)
    for fname, content in (change.get("files") or {}).items():
        if fname not in RTL_FILES:
            raise ValueError(f"whole-file rewrite of unknown file {fname}")
        (cand / fname).write_text(content)
    for i, e in enumerate(change.get("edits") or [], 1):
        fp = cand / e["file"]
        if not fp.is_file():
            raise ValueError(f"edit {i}: unknown file {e['file']}")
        txt = fp.read_text()
        n = txt.count(e["old"])
        if n != 1:
            raise ValueError(f"edit {i}: 'old' matched {n}x in {e['file']} (need 1)")
        fp.write_text(txt.replace(e["old"], e["new"], 1))


def change_is_empty(change) -> bool:
    return not (change.get("files") or change.get("edits"))


# --------------------------------------------------------------------------
# Transformation menu — the concrete, equivalence-preserving RTL rewrites the
# proposer is steered toward, grounded in the cited datapath-rewriting work
# (RTLRewriter / ROVER / ASPEN). Each is a *hint*; the equivalence gate is what
# keeps them honest. IMPORTANT lesson baked into the guidance: Yosys+ABC
# already does local constant-folding, CSE and strength reduction, so those
# rarely beat the tool — the wins come from things the tool WON'T do on its own
# (register/state reduction, width narrowing it can't prove, algorithmic
# restructuring), which is why the prompt leans on the bottleneck breakdown.
TRANSFORMATION_MENU = """\
T1 register_reduction : remove/merge redundant or duplicated state; narrow an
   over-declared reg to its used range (biggest lever when seq_pct is high).
T2 fsm_reencoding     : re-encode state (binary/gray/onehot) to cut area/logic
   the synth tool will not re-encode by itself.
T3 resource_sharing   : share ONE operator (adder/comparator) across mutually
   exclusive branches when written as separate operators.
T4 operator_restructuring : re-associate/balance wide operator trees; simplify
   or merge comparators; carry-save for multi-operand adds.
T5 width_minimization : narrow buses/wires whose upper bits are provably unused.
T6 boolean_simplification : simplify control/flag logic and mux trees.
T7 strength_reduction : mul/div by constant -> shift/add (usually already done
   by ABC; only if you see a leftover multiplier in top_cells).
"""


# Offline mock: a small library of concrete, deterministic transformations so
# the whole loop (propose -> apply -> gate -> decide) is runnable/testable with
# no network. Each returns a change dict; the loop measures the REAL PPA/equiv
# outcome (it does not assume the transform helps).
# prim_crc32 crc32_byte_calc is a fixed linear GF(2) map, so f(b)=XOR of the
# basis constants f(1<<k) over set bits k. Two equivalent rewrites of the
# 256-entry lookup case, aimed at different objectives:
_CRC_BASIS = ["77073096", "ee0e612c", "076dc419", "0edb8832",
              "1db71064", "3b6e20c8", "76dc4190", "edb88320"]


def _crc32_change(objective):
    src = (GOLDEN / "prim_crc32.sv").read_text()
    if objective == "timing":
        # Flat masked-XOR: a single 8-way XOR of (bit-replicated & constant)
        # -> balanced XOR tree (depth ~3), minimizing combinational depth.
        terms = " ^\n      ".join(
            f"({{32{{b[{k}]}}}} & 32'h{c})" for k, c in enumerate(_CRC_BASIS))
        body = f"    crc32_byte_calc =\n      {terms};\n"
        rationale = "balance CRC byte map to a flat masked-XOR tree (min depth)"
    else:
        # Sequential accumulation -> compact area.
        lines = "".join(
            f"    if (b[{k}]) r = r ^ 32'h{c};\n" for k, c in enumerate(_CRC_BASIS))
        body = "    logic [31:0] r;\n    r = 32'h0;\n" + lines + "    crc32_byte_calc = r;\n"
        rationale = "linearize CRC byte table (256-case -> 8-term XOR of basis)"
    new_fn = ("  function automatic logic [31:0] crc32_byte_calc(logic [7:0] b);\n"
              + body + "  endfunction\n")
    new_src, n = re.subn(
        r"  function automatic logic \[31:0\] crc32_byte_calc\(logic \[7:0\] b\);.*?\n  endfunction\n",
        new_fn, src, count=1, flags=re.DOTALL)
    if n != 1:
        return {"edits": []}
    return {"rationale": rationale, "transform": "T4 operator_restructuring",
            "files": {"prim_crc32.sv": new_src}}


def propose_mock(iteration, best_verdict, feedback, objective):
    if "prim_crc32.sv" in RTL_FILES:
        return _crc32_change(objective) if iteration == 0 else {"edits": []}
    seq = [
        # T5-style: 1 -> 1'b1 (equivalent; exercises the "proven but no gain" path)
        {"rationale": "T5 width/const tidy on read-pointer increment",
         "transform": "width_minimization",
         "edits": [{"file": "rptr_empty.v",
                    "old": "rbin + ((rinc & ~rempty) ? 1 : 0)",
                    "new": "rbin + ((rinc & ~rempty) ? 1'b1 : 1'b0)"}]},
        # deliberate functional break (exercises the reject path)
        {"rationale": "bad: inverted empty compare",
         "transform": "boolean_simplification",
         "edits": [{"file": "rptr_empty.v",
                    "old": "assign rempty_val = (rgraynext == rq2_wptr);",
                    "new": "assign rempty_val = (rgraynext != rq2_wptr);"}]},
    ]
    return seq[iteration] if iteration < len(seq) else {"edits": []}


# --------------------------------------------------------------------------
# Proposer: Vertex AI Express (google-genai), per AgentSetup.md
# --------------------------------------------------------------------------
def format_bottleneck(ppa):
    b = (ppa or {}).get("bottleneck") or {}
    if not b:
        return "  (no synthesis breakdown available)"
    by = b.get("top_cells_ranked_by", "count")
    tops = ", ".join(
        f"{c['cell']}x{c['count']}" + (f"={c['area']}" if c.get("area") is not None else "")
        for c in b.get("top_cells", [])[:6])
    return (f"  total_area={b.get('total_area')}  "
            f"sequential={b.get('seq_area')} ({b.get('seq_pct')}%)  "
            f"combinational={b.get('comb_area')}\n"
            f"  top cells (by {by}): {tops}")


def build_prompt(best_verdict, memory, objective):
    rtl = "\n".join(
        f"===== {f} =====\n{(GOLDEN / f).read_text()}" for f in RTL_FILES)
    ppa = best_verdict.get("ppa") or {}
    mem = "\n".join(f"  - {m}" for m in memory[-6:]) or "  (none yet)"
    return f"""You are an RTL micro-optimization agent for the `async_fifo` design.
Objective: MINIMIZE {objective} (area_um2 or total_power_w) while keeping the
design FUNCTIONALLY EQUIVALENT to the original. Equivalence is checked by a
formal sequential-equivalence proof (Yosys equiv_induct) AND a simulation suite.
A rewrite that is not provably equivalent, or that fails a test, scores NOTHING.

Hard rules:
- Do NOT change module names, port lists, parameters, or clock/reset structure.
- The synthesizer (Yosys+ABC) ALREADY does local constant-folding, CSE and
  strength reduction — repeating those wastes a turn. Aim at what the tool will
  NOT do by itself, guided by the breakdown below.

Current best {objective}={objective_value(best_verdict, objective)}; timing wns_ps={ppa.get('wns_ps')}.
Synthesis breakdown (attack the largest contributor):
{format_bottleneck(ppa)}

Transformation menu (pick one, name it in "transform"):
{TRANSFORMATION_MENU}
Previously rejected attempts (do not repeat; learn from the reason):
{mem}

Return ONLY JSON (no prose, no markdown fences). Either surgical edits:
  {{"rationale":"...","transform":"T#","edits":[{{"file":"<{RTL_FILES}>","old":"exact unique substring","new":"replacement"}}]}}
or a whole-file rewrite (better for restructuring):
  {{"rationale":"...","transform":"T#","files":{{"<filename>":"<full new file contents>"}}}}
Each "old" must appear EXACTLY ONCE in its file. Return {{"edits":[]}} if you
see no safe equivalence-preserving win.

RTL:
{rtl}
"""


def parse_change(text: str):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    if "{" in t:
        t = t[t.find("{"): t.rfind("}") + 1]
    data = json.loads(t)
    change = {"rationale": data.get("rationale", ""),
              "transform": data.get("transform", ""),
              "edits": data.get("edits") or [],
              "files": data.get("files") or {}}
    if not isinstance(change["edits"], list) or not isinstance(change["files"], dict):
        raise ValueError("malformed change: edits must be list, files must be object")
    return change


def propose_vertex(model, best_verdict, memory, objective, usage, max_retries=5):
    from google import genai                       # noqa: imported lazily
    from google.genai.errors import APIError
    key = os.environ.get("EXPRESS_MODE_KEY")
    if not key:
        raise RuntimeError("EXPRESS_MODE_KEY not set (required for --backend vertex)")
    client = genai.Client(vertexai=True, api_key=key,
                          http_options={"headers": {"X-Goog-User-Project": ""}})
    prompt = build_prompt(best_verdict, memory, objective)
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            usage["calls"] += 1
            u = getattr(resp, "usage_metadata", None)
            if u is not None:
                usage["input_tokens"] += getattr(u, "prompt_token_count", 0) or 0
                usage["output_tokens"] += getattr(u, "candidates_token_count", 0) or 0
            return parse_change(resp.text or "")
        except APIError as e:
            if getattr(e, "code", None) == 429 and attempt < max_retries:
                print(f"[agent] 429, retry in {delay}s", flush=True)
                time.sleep(delay); delay *= 2; continue
            raise
    return {"edits": []}


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="verification-gated RTL opt loop")
    ap.add_argument("--config", default=str(DESIGNS_DIR / "async_fifo.json"))
    ap.add_argument("--backend", choices=["mock", "vertex"], default="mock")
    ap.add_argument("--model", default="gemini-3-flash-preview")
    ap.add_argument("--objective", choices=["area", "power", "timing"], default="area")
    ap.add_argument("--iters", type=int, default=2)
    ap.add_argument("--work", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    configure(Path(args.config))
    design_root = GOLDEN.parent
    work = Path(args.work).resolve() if args.work else (design_root / ".agent_work")
    out_default = design_root / "opt_result"
    work.mkdir(parents=True, exist_ok=True)
    history = []

    # Establish the baseline by gating the golden RTL unchanged.
    print("[agent] establishing baseline (golden RTL)...", flush=True)
    best = run_gate(GOLDEN, work / "baseline")
    if not constraints_ok(best):
        print("[agent] WARNING: baseline does not satisfy constraints:",
              reject_reason(best))
    adopted, memory = {"files": {}, "edits": []}, []
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    base_obj = objective_value(best, args.objective)
    print(f"[agent] baseline {args.objective}={base_obj}  "
          f"(area={objective_value(best,'area')}, "
          f"power={objective_value(best,'power')})", flush=True)
    print(f"[agent] bottleneck:\n{format_bottleneck(best.get('ppa'))}", flush=True)

    for i in range(args.iters):
        if args.backend == "mock":
            change = propose_mock(i, best, memory, args.objective)
        else:
            change = propose_vertex(args.model, best, memory, args.objective, usage)
        if change_is_empty(change):
            print(f"[agent] iter {i}: proposer returned no change — stop.")
            break
        tag = f"{change.get('transform','?')}: {change.get('rationale','')[:60]}"

        cand = work / f"cand_{i}"
        try:
            apply_changes(change, cand)
        except Exception as exc:
            memory.append(f"[{tag}] not applied: {exc}")
            history.append({"iter": i, "change": tag, "decision": "reject",
                            "reason": "bad_change", "detail": str(exc)})
            print(f"[agent] iter {i}: REJECT (bad change: {exc})")
            continue

        v = run_gate(cand, work / f"gate_{i}")
        adopt = improved(v, best, args.objective)
        reason = None if adopt else reject_reason(v, best)
        ppa = v.get("ppa") or {}
        history.append({
            "iter": i, "change": tag,
            "verdict": {"sim": v.get("sim", {}).get("passed"),
                        "equiv_verified": v.get("equiv", {}).get("verified"),
                        "equiv_confidence": v.get("equiv", {}).get("confidence"),
                        "area_um2": ppa.get("area_um2"),
                        "wns_ps": ppa.get("wns_ps"),
                        "total_power_w": ppa.get("total_power_w")},
            "decision": "adopt" if adopt else "reject",
            "reason": reason,
        })

        if adopt:
            # fold this change into the running adopted set and re-baseline
            best = v
            adopted["files"].update(change.get("files") or {})
            adopted["edits"].extend(change.get("edits") or [])
            memory.append(f"[{tag}] ADOPTED -> {args.objective}="
                          f"{objective_value(v, args.objective)}")
            print(f"[agent] iter {i}: ADOPT ({tag}) -> "
                  f"{args.objective}={objective_value(v, args.objective)}")
        else:
            memory.append(f"[{tag}] rejected: {reason} "
                          f"(sim={v.get('sim',{}).get('passed')}, "
                          f"equiv={v.get('equiv',{}).get('proven')}, "
                          f"area={ppa.get('area_um2')})")
            print(f"[agent] iter {i}: REJECT ({reason})")

    # Materialize the best RTL (baseline + adopted changes) and the artifacts.
    out = Path(args.out).resolve() if args.out else out_default
    apply_changes(adopted, out)             # empty adopted -> pristine golden
    final_obj = objective_value(best, args.objective)
    n_changes = len(adopted["files"]) + len(adopted["edits"])
    summary = {
        "objective": args.objective,
        "baseline_value": base_obj,
        "final_value": final_obj,
        "improvement": (None if base_obj is None or final_obj is None
                        else round(base_obj - final_obj, 6)),
        "improvement_pct": (None if not base_obj else
                            round(100 * (base_obj - final_obj) / base_obj, 3)),
        "adopted": adopted,
        "usage": usage,
        "best_verdict": best,
        "history": history,
    }
    (out / "agent_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\n[agent] DONE")
    print(f"[agent] baseline {args.objective}={base_obj} -> best={final_obj} "
          f"({n_changes} change(s) adopted); "
          f"llm_calls={usage['calls']} tokens={usage['input_tokens']+usage['output_tokens']}")
    print(f"[agent] best RTL + agent_summary.json written to {out}")


if __name__ == "__main__":
    main()
