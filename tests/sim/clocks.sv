// DUT for clocks that are not driven from cocotb (see clocks.vhd).
`timescale 1ns/1ps
module clocks (
  input logic clk, req, send,
  input logic [7:0] din
);
  logic clk_int = 1'b0;
  logic clk_div = 1'b0;
  logic [7:0] cnt = '0, cap = '0, cnt_i = '0;
  logic req_q = 0, req_qq = 0, seen = 0, seen2 = 0;
  logic tgl = 0, s1 = 0, s2 = 0, s3 = 0, valid_b = 0;
  logic [7:0] data_a = '0, data_b = '0;

  always #7 clk_int = ~clk_int;

  always_ff @(posedge clk) begin
    cnt <= cnt + 8'd1;
    clk_div <= ~clk_div;
    req_q <= req;
    req_qq <= req_q;
    if (send) begin
      tgl <= ~tgl;
      data_a <= din;
    end
  end

  always_ff @(posedge clk_div) begin
    cap <= cnt;
    seen <= req_q;
    seen2 <= seen;
  end

  always_ff @(posedge clk_int) begin
    cnt_i <= cnt_i + 8'd1;
    s1 <= tgl; s2 <= s1; s3 <= s2;
    valid_b <= s2 ^ s3;
    if (s2 ^ s3) data_b <= data_a;
  end

  // dump for offline checking (Icarus): +vcd=<file>
  string vcd_file;
  initial if ($value$plusargs("vcd=%s", vcd_file)) begin
    $dumpfile(vcd_file);
    $dumpvars(0, clocks);
  end
endmodule
