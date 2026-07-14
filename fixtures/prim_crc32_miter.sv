// Simulation miter for prim_crc32: drives the golden and candidate DUTs with
// identical stimulus (directed edge cases + pseudo-random) and flags any digest
// mismatch. Used as the TIERED FALLBACK when formal equivalence is inconclusive
// (XOR/parity CEC is SAT-hard). The gate compiles the golden and candidate with
// their top module renamed to DUT_GOLD / DUT_CAND (see designs/*.json).
`timescale 1ns/1ps
module prim_crc32_miter;
  localparam int unsigned BPW = 4;   // synth/equiv config width
  reg               clk = 1'b0, rst_n = 1'b0;
  reg               set_crc = 1'b0, dv = 1'b0;
  reg  [31:0]       cin = 32'h0;
  reg  [BPW*8-1:0]  d = '0;
  wire [31:0]       og, oc;
  integer           i, errors = 0;
  reg  [31:0]       lfsr = 32'h1;

  DUT_GOLD #(.BytesPerWord(BPW)) g (
    .clk_i(clk), .rst_ni(rst_n), .set_crc_i(set_crc), .crc_in_i(cin),
    .data_valid_i(dv), .data_i(d), .crc_out_o(og));
  DUT_CAND #(.BytesPerWord(BPW)) c (
    .clk_i(clk), .rst_ni(rst_n), .set_crc_i(set_crc), .crc_in_i(cin),
    .data_valid_i(dv), .data_i(d), .crc_out_o(oc));

  always #5 clk = ~clk;

  task automatic step(input [BPW*8-1:0] data);
    begin dv = 1'b1; d = data; @(negedge clk);
      if (og !== oc) begin
        errors = errors + 1;
        if (errors < 5) $display("MISMATCH: data=%h gold=%08x cand=%08x", data, og, oc);
      end
    end
  endtask

  initial begin
    rst_n = 1'b0; #12 rst_n = 1'b1;
    @(negedge clk); set_crc = 1'b1; cin = 32'h0; @(negedge clk); set_crc = 1'b0;
    // directed edge vectors
    step('0); step('1); step({BPW*8{1'b1}});
    for (i = 0; i < BPW*8; i = i + 1) step(1 << i);          // walking-ones
    // reseed + long pseudo-random sweep (LFSR, deterministic across sims)
    for (i = 0; i < 20000; i = i + 1) begin
      lfsr = {lfsr[30:0], lfsr[31]^lfsr[21]^lfsr[1]^lfsr[0]};
      step({lfsr, ~lfsr} & {BPW*8{1'b1}});
    end
    if (errors == 0) $display("MITER PASS (%0d vectors)", 3 + BPW*8 + 20000);
    else             $display("MITER FAIL: %0d mismatches", errors);
    $finish;
  end
endmodule
