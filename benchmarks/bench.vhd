-- Benchmark workload: N_LFSR free-running 32-bit Galois LFSRs (simulation load)
-- plus a handshake where ack follows req by exactly 2 cycles, so the property
-- req |-> ##[1:3] ack always holds. s1..s512 are read-cost probes.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity bench is
  generic (N_LFSR : positive := 64);
  port (clk : in std_logic);
end entity;

architecture rtl of bench is
  type lfsr_array is array (natural range <>) of std_logic_vector(31 downto 0);

  function seed(i : natural) return std_logic_vector is
  begin
    return std_logic_vector(to_unsigned(16#1234567# + i * 7919 + 1, 32));
  end function;

  function init_lfsrs return lfsr_array is
    variable r : lfsr_array(0 to N_LFSR - 1);
  begin
    for i in r'range loop r(i) := seed(i); end loop;
    return r;
  end function;

  signal lfsr : lfsr_array(0 to N_LFSR - 1) := init_lfsrs;
  signal req, req_d1, ack : std_logic := '0';
  signal s1   : std_logic;
  signal rare : std_logic;                       -- 1 on ~1/256 of cycles (idle workload)
  signal s8   : std_logic_vector(7 downto 0);
  signal s32  : std_logic_vector(31 downto 0);
  signal s64  : std_logic_vector(63 downto 0);
  signal s128 : std_logic_vector(127 downto 0);
  signal s512 : std_logic_vector(511 downto 0);
begin
  process (clk)
    variable v : std_logic_vector(31 downto 0);
  begin
    if rising_edge(clk) then
      for i in lfsr'range loop
        v := lfsr(i);
        if v(0) = '1' then
          lfsr(i) <= ('0' & v(31 downto 1)) xor x"80200003";
        else
          lfsr(i) <= '0' & v(31 downto 1);
        end if;
      end loop;
      req    <= lfsr(0)(3) and lfsr(0)(7);   -- ~25 % of cycles
      req_d1 <= req;
      ack    <= req_d1;
    end if;
  end process;

  s1   <= lfsr(0)(0);
  rare <= '1' when lfsr(9)(7 downto 0) = x"5A" else '0';
  s8   <= lfsr(0)(7 downto 0);
  s32  <= lfsr(1);
  s64  <= lfsr(2) & lfsr(3);
  s128 <= lfsr(4) & lfsr(5) & lfsr(6) & lfsr(7);
  gen512 : for i in 0 to 15 generate
    s512(32 * i + 31 downto 32 * i) <= lfsr(8 + i);
  end generate;
end architecture;
