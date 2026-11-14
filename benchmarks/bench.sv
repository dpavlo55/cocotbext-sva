// Benchmark workload (see bench.vhd for the description).
module bench #(parameter int N_LFSR = 64) (input logic clk);
  logic [31:0] lfsr [N_LFSR];
  logic req    /*verilator public_flat_rd*/ = 1'b0;
  logic req_d1 = 1'b0;
  logic ack    /*verilator public_flat_rd*/ = 1'b0;
  logic         s1   /*verilator public_flat_rd*/;
  logic         rare /*verilator public_flat_rd*/;  // 1 on ~1/256 of cycles (idle workload)
  logic [7:0]   s8   /*verilator public_flat_rd*/;
  logic [31:0]  s32  /*verilator public_flat_rd*/;
  logic [63:0]  s64  /*verilator public_flat_rd*/;
  logic [127:0] s128 /*verilator public_flat_rd*/;
  logic [511:0] s512 /*verilator public_flat_rd*/;

  initial for (int i = 0; i < N_LFSR; i++) lfsr[i] = 32'h1234567 + i * 7919 + 1;

  // plain always: lfsr also has an initial value assignment above
  always @(posedge clk) begin
    for (int i = 0; i < N_LFSR; i++)
      lfsr[i] <= lfsr[i][0] ? ({1'b0, lfsr[i][31:1]} ^ 32'h80200003) : {1'b0, lfsr[i][31:1]};
    req    <= lfsr[0][3] & lfsr[0][7];
    req_d1 <= req;
    ack    <= req_d1;
  end

  assign s1   = lfsr[0][0];
  assign rare = (lfsr[9][7:0] == 8'h5A);
  assign s8   = lfsr[0][7:0];
  assign s32  = lfsr[1];
  assign s64  = {lfsr[2], lfsr[3]};
  assign s128 = {lfsr[4], lfsr[5], lfsr[6], lfsr[7]};
  genvar g;
  for (g = 0; g < 16; g++) begin : gen512
    assign s512[32*g +: 32] = lfsr[8 + g];
  end
endmodule
