#!/usr/bin/env python3
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


def run_gate(candidate: Path, work: Path) -> dict:
    out = work / "verdict.json"
    cmd = [sys.executable, str(RUN_GATE), "--config", str(CONFIG_PATH),
           "--candidate", str(candidate), "--golden", str(GOLDEN),
           "--work", str(work / "gate"), "--json", str(out)]
    print(f"[agent] gating {candidate.name} ...", flush=True)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not out.exists():
        return {"sim": {"passed": False}, "equiv": {"proven": False, "verified": False},
                "ppa": None, "accept": False, "reason": "gate_failed_no_verdict"}
    try:
        return json.loads(out.read_text())
    except Exception:
        return {"sim": {"passed": False}, "equiv": {"proven": False, "verified": False},
                "ppa": None, "accept": False, "reason": "invalid_verdict_json"}


def constraints_ok(v: dict) -> bool:
    ppa = v.get("ppa") or {}
    eq = v.get("equiv", {})
    verified = eq.get("verified", eq.get("proven"))
    return bool(v.get("sim", {}).get("passed")
                and verified and ppa.get("wns_ps") is not None)


def timing_ok(cand: dict, best: dict) -> bool:
    cw = (cand.get("ppa") or {}).get("wns_ps")
    bw = (best.get("ppa") or {}).get("wns_ps")
    return cw is not None and bw is not None and cw >= bw - EPS


def objective_value(v: dict, objective: str):
    ppa = v.get("ppa") or {}
    if objective == "timing":
        return ppa.get("wns_ps")
    return ppa.get("area_um2" if objective == "area" else "total_power_w")


def improved(cand: dict, best: dict, objective: str) -> bool:
    if not (constraints_ok(cand) and timing_ok(cand, best)):
        return False
    cv, bv = objective_value(cand, objective), objective_value(best, objective)
    if cv is None or bv is None:
        return False
    return cv > bv + EPS if objective == "timing" else cv < bv - EPS


def reject_reason(v: dict, best: dict = None) -> str:
    if v.get("reason") in ("gate_failed_no_verdict", "invalid_verdict_json"):
        return v["reason"]
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


def apply_changes(change, cand: Path, base_files: dict = None):
    if cand.exists():
        shutil.rmtree(cand)
    cand.mkdir(parents=True)
    for f in RTL_FILES:
        if base_files and f in base_files:
            (cand / f).write_text(base_files[f])
        else:
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


def snapshot_files(cand: Path) -> dict:
    return {f: (cand / f).read_text() for f in RTL_FILES if (cand / f).is_file()}


def change_is_empty(change) -> bool:
    return not (change.get("files") or change.get("edits"))


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

_CRC_BASIS = ["77073096", "ee0e612c", "076dc419", "0edb8832",
              "1db71064", "3b6e20c8", "76dc4190", "edb88320"]


def _crc32_change(objective):
    src = (GOLDEN / "prim_crc32.sv").read_text()
    if objective == "timing":
        terms = " ^\n      ".join(
            f"({{32{{b[{k}]}}}} & 32'h{c})" for k, c in enumerate(_CRC_BASIS))
        body = f"    crc32_byte_calc =\n      {terms};\n"
        rationale = "balance CRC byte map to a flat masked-XOR tree (min depth)"
    else:
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
        {"rationale": "T5 width/const tidy on read-pointer increment",
         "transform": "width_minimization",
         "edits": [{"file": "rptr_empty.v",
                    "old": "rbin + ((rinc & ~rempty) ? 1 : 0)",
                    "new": "rbin + ((rinc & ~rempty) ? 1'b1 : 1'b0)"}]},
        {"rationale": "bad: inverted empty compare",
         "transform": "boolean_simplification",
         "edits": [{"file": "rptr_empty.v",
                    "old": "assign rempty_val = (rgraynext == rq2_wptr);",
                    "new": "assign rempty_val = (rgraynext != rq2_wptr);"}]},
    ]
    return seq[iteration] if iteration < len(seq) else {"edits": []}


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


def build_prompt(best_verdict, memory, objective, current_files=None):
    files_map = current_files or {}
    rtl = "\n".join(
        f"===== {f} =====\n{files_map.get(f) or (GOLDEN / f).read_text()}"
        for f in RTL_FILES)
    ppa = best_verdict.get("ppa") or {}
    mem = "\n".join(f"  - {m}" for m in memory[-6:]) or "  (none yet)"
    return f"""You are an RTL micro-optimization agent.
Objective: MINIMIZE {objective} (area_um2 or total_power_w) while keeping the
design FUNCTIONALLY EQUIVALENT to the original. Equivalence is checked by a
formal sequential-equivalence proof (Yosys) AND a simulation suite.
A rewrite that is not provably equivalent, or that fails a test, scores NOTHING.

Hard rules:
- Do NOT change module names, port lists, parameters, or clock/reset structure.
- The synthesizer (Yosys+ABC) ALREADY does local constant-folding, CSE and
  strength reduction — repeating those wastes a turn. Aim at what the tool will
  NOT do by itself, guided by the breakdown below.
- The RTL shown below is the CURRENT BEST (includes any previously adopted
  changes). Propose your edit against THIS version, not the original design.
- IMPORTANT OUTPUT SAFETY: your response must be valid JSON. Any RTL code you
  place inside a JSON string value MUST have literal newlines escaped as \\n
  and any double-quote characters escaped as \\". Do not include raw
  unescaped newlines inside a JSON string.

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
    try:
        data = json.loads(t, strict=False)
    except json.JSONDecodeError:
        def _escape_ctrl(match):
            s = match.group(0)
            return s.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
        t2 = re.sub(r'"(?:[^"\\]|\\.)*"', _escape_ctrl, t, flags=re.DOTALL)
        data = json.loads(t2, strict=False)
    change = {"rationale": data.get("rationale", ""),
              "transform": data.get("transform", ""),
              "edits": data.get("edits") or [],
              "files": data.get("files") or {}}
    if not isinstance(change["edits"], list) or not isinstance(change["files"], dict):
        raise ValueError("malformed change: edits must be list, files must be object")
    return change


def propose_vertex(model, best_verdict, memory, objective, usage,
                    max_retries=5, current_files=None):
    from google import genai
    from google.genai.errors import APIError
    express = os.environ.get("EXPRESS_MODE_KEY")
    devkey = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if express:
        client = genai.Client(vertexai=True, api_key=express,
                              http_options={"headers": {"X-Goog-User-Project": ""}})
    elif devkey:
        client = genai.Client(api_key=devkey)
    else:
        raise RuntimeError("Set EXPRESS_MODE_KEY or GEMINI_API_KEY (required for --backend vertex)")
    prompt = build_prompt(best_verdict, memory, objective, current_files=current_files)
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
        except json.JSONDecodeError as e:
            print(f"[agent] JSON parse failed on model response: {e}", flush=True)
            return {"edits": []}
    return {"edits": []}


def main():
    ap = argparse.ArgumentParser(description="verification-gated RTL opt loop")
    ap.add_argument("--config", default=str(DESIGNS_DIR / "async_fifo.json"))
    ap.add_argument("--backend", choices=["mock", "vertex"], default="mock")
    ap.add_argument("--model", default="gemini-3.6-flash")
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

    print("[agent] establishing baseline (golden RTL)...", flush=True)
    best = run_gate(GOLDEN, work / "baseline")
    if not constraints_ok(best):
        print("[agent] WARNING: baseline does not satisfy constraints:",
              reject_reason(best))

    adopted = {"files": {}, "edits": []}
    current_state = {}
    memory = []
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    base_obj = objective_value(best, args.objective)

    out = Path(args.out).resolve() if args.out else out_default
    out.mkdir(parents=True, exist_ok=True)
    best_snapshots_dir = out / "best_snapshots"
    best_snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Tracks WHICH iteration produced the current `best` verdict, so the final
    # summary and a dedicated snapshot dir make it unambiguous which numbers
    # are authoritative -- avoids ever having to re-read a shared scratch
    # synth report mid-run and guess which run it belongs to.
    best_iteration = -1  # -1 == golden baseline, never overwritten by an adopt

    print(f"[agent] baseline {args.objective}={base_obj}  "
          f"(area={objective_value(best,'area')}, "
          f"power={objective_value(best,'power')})", flush=True)
    print(f"[agent] bottleneck:\n{format_bottleneck(best.get('ppa'))}", flush=True)

    for i in range(args.iters):
        if args.backend == "mock":
            change = propose_mock(i, best, memory, args.objective)
        else:
            change = propose_vertex(args.model, best, memory, args.objective,
                                     usage, current_files=current_state)
        if change_is_empty(change):
            print(f"[agent] iter {i}: proposer returned no change — stop.")
            break
        tag = f"{change.get('transform','?')}: {change.get('rationale','')[:60]}"

        cand = work / f"cand_{i}"
        try:
            apply_changes(change, cand, base_files=current_state)
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
            best = v
            best_iteration = i
            adopted["files"].update(change.get("files") or {})
            adopted["edits"].extend(change.get("edits") or [])
            current_state = snapshot_files(cand)

            # Save an immutable, iteration-tagged snapshot: RTL + verdict,
            # never overwritten by later iterations or other design runs.
            snap_dir = best_snapshots_dir / f"iter_{i:02d}"
            if snap_dir.exists():
                shutil.rmtree(snap_dir)
            snap_dir.mkdir(parents=True)
            for f, content in current_state.items():
                (snap_dir / f).write_text(content)
            (snap_dir / "verdict.json").write_text(json.dumps(v, indent=2) + "\n")
            (snap_dir / "change.json").write_text(json.dumps(change, indent=2) + "\n")

            memory.append(f"[{tag}] ADOPTED -> {args.objective}="
                          f"{objective_value(v, args.objective)}")
            print(f"[agent] iter {i}: ADOPT ({tag}) -> "
                  f"{args.objective}={objective_value(v, args.objective)}  "
                  f"[snapshot: best_snapshots/iter_{i:02d}]")
        else:
            memory.append(f"[{tag}] rejected: {reason} "
                          f"(sim={v.get('sim',{}).get('passed')}, "
                          f"equiv={v.get('equiv',{}).get('proven')}, "
                          f"area={ppa.get('area_um2')})")
            print(f"[agent] iter {i}: REJECT ({reason})")

    apply_changes(adopted, out, base_files=current_state if current_state else None)
    final_obj = objective_value(best, args.objective)
    n_changes = len(adopted["files"]) + len(adopted["edits"])
    summary = {
        "objective": args.objective,
        "baseline_value": base_obj,
        "final_value": final_obj,
        "best_iteration": best_iteration,
        "best_snapshot_dir": (str(best_snapshots_dir / f"iter_{best_iteration:02d}")
                               if best_iteration >= 0 else None),
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

    # Definitive pointer file -- always trust THIS over any shared scratch
    # synth report, which gets overwritten by every subsequent gate call.
    (out / "BEST_ITERATION.json").write_text(json.dumps({
        "best_iteration": best_iteration,
        "objective": args.objective,
        "baseline_value": base_obj,
        "final_value": final_obj,
        "snapshot_dir": summary["best_snapshot_dir"],
        "note": ("best_iteration == -1 means no candidate was ever adopted; "
                 "final RTL equals golden baseline unchanged.")
    }, indent=2) + "\n")

    print("\n[agent] DONE")
    print(f"[agent] baseline {args.objective}={base_obj} -> best={final_obj} "
          f"(iteration {best_iteration}; {n_changes} change(s) adopted); "
          f"llm_calls={usage['calls']} tokens={usage['input_tokens']+usage['output_tokens']}")
    print(f"[agent] best RTL + agent_summary.json + BEST_ITERATION.json written to {out}")


if __name__ == "__main__":
    main()
