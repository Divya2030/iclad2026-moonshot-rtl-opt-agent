# ICLAD 2026 Hackathon — Presentation Deck (draft)

Track: **Cloud** · Problem category: **NVIDIA RTL optimization** · ~15 slides / 15 min

Fill-ins marked `[[ ]]`. `[VISUAL]` = what to show. `NOTES:` = speaker script.
`[[LIVE]]` = number to refresh from the autonomous Gemini run before submission.

---

## Slide 1 — Title
**Verification-Gated RTL Optimization: optimize aggressively, ship only what's proven correct**
- Team **T16 — Moonshot** · **Divya Kohli**, UC Santa Cruz
- ICLAD 2026 GenAI Chip Hackathon — NVIDIA problems, Cloud Track (also registered On-Prem)

NOTES: We built a generative-AI agent that optimizes RTL for PPA, wrapped in a verification gate that refuses to accept any functionally-incorrect rewrite. Our bet: the winners are decided on **hidden testcases**, so correctness-under-uncertainty is the real prize.

---

## Slide 2 — The problem & how it's scored
- Task: use an LLM agent to optimize IP RTL (async_fifo, sha512, NVDLA, OpenTitan AES/ASCON/PRIM/KMAC).
- Scoring is **gated**: (1) functional correctness (existing testbenches) → then (2) PPA (Yosys/OpenSTA) + LLM calls/token cost.
- **Winners judged at DAC on HIDDEN testcases** — an optimization that passes the visible bench but is subtly wrong scores nothing.

[VISUAL] The gate: `correctness ✅ → PPA measured` vs `correctness ❌ → 0`.
NOTES: The scoring structure rewards correctness first. Hidden tests mean "passes the given TB" is not enough — you need behavior-preserving optimization.

---

## Slide 3 — Key insight (our thesis)
- The failure mode that loses the hackathon: a rewrite that **looks smaller/faster and passes the visible TB but is functionally wrong**.
- We proved this is real: a candidate with lower area that **passed simulation but was formally inequivalent** (and vice-versa).
- **Thesis:** make *formal equivalence* a hard gate in the optimization loop — so the agent can chase PPA aggressively without risking the correctness score.

[VISUAL] Two-column: "PPA-only optimizer" (fast, risky) vs "Verification-gated" (fast + safe).
NOTES: This directly targets the hidden-test judging. It's our core differentiator.

---

## Slide 4 — System architecture
- **Agent (Gemini)** proposes a rewrite → **Gate** (sim + equivalence + PPA) → **adopt iff correct AND improved** → feed result back → iterate.
- Golden RTL is never mutated; every candidate is measured against it.

[VISUAL] Loop diagram:
```
   ┌─ propose (Gemini) ──► apply ──► GATE ──► adopt/reject ─┐
   │      ▲  bottleneck + rejection feedback                │
   └──────┴────────────────────────────────────────────────┘
   GATE = SIM (scored TB) · EQUIV (formal, tiered) · PPA (synth+STA)
```
NOTES: The loop is deterministic Python; the model only proposes. Adoption is decided by measured, reproducible gates — not by the model's say-so.

---

## Slide 5 — Agent design (the proposer)
- **Transformation-driven**, not "make it smaller": the model is given
  - the real **synthesis bottleneck** (seq vs comb area, top cell types),
  - a **transformation menu** grounded in RTLRewriter / ROVER / ASPEN (register/FSM/resource-sharing/operator-restructuring/width/boolean/strength),
  - explicit instruction *not* to repeat what Yosys+ABC already does,
  - **rejection memory** (what failed and why) across rounds.
- Output: surgical substring edits **or** whole-file rewrites (for restructuring).
- Records **LLM calls + tokens** (the scored cost metric).

[VISUAL] The actual prompt (bottleneck + menu + memory), abbreviated.
NOTES: Steering the model at *where the cost is* + *what the tool won't do itself* is what makes proposals land instead of wasting turns.

---

## Slide 6 — The verification gate
- **SIM** — the scored correctness check (SVUT / Icarus / plain-Verilog TBs). Hard gate.
- **EQUIV** — Yosys-native sequential equivalence (bundled SAT + ABC; no external solver → runs in the eval Docker as-is).
- **PPA** — Yosys synth + OpenSTA on ASAP7: area, WNS, worst-slack, power + a cell-level bottleneck.
- **Relative timing gate**: reject only on WNS *regression* (designs can start timing-negative, e.g. sha512 −169 ps).

[VISUAL] verdict.json snippet (sim/equiv/ppa/accept).
NOTES: Everything the agent sees is measured and reproducible. The gate is the source of truth.

---

## Slide 7 — Tiered equivalence + a real finding
- XOR/parity datapaths (CRC/crypto) are **SAT-hard** → stock formal EC can *time out on a correct design*.
- We measured it: the CRC "flatten" defeated **both** Yosys-native equiv **and** ABC SAT-sweeping CEC (both >6 min timeout).
- So the gate **tiers** the evidence, with confidence labeled:
  1. **formal proof** → `proven`
  2. inconclusive → **simulation miter** (directed + 20k random vectors) → `sim-verified`
  3. miter mismatch → `refuted` (hard reject)
- `[[optional]]` GF(2) linear-EC: proves XOR-linear rewrites where SAT can't.

[VISUAL] Ladder: proven → sim-verified → refuted, with the CRC example annotated.
NOTES: This is honest engineering — we know exactly where the tools' ceiling is and we don't hide it; we quantify confidence instead.

---

## Slide 8 — One framework, many IPs
- **Config-driven** (`designs/*.json`): top module, RTL set, sim mode, synth flow, equiv settings.
- Validated on **3 IPs** spanning: Verilog + **SystemVerilog**; 3 sim frameworks (SVUT, Icarus TB, verilator-style pre-DV); 2 synth flows (`run_syn.sh`, `syn.tcl`+**sv2v**).

[VISUAL] Table: async_fifo / sha512 / prim_crc32 × {lang, sim, synth, equiv}.
NOTES: The harness generalizes — adding an IP is a JSON config + (if needed) a small sim fixture, not a rewrite.

---

## Slide 9 — Results: correctness (the gate has teeth)
- Every candidate is sim + equivalence checked before adoption.
- Demonstrated rejections:
  - inverted empty-flag (async_fifo): **sim fail + equiv fail** → rejected.
  - dropped XOR term (prim_crc32 flatten): formal inconclusive → **miter caught it** → `refuted`.
- No functionally-broken rewrite was ever adopted.

[VISUAL] Reject table: change → sim / equiv / decision.
NOTES: The point of the whole system: incorrect rewrites cannot slip through, which is exactly the hidden-test defense.

---

## Slide 10 — Results: PPA wins (gated, correctness-preserving)
| IP | objective | baseline → best | confidence |
|----|-----------|-----------------|------------|
| prim_crc32 | area | 92.35 → 89.40 µm² (**−3.19%**) | equiv-proven |
| prim_crc32 | timing | WNS −282 → **−9 ps** (97% closed) | sim-verified |
| `[[LIVE]]` sha512 | `[[area/timing]]` | `[[baseline → best]]` | `[[ ]]` |
| `[[LIVE]]` async_fifo | area | `[[ ]]` | `[[ ]]` |

[VISUAL] Bar chart of % improvement per IP; mark confidence tier.
NOTES: Every number here survived the correctness gate. `[[LIVE]]` rows come from the autonomous Gemini runs.

---

## Slide 11 — Case study: the CRC datapath flatten
- Bottleneck: 4-deep **serial** byte-CRC chain (~14 logic levels) → WNS −282 ps.
- Rewrite (ASPEN-style): collapse the linear recurrence into one **depth-~5 GF(2) XOR network**.
- Result: **WNS −282 → −9 ps** (timing essentially closed), area +59% (tradeoff), **5000-vector miter PASS**.
- Formal equiv timed out (XOR-hard) → accepted `sim-verified`; a 1-term corruption was `refuted`.

[VISUAL] Before/after datapath depth diagram + the WNS/area numbers.
NOTES: This is the strongest single demonstration — a hard, high-value rewrite, correctly gated, with the confidence honestly labeled.

---

## Slide 12 — Reproducibility / infrastructure
- We operate the **official eval Docker** — and fixed **two real upstream bugs** in it (Yosys `config-clang` removed at HEAD → pinned v0.50; Verilator `VERILATOR_ROOT` misinstall).
- One-command gate: `run_gate.py --config designs/<ip>.json --candidate <rtl>`; agent loop: `opt_agent.py --config … --backend vertex`.
- ASAP7 techlib + sv2v wired; verdict + agent-summary JSON for every run.

[VISUAL] `docker build` → `run_gate` → verdict.json flow.
NOTES: Graders can reproduce every result. Environment competence is itself a differentiator many teams underestimate.

---

## Slide 13 — Cost & efficiency
- Token/LLM-call cost is scored → the loop **records `usage` per run** and adopts only on measured improvement (no churn).
- `[[LIVE]]` autonomous run stats: `[[N calls, M tokens]]` to reach `[[result]]`.
- Levers: bottleneck-targeted prompts (fewer wasted turns), rejection memory (no repeats), cache synth per candidate.

[VISUAL] calls/tokens vs improvement.
NOTES: We treat cost as a first-class metric, not an afterthought.

---

## Slide 14 — Honest limitations & what's next
- **Formal EC ceiling on XOR-heavy rewrites** — mitigated by the tiered miter (sim-verified is weaker than proven; residual risk quantified). Fix: XOR-aware EC (`eqy`/CryptoMiniSat) or the GF(2) linear-EC.
- **Coverage**: 3 of 7 IPs deeply validated `[[update if more]]`.
- **Autonomy**: proposer is live on Gemini `[[LIVE]]`; earlier wins were human-seeded to validate the gate.

[VISUAL] "known limits → mitigation" table.
NOTES: We're explicit about what's proven vs assumed — that credibility matters for a verification-first entry.

---

## Slide 15 — Key takeaways
1. **Verification-gated optimization** = aggressive PPA search that cannot ship a functional regression → built for hidden-test judging.
2. **Tiered, honest confidence** (proven → sim-verified → refuted) instead of pretending everything is proven.
3. **Reusable, config-driven framework** validated across 3 IPs, Verilog + SystemVerilog, two synth flows, in the official Docker.
4. Real, gated wins: **−3.19% area, timing −282→−9 ps** `[[+ LIVE autonomous results]]`.

NOTES: If you optimize without verifying, hidden tests will find you. We verify, so we can optimize hard. Thank you — questions?

---

### Pre-submission checklist
- [ ] Replace all `[[ ]]` / `[[LIVE]]` with real values (esp. autonomous Gemini run numbers).
- [x] Team name/ID, members, affiliation on Slide 1. (T16 Moonshot · Divya Kohli · UCSC)
- [ ] Confirm NVIDIA submission format (runner/entrypoint) before packaging the repo.
- [ ] Export to the organizers' expected format (PDF/PPTX/Slides).
- [ ] Register for DAC (I LOVE DAC promo).
