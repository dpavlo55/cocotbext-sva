-- Top level of the probe benchmark in VHDL: the benchmark design and, after
-- it, the generated probes (external names).
library ieee;
use ieee.std_logic_1164.all;

entity bench_probe is
  port (clk : in std_logic);
end entity;

architecture tb of bench_probe is
begin
  u: entity work.bench port map (clk => clk);
  sva_probe_b1: entity work.sva_probe_b1;
  sva_probe_b64: entity work.sva_probe_b64;
end architecture;
