"""
MPOD Channel Probe
====================================
Reads all populated channels from a WIENER MPOD via SNMP and prints
a table of names, set/measured voltages, set/measured currents,
switch state, and decoded status flags.

Install:  pip install puresnmp

Usage:
    python mpod_probe.py --host 192.168.55.8
    python mpod_probe.py --host 192.168.55.8 --community public

OID reference
-------------
All OIDs derived directly from WIENER-CRATE-MIB.txt (rev 7280, 2025-12-10).

OID tree:
    enterprises(1.3.6.1.4.1) . wiener(19947) . crate(1) . output(3)
        . outputTable(2) . outputEntry(1) . <field> . <channel_index>

Full base: 1.3.6.1.4.1.19947.1.3.2.1

outputEntry fields used here (from MIB OutputEntry SEQUENCE):
    .1  outputIndex                         INTEGER  (table key, NOT walked directly)
    .2  outputName                          DisplayString
    .4  outputStatus                        BITS     (encoded as OCTET STRING)
    .5  outputMeasurementSenseVoltage       Float    (Opaque-encoded IEEE 754)
    .7  outputMeasurementCurrent            Float    (Opaque-encoded IEEE 754)
    .9  outputSwitch                        INTEGER  {off(0), on(1), ...}
    .10 outputVoltage                       Float    (Opaque-encoded IEEE 754) — set/target
    .12 outputCurrent                       Float    (Opaque-encoded IEEE 754) — current limit
    .13 outputVoltageRiseRate               Float    (Opaque-encoded IEEE 754)
    .14 outputVoltageFallRate               Float    (Opaque-encoded IEEE 754)

Channel index offset
--------------------
The MIB defines outputIndex as: u0(1), u1(2), ..., u700(701), u701(702), ...
SMI table indices start at 1, so channel u<N> has OID suffix N+1.
When a walk returns OID suffix K, the WIENER channel name is u{K-1}.

Float encoding
--------------
WIENER uses a non-standard Opaque Float sub-encoding (MIB Float TEXTUAL-CONVENTION):
    BER tag: 9f 78  (APPLICATION 31, constructed)
    Length:  04
    Value:   4-byte big-endian IEEE 754 single-precision float
Full Opaque wrapper: 44 07 9f 78 04 <4 bytes>
puresnmp strips the outer Opaque tag and returns the inner 7 bytes: 9f 78 04 <4 bytes>.

Status BITS encoding
--------------------
outputStatus is SMIv2 BITS type, encoded as OCTET STRING.
Bit N is at byte N//8, bit position (7 - N%8)  (bit 0 = MSB of byte 0).
"""

import argparse
import asyncio
import struct
import sys
from typing import Optional

try:
    import puresnmp
    from puresnmp import V2C, Client
    from x690.types import ObjectIdentifier
except ImportError:
    print("ERROR: puresnmp not installed.  Run:  pip install puresnmp")
    sys.exit(1)


# OID definitions, cites WIENER-CRATE-MIB.txt field number

_ENTRY = "1.3.6.1.4.1.19947.1.3.2.1"   # outputEntry base

OIDS = {
    # field .2  MIB: outputName  DisplayString
    "name":       ObjectIdentifier(f"{_ENTRY}.2"),

    # field .4  MIB: outputStatus  BITS (OCTET STRING encoded)
    "status":     ObjectIdentifier(f"{_ENTRY}.4"),

    # field .5  MIB: outputMeasurementSenseVoltage  Float [V]
    "volt_meas":  ObjectIdentifier(f"{_ENTRY}.5"),

    # field .7  MIB: outputMeasurementCurrent  Float [A]
    "curr_meas":  ObjectIdentifier(f"{_ENTRY}.7"),

    # field .9  MIB: outputSwitch  INTEGER {off(0), on(1), ...}
    "switch":     ObjectIdentifier(f"{_ENTRY}.9"),

    # field .10 MIB: outputVoltage  Float [V]  — the SET/target voltage
    "volt_set":   ObjectIdentifier(f"{_ENTRY}.10"),

    # field .12 MIB: outputCurrent  Float [A]  — the current LIMIT
    "curr_set":   ObjectIdentifier(f"{_ENTRY}.12"),

    # field .13 MIB: outputVoltageRiseRate  Float [V/s]
    "rise_rate":  ObjectIdentifier(f"{_ENTRY}.13"),

    # field .14 MIB: outputVoltageFallRate  Float [V/s]
    "fall_rate":  ObjectIdentifier(f"{_ENTRY}.14"),
}


# outputStatus bit definitions
# Source: WIENER-CRATE-MIB.txt, outputStatus BITS { ... } ::= { outputEntry 4 }
# Encoding: bit N -> byte N//8, bit position (7 - N%8)  [bit 0 = MSB of byte 0]──

STATUS_BITS = {
    0:  "On",
    1:  "Inhibit",
    2:  "FailMinSenseV",
    3:  "FailMaxSenseV",
    4:  "FailMaxTerminalV",
    5:  "FailMaxCurrent",
    6:  "FailMaxTemp",
    7:  "FailMaxPower",
    8:  "FailCacheUpdate",
    9:  "FailTimeout",
    10: "CurrentLimited",
    11: "RampUp",
    12: "RampDown",
    13: "EnableKill",
    14: "EmergencyOff",
    15: "Adjusting",
    16: "ConstantVoltage",
}

SWITCH_LABELS = {0: "Off", 1: "On", 2: "resetEmergOff",
                 3: "setEmergOff", 10: "clearEvents"}


# Value decoders

def decode_float(value) -> Optional[float]:
    """Decode WIENER Opaque Float.
    puresnmp returns full Opaque BER: 44 <len> 9f 78 04 <4-byte IEEE 754>
    (outer 44 = Opaque tag, inner 9f 78 04 = WIENER float sub-tag)
    Source: WIENER-CRATE-MIB.txt Float TEXTUAL-CONVENTION, example:
      value 123 inner='9f780442f60000', full Opaque='44079f780442f60000'
    """
    try:
        raw = bytes(value)
        # Full Opaque wrapper present (puresnmp default)
        if len(raw) >= 9 and raw[0] == 0x44 and raw[2] == 0x9f and raw[3] == 0x78 and raw[4] == 0x04:
            return struct.unpack('>f', raw[5:9])[0]
        # Inner only (outer wrapper already stripped)
        if len(raw) >= 7 and raw[0] == 0x9f and raw[1] == 0x78 and raw[2] == 0x04:
            return struct.unpack('>f', raw[3:7])[0]
        # Double-precision variants
        if len(raw) >= 13 and raw[0] == 0x44 and raw[2] == 0x9f and raw[3] == 0x79 and raw[4] == 0x08:
            return struct.unpack('>d', raw[5:13])[0]
        if len(raw) >= 11 and raw[0] == 0x9f and raw[1] == 0x79 and raw[2] == 0x08:
            return struct.unpack('>d', raw[3:11])[0]
    except (TypeError, struct.error):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decode_status(value) -> str:
    """
    Decode outputStatus BITS value (OCTET STRING encoding).

    SMIv2 BITS: bit N is at byte N//8, bit position (7 - N%8).
    Bit 0 = MSB of first byte.

    Source: WIENER-CRATE-MIB.txt, outputStatus BITS definition.
    """
    try:
        raw = bytes(value)
    except TypeError:
        return f"raw={value}"

    flags = []
    for bit_num, name in STATUS_BITS.items():
        byte_idx   = bit_num // 8
        bit_in_byte = 7 - (bit_num % 8)
        if byte_idx < len(raw) and (raw[byte_idx] >> bit_in_byte) & 1:
            flags.append(name)

    return ", ".join(flags) if flags else "Idle"


def decode_str(value) -> str:
    """Decode channel name. x690 OctetString repr is OctetString(b'U700')."""
    try:
        return bytes(value).decode("ascii", errors="replace").strip("\x00")
    except TypeError:
        pass
    s = str(value)
    # x690 repr format: "OctetString(b'U700')"
    if "OctetString" in s:
        try:
            return s.split("b'")[1].rstrip("')")
        except IndexError:
            pass
    return s.strip()


def oid_suffix_to_channel(idx: int) -> tuple[str, int, int]:
    """
    Convert SNMP OID suffix to WIENER channel label and slot/channel numbers.

    MIB rule (outputIndex enum): u<N> has OID suffix N+1.
    So OID suffix idx  ->  channel u{idx-1}  ->  slot (idx-1)//100, ch (idx-1)%100.

    Source: WIENER-CRATE-MIB.txt outputIndex OBJECT-TYPE, comment:
    "SMI index starts at 1, so index 1 corresponds to U0."
    """
    n    = idx - 1
    slot = n // 100
    ch   = n  % 100
    return f"u{n}", slot, ch


# SNMP helpers

async def walk_oid(client: Client, oid: ObjectIdentifier) -> dict:
    """Walk one OID subtree; return {oid_suffix: raw_value}."""
    results = {}
    try:
        async for varbind in client.walk(oid):
            suffix = int(str(varbind.oid).rsplit(".", 1)[-1])
            results[suffix] = varbind.value
    except Exception as exc:
        print(f"  [WARN] walk failed for {oid}: {exc}")
    return results


# Main probe

def decode_int(value) -> Optional[int]:
    """Convert x690 Integer (or plain int) to Python int.
    puresnmp returns SNMP INTEGER as x690.types.Integer whose
    str() representation is 'Integer(N)' — int() won't accept it directly."""
    if value is None:
        return None
    try:
        return int(value)
    except TypeError:
        s = str(value)
        if s.startswith("Integer(") and s.endswith(")"):
            return int(s[8:-1])
        try:
            return int(s)
        except ValueError:
            return None

async def probe(host: str, community: str, port: int = 161) -> None:
    print(f"\n  WIENER MPOD Channel Probe")
    print(f"  {'─'*42}")
    print(f"  Host      : {host}:{port}")
    print(f"  Community : {community}")
    print()

    client = Client(host, V2C(community), port=port)

    # Discover channels via outputName (.2)
    print("  Walking outputName (.2) to discover channels...", end=" ", flush=True)
    names = await walk_oid(client, OIDS["name"])
    print(f"{len(names)} channel(s) found.")

    if not names:
        print("\n  No channels returned.  Checklist:")
        print("    • Can you ping the MPOD?")
        print("    • Community string correct? (default: public)")
        print("    • UDP 161 unblocked in Windows Firewall?")
        print("    • MPOD powered on with modules installed?")
        return

    print("  Fetching voltage / current / status fields...", end=" ", flush=True)
    volt_set  = await walk_oid(client, OIDS["volt_set"])
    curr_set  = await walk_oid(client, OIDS["curr_set"])
    volt_meas = await walk_oid(client, OIDS["volt_meas"])
    curr_meas = await walk_oid(client, OIDS["curr_meas"])
    switches  = await walk_oid(client, OIDS["switch"])
    statuses  = await walk_oid(client, OIDS["status"])
    print("done.\n")

    # Table layout
    cols = [16, 10, 12, 12, 12, 12, 14, 36]
    hdrs = ["Channel", "Name", "V_set (V)", "V_meas (V)",
            "I_lim (mA)", "I_meas (mA)", "Switch", "Status"]
    sep  = "  ".join("─" * w for w in cols)
    hdr  = "  ".join(h.ljust(w) for h, w in zip(hdrs, cols))

    print(f"  {sep}")
    print(f"  {hdr}")
    print(f"  {sep}")

    for idx in sorted(names.keys()):
        ch_label, slot, ch = oid_suffix_to_channel(idx)
        label    = f"{ch_label} (s{slot}c{ch:02d})"
        name     = decode_str(names[idx])

        vs = decode_float(volt_set.get(idx))
        vm = decode_float(volt_meas.get(idx))
        cs = decode_float(curr_set.get(idx))
        cm = decode_float(curr_meas.get(idx))
        sw = switches.get(idx)
        st = statuses.get(idx)

        vs_str = f"{vs:+.3f}"        if vs is not None else "?"
        vm_str = f"{vm:+.3f}"        if vm is not None else "?"
        cs_str = f"{cs*1e3:.4f}"     if cs is not None else "?"
        cm_str = f"{cm*1e3:.4f}"     if cm is not None else "?"
        sw_int = decode_int(sw)
        sw_str = SWITCH_LABELS.get(sw_int, str(sw_int)) if sw_int is not None else "?"
        st_str = decode_status(st)   if st is not None else "?"

        row = [label, name, vs_str, vm_str, cs_str, cm_str, sw_str, st_str]
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, cols)))

    print(f"  {sep}")

    active = sum(1 for idx in names
                 if statuses.get(idx) and
                 len(bytes(statuses[idx])) > 0 and
                 (bytes(statuses[idx])[0] & 0x80))  # bit 0 = outputOn = MSB byte 0

    print(f"\n  Total channels : {len(names)}")
    print(f"  Channels On    : {active}")
    print(f"  Channels Off   : {len(names) - active}\n")

    # Opaque float diagnostic, only shown if floats are still not decoding
    if volt_set and all(decode_float(v) is None for v in volt_set.values()):
        sample = next(iter(volt_set.values()))
        print(f"  [HINT] Voltage floats not decoded.")
        print(f"         Raw volt_set bytes: {bytes(sample).hex()}")
        print(f"         Expected: 9f 78 04 <4-byte IEEE 754>")
        print(f"         (See WIENER-CRATE-MIB.txt Float TEXTUAL-CONVENTION)")


def main():
    p = argparse.ArgumentParser(
        description="WIENER MPOD channel probe, verifies SNMP connectivity "
                    "and reads all channel data."
    )
    p.add_argument("--host",      default="192.168.1.100",
                   help="MPOD IP address")
    p.add_argument("--community", default="public",
                   help="SNMP read community (default: public)")
    p.add_argument("--port",      type=int, default=161,
                   help="SNMP UDP port (default: 161)")
    args = p.parse_args()
    asyncio.run(probe(args.host, args.community, args.port))


if __name__ == "__main__":
    main()
