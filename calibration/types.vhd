-- Signal types for reader calibration.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity types is
  port (clk : in std_logic);
end entity;

architecture rtl of types is
  type state_t is (S_IDLE, S_BUSY, S_DONE);
  signal sl    : std_logic := '1';
  signal slv8  : std_logic_vector(7 downto 0) := x"A5";
  signal slv32 : std_logic_vector(31 downto 0) := x"DEADBEEF";
  signal slv40 : std_logic_vector(39 downto 0) := x"12345678AB";
  signal asc   : std_logic_vector(0 to 7) := x"A5";
  signal sgn   : signed(7 downto 0) := to_signed(-3, 8);
  signal int   : integer := -7;
  signal nat   : natural range 0 to 255 := 200;
  signal bool  : boolean := true;
  signal bt    : bit := '1';
  signal st    : state_t := S_BUSY;
  signal slx   : std_logic := 'X';
  signal slvx  : std_logic_vector(3 downto 0) := "1X0Z";
begin
end architecture;
