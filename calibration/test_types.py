"""Reader calibration: what each simulator reports for common signal types,
and which low-level reads return the correct value."""
import json
import os

import cocotb
from cocotb.triggers import Timer

SIM = os.environ.get("CALIB_SIM", "?")
LANG = os.environ.get("CALIB_LANG", "?")
NAMES = ["sl", "slv8", "slv32", "slv40", "asc", "sgn", "int", "int_", "nat", "i2", "bool", "bt",
         "st", "slx", "slvx"]


@cocotb.test()
async def probe(dut):
    await Timer(1, unit="ns")
    out = os.environ.get("CALIB_OUT")
    for name in NAMES:
        try:
            h = getattr(dut, name)
        except AttributeError:
            continue
        ll = h._handle
        info = {"sim": SIM, "lang": LANG, "sig": name, "cls": type(h).__name__,
                "type": ll.get_type_string(), "range": list(ll.get_range()) if hasattr(ll, "get_range") else None,
                "signed": ll.get_signed(), "len": len(h) if hasattr(h, "__len__") else None}
        try:
            info["value"] = str(h.value)
        except Exception as e:
            info["value"] = f"ERR {e}"
        for m in ("get_signal_val_binstr", "get_signal_val_long"):
            try:
                info[m] = getattr(ll, m)()
            except Exception as e:
                info[m] = f"ERR {type(e).__name__}"
        cocotb.log.info("TYPE %s", json.dumps(info))
        if out:
            with open(out, "a") as f:
                f.write(json.dumps(info) + "\n")
