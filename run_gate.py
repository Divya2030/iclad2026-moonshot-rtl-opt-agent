#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# run_gate.py — Verification gate loop for RTL-optimization tasks.
#
#   1. SIM   (mandatory, == the SCORED correctness check)
#   2. EQUIV (strong guardrail): Yosys equiv
#   3. PPA   (only measured if SIM passes): Yosys synth + OpenSTA
# ---------------------------------------------------------------------------
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NVIDIA_ROOT = HERE.parents[1]
DESIGNS_DIR = HERE / "designs"

CFG = {}
DESIGN_ROOT = GOLDEN = SIM_DIR = SYN_DIR = None
RTL_FILES = []
TOP = None
EQUIV_DEFINES = []
SIM_SPEC = {}


def configure(config_path: Path):
    global CFG, DESIGN_ROOT, GOLDEN, SIM_DIR, SYN_DIR, RTL_FILES, TOP
    global EQUIV_DEFINES, SIM_SPEC
    CFG = json.loads(Path(config_path).read_text())
    DESIGN_ROOT = (NVIDIA_ROOT / CFG["root"]).resolve()
    GOLDEN = DESIGN_ROOT / CFG["rtl_dir"]
    SYN_DIR = DESIGN_ROOT / CFG["syn_dir"]
    RTL_FILES = CFG["rtl_files"]
    TOP = CFG["top"]
    EQUIV_DEFINES = CFG.get("equiv", {}).get("defines", [])
    SIM_SPEC = CFG["sim"]
    SIM_DIR = DESIGN_ROOT / SIM_SPEC.get("cwd", ".")


def run(cmd, cwd=None, timeout=900):
    print(f"[gate] $ {' '.join(str(c) for c in cmd)}  (cwd={cwd})", flush=True)
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.returncode, p.stdout


def build_work_rtl(candidate: Path, golden: Path, work: Path) -> Path:
    rtl = work / "rtl"
    if rtl.exists():
        shutil.rmtree(rtl)
    rtl.mkdir(parents=True)
    for f in RTL_FILES:
        src = candidate / f if (candidate / f).is_file() else golden / f
        if not src.is_file():
            raise FileNotFoundError(f"{f} not found in candidate or golden")
        shutil.copy2(src, rtl / f)
    return rtl


def gate_sim(work_rtl: Path, work: Path):
    mode = SIM_SPEC.get("mode", "svut")
    if mode == "svut":
        return gate_sim_svut(work_rtl, work)
    if mode == "iverilog_tb":
        return gate_sim_iverilog_tb(work_rtl, work)
    if mode == "iverilog_diff":
        return gate_sim_iverilog_diff(work_rtl, work)
    return {"passed": False, "error": f"unknown sim mode {mode}"}


def gate_sim_iverilog_diff(work_rtl: Path, work: Path):
    rtl = [str(work_rtl / f) for f in RTL_FILES]
    tbs = [str(DESIGN_ROOT / t) for t in SIM_SPEC.get("tb_files", [])]
    tbs += [str(HERE / h) for h in SIM_SPEC.get("harness_files", [])]
    out_vvp = work / "sim_diff.vvp"
    rc_c, out_c = run(["iverilog", *SIM_SPEC.get("iverilog_args", ["-g2012"]),
                       "-o", str(out_vvp), *rtl, *tbs], cwd=DESIGN_ROOT, timeout=600)
    if rc_c != 0:
        (work / "sim.log").write_text(out_c)
        return {"passed": False, "stage": "compile", "returncode": rc_c}
    rc_r, out_r = run(["vvp", str(out_vvp)], cwd=DESIGN_ROOT, timeout=600)
    (work / "sim.log").write_text(out_c + out_r)
    expected = (DESIGN_ROOT / SIM_SPEC["expected"]).read_text().splitlines()
    anchor, n = SIM_SPEC.get("extract_anchor"), SIM_SPEC.get("extract_lines")
    lines = out_r.splitlines()
    got = []
    for i, ln in enumerate(lines):
        if ln.strip() == anchor:
            got = [x.strip() for x in lines[i:i + n]]
            break
    exp = [x.strip() for x in expected[:n]]
    passed = got == exp and len(got) == n
    return {"passed": passed, "returncode": rc_r,
            "matched_lines": sum(1 for a, b in zip(got, exp) if a == b),
            "expected_lines": len(exp), "got_lines": len(got)}


def gate_sim_svut(work_rtl: Path, work: Path):
    files_f = work / "files.f"
    files_f.write_text("".join(f"{work_rtl / f}\n" for f in RTL_FILES))
    results = []
    for defs in SIM_SPEC.get("defines", [""]):
        rc, out = run(["svutRun", "-f", str(files_f)]
                      + (["-define", defs] if defs else []),
                      cwd=SIM_DIR, timeout=600)
        tag = defs.replace(';', '_').replace('=', '') or "default"
        (work / f"sim_{tag}.log").write_text(out)
        results.append(parse_svut(out, rc))
    return {"passed": all(r["passed"] for r in results), "runs": results}


def gate_sim_iverilog_tb(work_rtl: Path, work: Path):
    rtl = [str(work_rtl / f) for f in RTL_FILES]
    iargs = SIM_SPEC.get("iverilog_args", ["-g2012"])
    results = []
    for tb_rel in SIM_SPEC.get("tb_files", []):
        tb = DESIGN_ROOT / tb_rel
        name = Path(tb_rel).stem
        out_vvp = work / f"{name}.vvp"
        rc_c, out_c = run(["iverilog", *iargs, "-o", str(out_vvp), *rtl, str(tb)],
                          cwd=DESIGN_ROOT, timeout=600)
        if rc_c != 0:
            (work / f"sim_{name}.log").write_text(out_c)
            results.append({"tb": name, "passed": False, "stage": "compile",
                            "returncode": rc_c})
            continue
        rc_r, out_r = run(["vvp", str(out_vvp)], cwd=DESIGN_ROOT, timeout=600)
        (work / f"sim_{name}.log").write_text(out_c + out_r)
        results.append(parse_iverilog_tb(name, out_r, rc_r))
    return {"passed": bool(results) and all(r["passed"] for r in results),
            "runs": results}


def parse_svut(out: str, rc: int):
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    m = re.search(r"STATUS:\s*(\d+)\s*/\s*(\d+)\s+test", clean)
    if m:
        passed_n, ran = int(m.group(1)), int(m.group(2))
        return {"passed": passed_n == ran and ran > 0,
                "tests_ran": ran, "tests_passed": passed_n,
                "fail_markers": ran - passed_n, "returncode": rc}
    fails = len(re.findall(r"FAILURE:|ERROR:", clean))
    return {"passed": fails == 0, "tests_ran": None,
            "fail_markers": fails, "returncode": rc}


def parse_iverilog_tb(name: str, out: str, rc: int):
    top_ok = re.search(r"All\s+\d+\s+test cases completed successfully", out)
    em = re.search(r"(\d+)\s+errors? detected", out)
    errs = int(em.group(1)) if em else 0
    tc_fail = len(re.findall(r"TC\s+\d+[^\n]*FAILED", out))
    tc_ok = len(re.findall(r"TC\s+\d+[^\n]*successful", out))
    passed = (bool(top_ok) or (tc_ok > 0 and tc_fail == 0)) and errs == 0
    return {"tb": name, "passed": passed, "returncode": rc,
            "errors_detected": errs, "tc_failed": tc_fail, "tc_ok": tc_ok}


def gate_equiv(golden: Path, work_rtl: Path, work: Path):
    eq = CFG.get("equiv", {})
    if not eq.get("enabled", True):
        return {"proven": None, "verified": None, "skipped": True}
    rc, out = run(["bash", str(HERE / "equiv_check.sh"),
                   str(golden), str(work_rtl), TOP,
                   ",".join(RTL_FILES), " ".join(EQUIV_DEFINES)],
                  cwd=HERE, timeout=1800)
    (work / "equiv.log").write_text(out)
    if rc == 0:
        return {"proven": True, "verified": True, "method": "formal",
                "confidence": "proven", "returncode": rc}
    miter = eq.get("sim_miter")
    if not miter:
        return {"proven": False, "verified": False, "method": "formal",
                "confidence": "unverified", "returncode": rc}
    m = gate_equiv_miter(golden, work_rtl, work, miter)
    return {"proven": False, "verified": bool(m["passed"]),
            "method": "sim-miter", "returncode": rc,
            "confidence": "sim-verified" if m["passed"] else "refuted", **m}


def gate_equiv_miter(golden: Path, work_rtl: Path, work: Path, spec: dict):
    if len(RTL_FILES) != 1:
        return {"passed": False, "miter_note": "sim-miter only supports single-file designs"}
    gold_src = (golden / RTL_FILES[0]).read_text()
    cand_src = (work_rtl / RTL_FILES[0]).read_text()
    gold_f = work / "miter_gold.sv"
    cand_f = work / "miter_cand.sv"
    gold_f.write_text(re.sub(rf"\bmodule\s+{TOP}\b", f"module {spec['golden_module']}", gold_src, count=1))
    cand_f.write_text(re.sub(rf"\bmodule\s+{TOP}\b", f"module {spec['cand_module']}", cand_src, count=1))
    fixture = HERE / spec["fixture"]
    out_vvp = work / "miter.vvp"
    rc_c, out_c = run(["iverilog", "-g2012", "-o", str(out_vvp),
                       str(gold_f), str(cand_f), str(fixture)], cwd=work, timeout=600)
    if rc_c != 0:
        (work / "miter.log").write_text(out_c)
        return {"passed": False, "miter_stage": "compile"}
    rc_r, out_r = run(["vvp", str(out_vvp)], cwd=work, timeout=600)
    (work / "miter.log").write_text(out_c + out_r)
    passed = bool(re.search(spec.get("pass_regex", "MITER PASS"), out_r))
    mm = re.search(r"MITER PASS \((\d+) vectors\)", out_r)
    return {"passed": passed, "miter_vectors": int(mm.group(1)) if mm else None}


def gate_ppa(work_rtl: Path, work: Path):
    import os
    syn = CFG.get("syn", {})
    mode = syn.get("mode", "run_syn_sh")
    env = os.environ.copy()
    rc = 0
    log = []
    if mode == "run_syn_sh":
        env[syn.get("verilog_files_env", "VERILOG_FILES")] = \
            " ".join(str(work_rtl / f) for f in RTL_FILES)
        cmd = syn.get("cmd", ["./run_syn.sh", "all"])
        print(f"[gate] $ {' '.join(cmd)}  (candidate -> {work_rtl})", flush=True)
        p = subprocess.run(cmd, cwd=SYN_DIR, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=1800)
        rc, log = p.returncode, [p.stdout]
    elif mode == "tcl":
        fl = work / "candidate.f"
        fl.write_text("".join(str((work_rtl / f).resolve()) + "\n" for f in RTL_FILES))
        env[syn.get("filelist_env", "VERILOG_FILELIST")] = str(fl)
        env["DESIGN_NAME"] = TOP
        for step in syn.get("steps", []):
            sh = f"source env.sh >/dev/null 2>&1; exec {' '.join(step)}"
            print(f"[gate] $ {' '.join(step)}  (DESIGN_NAME={TOP})", flush=True)
            p = subprocess.run(["bash", "-c", sh], cwd=SYN_DIR, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=1800)
            log.append(p.stdout)
            if p.returncode != 0:
                rc = p.returncode
                break
    (work / "synth.log").write_text("\n".join(log))
    stat = SYN_DIR / syn.get("stat", "syn_results/synth_stat.txt")
    return {
        "synth_returncode": rc,
        "area_um2":      parse_area(stat),
        "wns_ps":        parse_wns(SYN_DIR / syn.get("timing", "reports/sta_timing_report.txt")),
        "worst_slack_ps": parse_worst_slack(SYN_DIR / syn.get("paths", "reports/sta_timing_paths.txt")),
        "total_power_w": parse_power(SYN_DIR / syn.get("power", "reports/sta_power_report.txt")),
        "bottleneck":    parse_bottleneck(stat),
    }


def _read(p: Path):
    return p.read_text(errors="replace") if p.is_file() else ""


def parse_area(p: Path):
    m = re.search(r"[Cc]hip area.*?:\s*([\d.]+)", _read(p))
    return float(m.group(1)) if m else None


def parse_wns(p: Path):
    m = re.search(r"\|\s*WNS\s*\|\s*(-?[\d.]+)\s*ps", _read(p))
    return float(m.group(1)) if m else None


def parse_worst_slack(p: Path):
    vals = [float(v) for v in
            re.findall(r"(-?[\d.]+)\s+slack\s*\((?:MET|VIOLATED)\)", _read(p))]
    return min(vals) if vals else None


def parse_bottleneck(p: Path):
    txt = _read(p)
    total = parse_area(p)
    ms = re.search(r"sequential elements:\s*([\d.]+)\s*\(([\d.]+)%\)", txt)
    seq = float(ms.group(1)) if ms else None
    seq_pct = float(ms.group(2)) if ms else None
    cells, ranked_by = [], None
    for cnt, area, name in re.findall(r"^\s*(\d+)\s+([\d.]+)\s+(\w*ASAP7\w*)\s*$", txt, re.M):
        cells.append({"cell": name, "count": int(cnt), "area": float(area)})
    if cells:
        cells.sort(key=lambda c: -c["area"]); ranked_by = "area"
    else:
        for name, cnt in re.findall(r"^\s*(\w*ASAP7\w*)\s+(\d+)\s*$", txt, re.M):
            cells.append({"cell": name, "count": int(cnt), "area": None})
        cells.sort(key=lambda c: -c["count"]); ranked_by = "count"
    return {
        "total_area": total,
        "seq_area": seq, "seq_pct": seq_pct,
        "comb_area": (None if total is None or seq is None else round(total - seq, 3)),
        "top_cells_ranked_by": ranked_by,
        "top_cells": cells[:8],
    }


def parse_power(p: Path):
    m = re.search(
        r"^Total\s+[\d.eE+-]+\s+[\d.eE+-]+\s+[\d.eE+-]+\s+([\d.eE+-]+)\s+[\d.]+%",
        _read(p), re.MULTILINE)
    return float(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser(description="verification gate loop (config-driven)")
    ap.add_argument("--config", default=str(DESIGNS_DIR / "async_fifo.json"))
    ap.add_argument("--candidate", required=True, help="dir with changed RTL")
    ap.add_argument("--golden", default=None, help="override golden RTL dir")
    ap.add_argument("--work", default=None)
    ap.add_argument("--skip-ppa", action="store_true")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    configure(Path(args.config))
    golden = Path(args.golden).resolve() if args.golden else GOLDEN
    candidate = Path(args.candidate).resolve()
    work = Path(args.work).resolve() if args.work else (DESIGN_ROOT / ".gate_work")
    work.mkdir(parents=True, exist_ok=True)

    work_rtl = build_work_rtl(candidate, golden, work)

    verdict = {"design": CFG["design"], "candidate": str(candidate)}
    verdict["sim"] = gate_sim(work_rtl, work)
    verdict["equiv"] = gate_equiv(golden, work_rtl, work)

    if verdict["sim"]["passed"] and not args.skip_ppa:
        verdict["ppa"] = gate_ppa(work_rtl, work)
    else:
        verdict["ppa"] = None

    verdict["accept"] = bool(verdict["sim"]["passed"])
    verdict["equiv_proven"] = bool(verdict["equiv"].get("proven"))
    verdict["equiv_verified"] = bool(verdict["equiv"].get("verified"))
    verdict["equiv_confidence"] = verdict["equiv"].get("confidence")

    text = json.dumps(verdict, indent=2)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n")

    sys.exit(0 if (verdict["accept"] and verdict["equiv_verified"]) else 1)


if __name__ == "__main__":
    main()
