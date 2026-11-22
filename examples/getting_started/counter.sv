// An 8-bit counter: cleared by rst_n (active low), counts up while en is high.
module counter (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       en,
  output logic [7:0] count
);
  always_ff @(posedge clk)
    if (!rst_n)  count <= '0;
    else if (en) count <= count + 8'd1;
endmodule
