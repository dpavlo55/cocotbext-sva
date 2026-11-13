"""Fast signal readers chosen per handle.

The public `handle.value` builds Logic/LogicArray objects (5-10x the cost of a
raw GPI read for narrow signals). This module is the only place that uses the
private low-level handle (`handle._handle`); if it is unavailable the public
API is used instead.

Reader choice (from phase 0 / reader calibration on nvc, GHDL, Icarus,
Verilator and a commercial simulator):

* Logic scalars / vectors through VPI (Verilog simulators, and GHDL for VHDL):
  `get_signed()` returns 0/1. GPI long is correct up to 32 bits, but may be
  sign-extended (signed objects, 32-bit values): mask to the width.
* Logic scalars / vectors through VHPI or FLI (VHDL in nvc, commercial
  simulators): `get_signed()` returns -1 and GPI long is wrong (enum
  positions, 0, or an error value). Use the binary string.
* Wider than 32 bits: binary string.
* Integer / enum objects: GPI long (32-bit), masked.

Unknown bits (X, Z, U, W, -) read as 0, H as 1, L as 0. X masks are read
separately from the binary string, only for $isunknown / $countbits.
"""
from __future__ import annotations

from dataclasses import dataclass

_TO_BIN = str.maketrans({"H": "1", "h": "1", "L": "0", "l": "0", "X": "0", "x": "0", "Z": "0",
                         "z": "0", "U": "0", "u": "0", "W": "0", "w": "0", "-": "0"})
_TO_XMASK = str.maketrans({"0": "0", "1": "0", "H": "0", "h": "0", "L": "0", "l": "0",
                           "X": "1", "x": "1", "Z": "1", "z": "1", "U": "1", "u": "1",
                           "W": "1", "w": "1", "-": "1"})


@dataclass
class SignalInfo:
    reader: object        # () -> int
    width: int
    signed: bool
    left: int | None      # declared range, None for scalars
    right: int | None
    vpi: bool             # accessed through VPI (long reads valid)
    label: str


def _binstr_to_int(get):
    def read():
        s = get()
        try:
            return int(s, 2)
        except ValueError:
            return int(s.translate(_TO_BIN), 2) if s else 0
    return read


def _bit_reader(get):
    def read():
        c = get()
        return 1 if c == "1" or c == "H" or c == "h" else 0
    return read


def signal_info(handle, label: str = "") -> SignalInfo:
    """Inspect a cocotb value handle and choose the fastest correct reader."""
    from cocotb.handle import EnumObject, IntegerObject, LogicArrayObject, LogicObject, RealObject

    label = label or getattr(handle, "_path", "") or str(handle)
    ll = getattr(handle, "_handle", None)
    if isinstance(handle, RealObject):
        from ._errors import SvaUnsupportedError
        raise SvaUnsupportedError(f"real-valued signal '{label}' is not supported in assertions")
    if ll is None:  # no low-level handle: public API fallback
        return _public_info(handle, label)

    signed_flag = ll.get_signed()
    if isinstance(handle, (IntegerObject, EnumObject)):
        get = ll.get_signal_val_long
        width = 32
        m = (1 << width) - 1
        return SignalInfo(lambda: get() & m, width, isinstance(handle, IntegerObject) and signed_flag != 0,
                          None, None, True, label)

    is_scalar = isinstance(handle, LogicObject)
    width = 1 if is_scalar else len(handle)
    left = right = None
    if not is_scalar:
        try:
            left, right, _ = ll.get_range()
        except Exception:
            left, right = width - 1, 0
    vpi = signed_flag in (0, 1)
    signed = signed_flag == 1
    if vpi and width <= 32:
        get = ll.get_signal_val_long
        m = (1 << width) - 1
        reader = lambda: get() & m  # noqa: E731
    elif is_scalar:
        reader = _bit_reader(ll.get_signal_val_binstr)
    else:
        reader = _binstr_to_int(ll.get_signal_val_binstr)
    return SignalInfo(reader, width, signed, left, right, vpi, label)


def x_reader(handle):
    """() -> X mask of the handle (bits that are X/Z/U/W/-)."""
    ll = getattr(handle, "_handle", None)
    if ll is None:
        return lambda: int(str(handle.value).translate(_TO_XMASK) or "0", 2)
    get = ll.get_signal_val_binstr

    def read():
        s = get()
        if not s or s.isdigit() and set(s) <= {"0", "1"}:
            return 0
        try:
            return int(s.translate(_TO_XMASK), 2)
        except ValueError:
            return 0
    return read


def _public_info(handle, label):
    v = handle.value
    width = len(v) if hasattr(v, "__len__") else 32

    def read():
        val = handle.value
        try:
            return int(val)
        except (ValueError, TypeError):
            return int(str(val).translate(_TO_BIN) or "0", 2)
    return SignalInfo(read, width, False, None, None, False, label)
