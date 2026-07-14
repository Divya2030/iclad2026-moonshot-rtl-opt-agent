// GF(2) probe harness for prim_crc32. Extracts the register next-state function
// F(state,data) by evaluating it at 0, every input basis vector, and some
// pseudo-random vectors. For an affine function these basis responses COMPLETELY
// characterize it over the whole input space, so two designs with equal probe
// output are equivalent for all 2^NIN inputs (see lec_gf2.py). The DUT top is
// renamed to DUT by the gate. Protocol: set_crc loads crc_q = ~crc_in_i, then a
// data_valid cycle computes F; crc_out_o = ~crc_q exposes it.
`timescale 1ns/1ps
module prim_crc32_probe;
  localparam int unsigned BPW = 4;
  localparam int unsigned NIN = 32 + BPW*8;   // state(32) + data(32) = 64
  reg              clk = 0, rst_n = 0, set_crc = 0, dv = 0;
  reg  [31:0]      cin = 0;
  reg  [BPW*8-1:0] d = 0;
  wire [31:0]      cout;
  integer          i;
  reg  [63:0]      lfsr;

  DUT #(.BytesPerWord(BPW)) dut (
    .clk_i(clk), .rst_ni(rst_n), .set_crc_i(set_crc), .crc_in_i(cin),
    .data_valid_i(dv), .data_i(d), .crc_out_o(cout));

  always #5 clk = ~clk;

  // v = {data[31:0], state[31:0]} ; prints F(state,data) as 8 hex digits.
  task automatic eval(input [63:0] v);
    begin
      @(negedge clk); set_crc = 1; dv = 0; cin = ~v[31:0];   // load crc_q = state
      @(posedge clk);                                        //  (registered)
      @(negedge clk); set_crc = 0; dv = 1; d = v[63:32];     // apply data
      @(posedge clk);                                        //  crc_q <= F
      @(negedge clk); dv = 0;
      $display("%08x", ~cout);                               // F = ~crc_out
    end
  endtask

  initial begin
    rst_n = 0; #12 rst_n = 1;
    eval(64'h0);
    for (i = 0; i < NIN; i = i + 1) eval(64'h1 << i);        // basis
    lfsr = 64'hACE1_2345_1234_5678;
    for (i = 0; i < 48; i = i + 1) begin                     // random consistency
      lfsr = {lfsr[62:0], lfsr[63]^lfsr[62]^lfsr[60]^lfsr[59]};
      eval(lfsr);
    end
    $finish;
  end
endmodule
