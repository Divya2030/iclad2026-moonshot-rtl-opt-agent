// Harness fixture: iverilog-runnable wrapper for the OpenTitan prim_crc32
// pre-DV testbench. The upstream `prim_crc32_sim` is a verilator-style top
// whose clock/reset are driven by a C++ main; this wrapper supplies them so
// the same self-checking sequence runs under Icarus Verilog. Its $display
// output is compared against pre_dv/prim_crc32/predv_expected.txt by the gate.
`timescale 1ns/1ps
module tb_prim_crc32_top;
  reg clk = 1'b0;
  reg rst_n = 1'b0;
  always #5 clk = ~clk;
  initial begin
    rst_n = 1'b0;
    #23 rst_n = 1'b1;   // release reset off a clock edge
  end
  prim_crc32_sim u_sim (.IO_CLK(clk), .IO_RST_N(rst_n));
endmodule
