-- Integration-test DUT.
--   ack   = req delayed by 2 cycles, unless `drop` was high with the request
--   rsp/rtag = req/tag delayed by 3 cycles (tag corrupted when `corrupt` was high)
--   FSM: start -> BUSY for 3 cycles -> DONE (1 cycle) -> IDLE ; busy/done outputs
--   count: increments while en
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dut is
  port (
    clk, rst_n           : in  std_logic;
    req, drop, start, en : in  std_logic;
    corrupt              : in  std_logic;
    tag                  : in  std_logic_vector(3 downto 0);
    ack, busy, done, rsp : out std_logic;
    rtag                 : out std_logic_vector(3 downto 0);
    count                : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of dut is
  type state_t is (S_IDLE, S_BUSY, S_DONE);
  signal state   : state_t := S_IDLE;
  signal timer   : unsigned(1 downto 0) := (others => '0');
  signal r1, r2  : std_logic := '0';
  signal p1, p2, p3 : std_logic := '0';
  signal t1, t2, t3 : std_logic_vector(3 downto 0) := (others => '0');
  signal cnt     : unsigned(7 downto 0) := (others => '0');
begin
  process (clk) begin
    if rising_edge(clk) then
      if rst_n = '0' then
        r1 <= '0'; r2 <= '0'; p1 <= '0'; p2 <= '0'; p3 <= '0';
        state <= S_IDLE; timer <= (others => '0'); cnt <= (others => '0');
      else
        r1 <= req and not drop;
        r2 <= r1;
        p1 <= req; p2 <= p1; p3 <= p2;
        if corrupt = '1' then t1 <= not tag; else t1 <= tag; end if;
        t2 <= t1; t3 <= t2;
        if en = '1' then cnt <= cnt + 1; end if;
        case state is
          when S_IDLE => if start = '1' then state <= S_BUSY; timer <= "00"; end if;
          when S_BUSY => if timer = "10" then state <= S_DONE; else timer <= timer + 1; end if;
          when S_DONE => state <= S_IDLE;
        end case;
      end if;
    end if;
  end process;
  ack   <= r2;
  rsp   <= p3;
  rtag  <= t3;
  busy  <= '1' when state = S_BUSY else '0';
  done  <= '1' when state = S_DONE else '0';
  count <= std_logic_vector(cnt);
end architecture;
