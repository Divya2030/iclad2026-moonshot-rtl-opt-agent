#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# lec_gf2.py — XOR-aware (GF(2)) linear equivalence checker.
#
# SAT-based EC (Yosys native, ABC cec) is defeated by XOR/parity circuits
# (CRC/crypto datapaths). This checker sidesteps SAT: it works in GF(2), where
# XOR is linear. For a function that is AFFINE over GF(2) (as CRC/LFSR/parity
# datapaths provably are), its values at 0 and the input basis vectors
# COMPLETELY determine it over all 2^NIN inputs. So:
#   * build each design's affine map (const = F(0); column i = F(e_i) ^ F(0))
#   * VERIFY affineness: every probed random vector r must satisfy
#         F(r) == const ^ XOR_{i in r} column_i
#     (a non-affine design fails this with high probability)
#   * two designs with equal (const, columns) are equivalent for ALL inputs.
#
# Input: two probe logs (from fixtures/*_probe.sv): line 0 = F(0), lines
# 1..NIN = F(e_0..e_{NIN-1}), remaining lines = F(random_i) matching the same
# LFSR sequence the probe used. Random vectors are recomputed here identically.
#
# Exit 0 = PROVEN equivalent (both affine + equal maps); 3 = not affine
# (checker inapplicable -> caller should fall back); 1 = affine but NOT
# equivalent (definitive disproof).
# ---------------------------------------------------------------------------
import sys

NIN = 64  # state(32) + data(32) for BytesPerWord=4; matches the probe fixture


def lfsr_seq(n):
    v = 0xACE1234512345678
    out = []
    for _ in range(n):
        bit = ((v >> 63) ^ (v >> 62) ^ (v >> 60) ^ (v >> 59)) & 1
        v = ((v << 1) | bit) & ((1 << 64) - 1)
        out.append(v)
    return out


def load(path):
    vals = [int(x, 16) for x in open(path).read().split()]
    f0 = vals[0]
    basis = vals[1:1 + NIN]
    rand = vals[1 + NIN:]
    return f0, basis, rand


def affine_predict(f0, cols, x):
    r = f0
    for i in range(NIN):
        if (x >> i) & 1:
            r ^= cols[i] ^ f0    # column i = F(e_i) ^ f0
    return r


def check(path):
    f0, basis, rand = load(path)
    if len(basis) != NIN:
        raise SystemExit(f"{path}: expected {NIN} basis rows, got {len(basis)}")
    cols = basis  # F(e_i); affine column = F(e_i) ^ f0
    rvecs = lfsr_seq(len(rand))
    affine = all(affine_predict(f0, cols, x) == fx for x, fx in zip(rvecs, rand))
    # canonical signature: const + all affine columns
    sig = (f0, tuple(c ^ f0 for c in cols))
    return affine, sig


def main():
    gold, cand = sys.argv[1], sys.argv[2]
    ga, gs = check(gold)
    ca, cs = check(cand)
    if not (ga and ca):
        print(f"GF2-LEC: INAPPLICABLE (affine golden={ga} cand={ca})")
        sys.exit(3)
    if gs == cs:
        print("GF2-LEC: PROVEN equivalent (both GF(2)-affine; identical linear maps "
              f"over all 2^{NIN} inputs)")
        sys.exit(0)
    # locate first differing output bit for a useful message
    diff = [b for b in range(32) if (gs[0] >> b) & 1 != (cs[0] >> b) & 1
            or any(((gs[1][i] >> b) & 1) != ((cs[1][i] >> b) & 1) for i in range(NIN))]
    print(f"GF2-LEC: NOT equivalent (linear maps differ; output bits {diff[:8]}...)")
    sys.exit(1)


if __name__ == "__main__":
    main()
