// Signal types for reader calibration.
module types (input logic clk);
  typedef enum logic [1:0] {S_IDLE, S_BUSY, S_DONE} state_t;
  logic        sl    /*verilator public_flat_rd*/ = 1'b1;
  logic [7:0]  slv8  /*verilator public_flat_rd*/ = 8'hA5;
  logic [31:0] slv32 /*verilator public_flat_rd*/ = 32'hDEADBEEF;
  logic [39:0] slv40 /*verilator public_flat_rd*/ = 40'h12345678AB;
  logic [0:7]  asc   /*verilator public_flat_rd*/ = 8'hA5;
  logic signed [7:0] sgn /*verilator public_flat_rd*/ = -8'sd3;
  integer      int_  /*verilator public_flat_rd*/ = -7;
  int          i2    /*verilator public_flat_rd*/ = 200;
  bit          bt    /*verilator public_flat_rd*/ = 1'b1;
  state_t      st    /*verilator public_flat_rd*/ = S_BUSY;
  logic        slx   /*verilator public_flat_rd*/ = 1'bx;
  logic [3:0]  slvx  /*verilator public_flat_rd*/ = 4'b1x0z;
endmodule
