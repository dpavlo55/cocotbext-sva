-- Top level for the probe test in VHDL: the DUT and, after it, the generated
-- probes (they refer to the DUT's signals by external names).
library ieee;
use ieee.std_logic_1164.all;

entity dut_probe is
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

architecture tb of dut_probe is
begin
  u_dut: entity work.dut
    port map (clk => clk, rst_n => rst_n, req => req, drop => drop, start => start, en => en,
              corrupt => corrupt, tag => tag, ack => ack, busy => busy, done => done, rsp => rsp,
              rtag => rtag, count => count);
  sva_probe_p1: entity work.sva_probe_p1;
  sva_probe_p16: entity work.sva_probe_p16;
end architecture;
