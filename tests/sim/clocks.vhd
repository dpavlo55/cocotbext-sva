-- DUT for clocks that are not driven from cocotb (and several clocks at once).
--   clk      : driven by cocotb
--   clk_div  : clk divided by 2 by a register (derived inside the HDL)
--   clk_int  : generated inside the HDL, 14 ns period, first rising edge at 7 ns
-- clk domain:      cnt counts; req_q <= req; req_qq <= req_q;
--                  send: tgl toggles and data_a <= din
-- clk_div domain:  cap <= cnt; seen <= req_q; seen2 <= seen
--                  (these read the clk domain after its update: the usual race
--                  of a derived clock)
-- clk_int domain:  cnt_i counts; s1..s3 synchronise tgl; valid_b pulses one
--                  cycle per toggle and data_b <= data_a with it
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity clocks is
  port (
    clk       : in std_logic;
    req, send : in std_logic;
    din       : in std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of clocks is
  signal clk_int : std_logic := '0';
  signal clk_div : std_logic := '0';
  signal cnt, cap, cnt_i : unsigned(7 downto 0) := (others => '0');
  signal req_q, req_qq, seen, seen2 : std_logic := '0';
  signal tgl, s1, s2, s3, valid_b : std_logic := '0';
  signal data_a, data_b : std_logic_vector(7 downto 0) := (others => '0');
begin
  clk_int <= not clk_int after 7 ns;

  process (clk) begin
    if rising_edge(clk) then
      cnt <= cnt + 1;
      clk_div <= not clk_div;
      req_q <= req;
      req_qq <= req_q;
      if send = '1' then
        tgl <= not tgl;
        data_a <= din;
      end if;
    end if;
  end process;

  process (clk_div) begin
    if rising_edge(clk_div) then
      cap <= cnt;
      seen <= req_q;
      seen2 <= seen;
    end if;
  end process;

  process (clk_int) begin
    if rising_edge(clk_int) then
      cnt_i <= cnt_i + 1;
      s1 <= tgl; s2 <= s1; s3 <= s2;
      valid_b <= s2 xor s3;
      if (s2 xor s3) = '1' then data_b <= data_a; end if;
    end if;
  end process;
end architecture;
