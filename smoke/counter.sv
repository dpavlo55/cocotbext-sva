module counter (input logic clk, input logic rst_n, input logic en,
                output logic [7:0] count);
  always_ff @(posedge clk)
    if (!rst_n) count <= '0;
    else if (en) count <= count + 1'b1;

  // Native concurrent assertion: proves SVA runs alongside cocotb.
  a_incr: assert property (@(posedge clk) disable iff (!rst_n)
                           en |=> count == $past(count) + 8'd1)
    else $error("a_incr failed");
endmodule
