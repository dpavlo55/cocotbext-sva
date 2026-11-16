// Integration-test DUT (see dut.vhd for the description).
module dut (
  input  logic clk, rst_n, req, drop, start, en, corrupt,
  input  logic [3:0] tag,
  output logic ack, busy, done, rsp,
  output logic [3:0] rtag,
  output logic [7:0] count
);
  typedef enum logic [1:0] {S_IDLE, S_BUSY, S_DONE} state_t;
  state_t state /*verilator public_flat_rd*/ = S_IDLE;
  logic [1:0] timer = '0;
  logic r1 = 0, r2 = 0, p1 = 0, p2 = 0, p3 = 0;
  logic [3:0] t1 = '0, t2 = '0, t3 = '0;
  logic [7:0] cnt = '0;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      r1 <= 0; r2 <= 0; p1 <= 0; p2 <= 0; p3 <= 0;
      state <= S_IDLE; timer <= '0; cnt <= '0;
    end else begin
      r1 <= req & ~drop;
      r2 <= r1;
      p1 <= req; p2 <= p1; p3 <= p2;
      t1 <= corrupt ? ~tag : tag;
      t2 <= t1; t3 <= t2;
      if (en) cnt <= cnt + 8'd1;
      case (state)
        S_IDLE: if (start) begin state <= S_BUSY; timer <= '0; end
        S_BUSY: if (timer == 2'd2) state <= S_DONE; else timer <= timer + 2'd1;
        S_DONE: state <= S_IDLE;
        default: state <= S_IDLE;
      endcase
    end
  end
  assign ack   = r2;
  assign rsp   = p3;
  assign rtag  = t3;
  assign busy  = (state == S_BUSY);
  assign done  = (state == S_DONE);
  assign count = cnt;

  // dump for offline checking (tests/sim/run_offline.py, Icarus): +vcd=<file>
  // (VCD or FST, as chosen by the -vcd / -fst option of vvp)
  string vcd_file;
  initial if ($value$plusargs("vcd=%s", vcd_file)) begin
    $dumpfile(vcd_file);
    $dumpvars(0, dut);
  end
endmodule
