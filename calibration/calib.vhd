-- Sampling calibration DUT. Three clock kinds, one counter per clock domain.
--   clk      : driven by cocotb
--   clk_int  : generated inside the HDL (free-running, 10 ns)
--   clk_div  : derived from clk inside the HDL (divide by 2, toggled on clk edge)
-- Counters start at 0 and increment on every rising edge of their clock, so a
-- read at the edge callback shows either the pre-edge value (SVA-correct) or
-- the post-edge value (one higher).
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity calib is
  port (clk : in std_logic);
end entity;

architecture rtl of calib is
  signal clk_int  : std_logic := '0';
  signal clk_div  : std_logic := '0';
  signal cnt_ext  : unsigned(15 downto 0) := (others => '0');
  signal cnt_comb : unsigned(15 downto 0);
  signal cnt_int  : unsigned(15 downto 0) := (others => '0');
  signal cnt_div  : unsigned(15 downto 0) := (others => '0');
begin
  clk_int <= not clk_int after 5 ns;

  process (clk) begin
    if rising_edge(clk) then
      cnt_ext <= cnt_ext + 1;
      clk_div <= not clk_div;
    end if;
  end process;

  cnt_comb <= cnt_ext;  -- combinational copy of a register

  process (clk_int) begin
    if rising_edge(clk_int) then cnt_int <= cnt_int + 1; end if;
  end process;

  process (clk_div) begin
    if rising_edge(clk_div) then cnt_div <= cnt_div + 1; end if;
  end process;
end architecture;
