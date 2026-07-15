#!/usr/bin/env python3
# Symbolically flatten the prim_crc32 4-byte CRC recurrence into a single
# GF(2)-linear network (depth-reduced), so synthesis sees a shallow balanced
# XOR tree instead of a 4-deep serial byte-CRC chain. Emits guarded SV; the
# equivalence gate verifies the derived matrix against the golden loop.
#
# Recurrence (linearity of CRC): with L_state(x)=(x>>8)^byte_calc(x[7:0]),
#   s[i+1] = L_state(s[i]) ^ byte_calc(d[i])
# byte_calc(b)[o] = XOR_k B[o][k]*b[k], B[o][k] = (basis[k]>>o)&1.
import sys
from pathlib import Path

BASIS = [0x77073096, 0xee0e612c, 0x076dc419, 0x0edb8832,
         0x1db71064, 0x3b6e20c8, 0x76dc4190, 0xedb88320]
B = [[(BASIS[k] >> o) & 1 for k in range(8)] for o in range(32)]  # B[o][k]

def xorfold(*sets):
    acc = set()
    for s in sets:
        acc ^= s          # symmetric difference == GF(2) XOR (cancel dups)
    return acc

def byte_calc(bits):      # bits: list[set] of length 8 -> list[set] length 32
    return [xorfold(*[bits[k] for k in range(8) if B[o][k]]) for o in range(32)]

def l_state(x):           # x: list[set] length 32 -> list[set] length 32
    bc = byte_calc(x[0:8])
    out = []
    for o in range(32):
        shifted = x[o + 8] if (o + 8) < 32 else set()
        out.append(xorfold(shifted, bc[o]))
    return out

N = 4
# inputs: crc_stages[0] bits -> "q{j}",  data_i bits -> "d{j}"
s = [ {f"q{j}"} for j in range(32) ]
for i in range(N):
    dbyte = [ {f"d{i*8+k}"} for k in range(8) ]
    bcd = byte_calc(dbyte)
    s = [ xorfold(l_state(s)[o], bcd[o]) for o in range(32) ]

def term(v):
    j = int(v[1:])
    return f"crc_stages[0][{j}]" if v[0] == "q" else f"data_i[{j}]"

lines = []
for o in range(32):
    terms = sorted(s[o], key=lambda v: (v[0], int(v[1:])))
    expr = " ^ ".join(term(t) for t in terms) if terms else "1'b0"
    lines.append(f"  assign crc_flat_next[{o}] = {expr};")
flat_assigns = "\n".join(lines)

import re
golden = Path(sys.argv[1]).read_text()

# (A) declare a separately-named flat next-state signal + its assigns, right
# after the crc_stages array declaration. Using a fresh name (NOT crc_stages)
# keeps equiv's name-matching limited to the shared register/outputs, so the
# only real proof obligation is crc_d/crc_q — no false mismatches on unused
# intermediates.
decl = "  logic [31:0] crc_stages[BytesPerWord + 1];\n"
inject = decl + "\n  // depth-reduced (flattened) next-state for BytesPerWord==4\n" \
    + "  logic [31:0] crc_flat_next;\n" + flat_assigns + "\n"
golden, na = golden.replace(decl, inject, 1), 1

# (B) select the flat next-state in crc_d for the BytesPerWord==4 config; keep
# the serial crc_stages chain intact for other widths (pre-DV sim uses 6).
old_comb = ("    if (set_crc_i) begin\n"
            "      crc_d = ~crc_in_i;\n"
            "    end else begin\n"
            "      crc_d = crc_stages[BytesPerWord];\n"
            "    end")
new_comb = ("    if (set_crc_i) begin\n"
            "      crc_d = ~crc_in_i;\n"
            "    end else if (BytesPerWord == 4) begin\n"
            "      crc_d = crc_flat_next;\n"
            "    end else begin\n"
            "      crc_d = crc_stages[BytesPerWord];\n"
            "    end")
new_src, nb = re.subn(re.escape(old_comb), new_comb, golden, count=1)
assert na == 1 and nb == 1, f"inject={na} comb={nb}"
Path(sys.argv[2]).write_text(new_src)
print(f"wrote {sys.argv[2]}; per-bit XOR term counts:",
      [len(s[o]) for o in range(0, 32, 8)], "... max", max(len(x) for x in s))
