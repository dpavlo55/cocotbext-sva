-- An 8-bit counter: cleared by rst_n (active low), counts up while en is high.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity counter is
  port (
    clk   : in  std_logic;
    rst_n : in  std_logic;
    en    : in  std_logic;
    count : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of counter is
  signal cnt : unsigned(7 downto 0);
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst_n = '0' then
        cnt <= (others => '0');
      elsif en = '1' then
        cnt <= cnt + 1;
      end if;
    end if;
  end process;
  count <= std_logic_vector(cnt);
end architecture;
