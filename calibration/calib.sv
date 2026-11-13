// Sampling calibration DUT (see calib.vhd for the description).
module calib (input logic clk);
  logic        clk_int  = 1'b0;
  logic        clk_div  = 1'b0;
  logic [15:0] cnt_ext  = '0;
  logic [15:0] cnt_comb;
  logic [15:0] cnt_int  = '0;
  logic [15:0] cnt_div  = '0;

  always #5 clk_int = ~clk_int;

  always_ff @(posedge clk) begin
    cnt_ext <= cnt_ext + 16'd1;
    clk_div <= ~clk_div;
  end

  assign cnt_comb = cnt_ext;

  always_ff @(posedge clk_int) cnt_int <= cnt_int + 16'd1;
  always_ff @(posedge clk_div) cnt_div <= cnt_div + 16'd1;
endmodule
