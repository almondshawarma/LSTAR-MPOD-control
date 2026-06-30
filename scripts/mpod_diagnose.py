"""
MPOD Crate Diagnostics
======================
Checks system-level state and enumerates detected modules so you can tell
whether the MPOD controller can see the iseg HV modules at all 
(major problem we experienced while setting up the MPOD).

Replaces these net-snmp CLI commands
  snmpget  ... sysMainSwitch.0
  snmpget  ... sysStatus.0
  snmpget  ... moduleNumber.0
  snmpwalk ... moduleDescription

OID roots (WIENER-CRATE-MIB.txt):
  wiener  = 1.3.6.1.4.1.19947
  crate   = wiener.1
  system  = crate.1          (system-level scalars)
  output  = crate.3          (module table + channel table)

Scalar OIDs (append .0 for GET):
  sysMainSwitch    = system.1   = 1.3.6.1.4.1.19947.1.1.1
  sysStatus        = system.2   = 1.3.6.1.4.1.19947.1.1.2
  moduleNumber     = output.5   = 1.3.6.1.4.1.19947.1.3.5

Module table (poll indices 1-10):
  moduleDescription= moduleEntry.2 = 1.3.6.1.4.1.19947.1.3.6.1.2.<slot>

Usage:
  python mpod_diagnose.py
  python mpod_diagnose.py --host 192.168.55.8
  python mpod_diagnose.py --host 192.168.55.8 --fix-main-switch
"""

import argparse
import asyncio
import sys

try:
    from puresnmp import V2C, Client
    from puresnmp.types import Integer
    from x690.types import ObjectIdentifier
except ImportError:
    print("ERROR: puresnmp not installed.")
    sys.exit(1)

# ── OIDs ────────────────────────────────────────────────────────────────────

OID_SYS_MAIN_SWITCH  = "1.3.6.1.4.1.19947.1.1.1.0"   # scalar
OID_SYS_STATUS       = "1.3.6.1.4.1.19947.1.1.2.0"   # scalar
OID_MODULE_NUMBER    = "1.3.6.1.4.1.19947.1.3.5.0"   # scalar
OID_MODULE_DESC_BASE = "1.3.6.1.4.1.19947.1.3.6.1.2" # table base (append .slot)

# ── sysStatus bit decoder ────────────────────────────────────────────────────

SYS_STATUS_BITS = {
    0:  "mainOn",
    1:  "mainInhibit",
    2:  "localControlOnly",
    3:  "inputFailure",
    4:  "outputFailure",
    5:  "fantrayFailure",
    6:  "sensorFailure",
    7:  "vmeSysfail",
    8:  "plugAndPlayIncompatible",
    9:  "busReset",
    10: "supplyDerating",
    11: "supplyFailure",
    12: "supplyDerating2",
    13: "supplyFailure2",
    14: "supplyPresent",
    15: "supplyPresent2",
}

def decode_sys_status(raw) -> list:
    """SNMP BITS: MSB of byte 0 = flag 0 in MIB definition."""
    try:
        b = bytes(raw)
    except TypeError:
        return [f"(raw: {raw})"]
    active = []
    for byte_i, byte_val in enumerate(b):
        for bit_i in range(8):
            flag_num = byte_i * 8 + bit_i
            if byte_val & (0x80 >> bit_i):
                label = SYS_STATUS_BITS.get(flag_num, f"bit{flag_num}")
                active.append(label)
    return active if active else ["(no flags set)"]


# ── Helpers ──────────────────────────────────────────────────────────────────

def oid(s):
    return ObjectIdentifier(s)

def safe_str(v):
    try:
        return v.decode("ascii", errors="replace").strip("\x00").strip()
    except AttributeError:
        return str(v).strip()


# ── Main diagnostic ──────────────────────────────────────────────────────────

async def diagnose(host, community, port):
    print(f"\n  MPOD Crate Diagnostics  —  {host}:{port}")
    print(f"  {'='*52}")

    client = Client(host, V2C(community), port=port)

    # ── 1. System scalars ────────────────────────────────────────────────────
    print("\n  [1] System state")
    try:
        results = await client.multiget([
            oid(OID_SYS_MAIN_SWITCH),
            oid(OID_SYS_STATUS),
            oid(OID_MODULE_NUMBER),
        ])
        main_switch_val = int(results[0].value)
        main_switch_str = {
            0: "OFF  <- WARNING: HV backplane supply is disabled",
            1: "ON",
        }.get(main_switch_val, f"Unknown ({main_switch_val})")

        status_flags = decode_sys_status(results[1].value)
        module_count  = int(results[2].value)

        print(f"    sysMainSwitch : {main_switch_str}")
        print(f"    sysStatus     : {', '.join(status_flags)}")
        print(f"    moduleNumber  : {module_count}  (modules detected by controller)")

        flags_set = set(status_flags)
        if "mainOn" not in flags_set:
            print("\n  WARNING: mainOn flag NOT set - crate output power is off.")
            print("     Fix: set sysMainSwitch to 1, or press the physical power button.")
        if "plugAndPlayIncompatible" in flags_set:
            print("\n  WARNING: plugAndPlayIncompatible: module firmware may be too old")
            print("     for this controller firmware version.")
        if "supplyFailure" in flags_set or "supplyFailure2" in flags_set:
            print("\n  WARNING: supplyFailure detected, an internal PSU has faulted.")

    except Exception as e:
        print(f"    ERROR reading system scalars: {e}")
        print("    (Is the host reachable and community string correct?)")
        return

    # ── 2. Module inventory via per-slot GET (avoids walk() version bugs) ────
    # Crate has at most 10 slots (indices 1-10).
    print(f"\n  [2] Module inventory  (polling slots 1-10)")
    MAX_SLOTS = 10
    hv_modules = []
    lv_modules = []

    for slot in range(1, MAX_SLOTS + 1):
        slot_oid = oid(f"{OID_MODULE_DESC_BASE}.{slot}")
        try:
            result = await client.get(slot_oid)
            desc = safe_str(result.value)
            # Skip empty responses that unpopulated slots return on some firmware
            if not desc or set(desc) <= {'\x00', ' '}:
                continue
            is_hv = (
                desc.lower().startswith("iseg") or
                any(x in desc for x in ("EHS", "EBS", "EDS", "EHQ", "NHS", "NHQ"))
            )
            if is_hv:
                hv_modules.append((slot, desc))
            else:
                lv_modules.append((slot, desc))
        except Exception:
            pass  # Slot not populated (expected)

    if lv_modules:
        print(f"\n    LV modules ({len(lv_modules)}):")
        for slot, desc in lv_modules:
            print(f"      slot {slot}  {desc}")
    else:
        print("\n    LV modules: none detected")

    if hv_modules:
        print(f"\n    HV modules ({len(hv_modules)}):")
        for slot, desc in hv_modules:
            print(f"      slot {slot}  {desc}")
    else:
        print("\n    HV modules: NONE DETECTED by controller")

    # ── 3. Verdict ────────────────────────────────────────────────────────────
    print(f"\n  [3] Verdict")
    detected = len(hv_modules) + len(lv_modules)
    print(f"    Modules detected by controller : {detected}")

    if not hv_modules:
        print("""
  The controller cannot see any iseg HV modules.
""")
    print()


async def fix_main_switch(host, community, port):
    client = Client(host, V2C(community), port=port)
    print(f"\n  Setting sysMainSwitch -> ON on {host}...")
    await client.set(oid(OID_SYS_MAIN_SWITCH), Integer(1))
    await asyncio.sleep(1.0)
    result = await client.get(oid(OID_SYS_MAIN_SWITCH))
    val = int(result.value)
    status = "ON (confirmed)" if val == 1 else f"still {val} -- check physical switch"
    print(f"  Readback: sysMainSwitch = {status}\n")


def main():
    p = argparse.ArgumentParser(
        description="MPOD crate diagnostics"
    )
    p.add_argument("--host",            default="192.168.55.8")
    p.add_argument("--community",       default="public",
                   help="Read community (default: public)")
    p.add_argument("--port",            type=int, default=161)
    p.add_argument("--fix-main-switch", action="store_true",
                   help="Also attempt to force sysMainSwitch=ON")
    p.add_argument("--write-community", default="guru")
    args = p.parse_args()

    asyncio.run(diagnose(args.host, args.community, args.port))

    if args.fix_main_switch:
        asyncio.run(fix_main_switch(args.host, args.write_community, args.port))


if __name__ == "__main__":
    main()
