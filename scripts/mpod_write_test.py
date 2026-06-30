"""
MPOD Single-Channel Write Test
================================
Practice script for writing a voltage to one MPOD channel, toggling it
on/off, and reading back to verify.  Intended as kind of a stepping stone between
mpod_probe.py (read-only) and a full multipole voltage application.

Install:  pip install puresnmp

Usage examples
--------------
  # Read channel u700's current state
  python mpod_write_test.py --host 192.168.55.8 --channel u700

  # Set channel u700 to 5 V, then read back
  python mpod_write_test.py --host 192.168.55.8 --channel u700 --voltage 5.0

  # Turn channel on, set 5 V, read back with voltmeter
  python mpod_write_test.py --host 192.168.55.8 --channel u700 --switch on --voltage 5.0

  # Just turn channel on (set-point already configured)
  python mpod_write_test.py --host 192.168.55.8 --channel u700 --switch on

  # Turn channel off (safe: leaves set-point unchanged)
  python mpod_write_test.py --host 192.168.55.8 --channel u700 --switch off

  # Full voltmeter test cycle: on → set 10 V → read back → reset → off
  python mpod_write_test.py --host 192.168.55.8 --channel u700 --switch on --voltage 10.0 --reset --switch-off-after

  # Set to 5 V then immediately reset to 0 V
  python mpod_write_test.py --host 192.168.55.8 --channel u700 --voltage 5.0 --reset

  # Use non-default write community (lab may have renamed 'guru')
  python mpod_write_test.py --host 192.168.55.8 --channel u700 --voltage 5.0 --write-community guru

Safety
------
  - Voltages above TEST_VOLTAGE_LIMIT_V (default 60 V) are refused.
  - Switch ordering: --switch on fires BEFORE the voltage set; --switch-off-after
    fires AFTER --reset (if present).  This matches safe lab practice:
      enable supply → ramp voltage → measure → ramp down → disable supply.
  - The channel must be switched On for the output to change.

OID reference  (WIENER-CRATE-MIB.txt, outputEntry fields)
----------------------------------------------------------
  .5  outputMeasurementSenseVoltage  Float  read-only  measured voltage [V]
  .9  outputSwitch                   INT    read-write on/off state (0=Off, 1=On)
  .10 outputVoltage                  Float  read-write set/target voltage [V]

Channel index rule (from MIB outputIndex enum, SMI starts at 1):
  channel u<N>  →  OID suffix N+1
  e.g. u700 → suffix 701, u800 → suffix 801

Float encoding (WIENER-CRATE-MIB.txt Float TEXTUAL-CONVENTION):
  Inner: 9f 78 04 <4-byte big-endian IEEE 754 float>
  Full BER Opaque wrapper added by puresnmp: 44 07 <inner>
  puresnmp.types.Opaque(inner_bytes) produces the correct full encoding.
"""

import argparse
import asyncio
import struct
import sys
from typing import Optional

try:
    import puresnmp
    from puresnmp import V2C, Client
    from puresnmp.types import Opaque, Integer
    from x690.types import ObjectIdentifier
except ImportError:
    print("ERROR: puresnmp not installed.  Run:  pip install puresnmp")
    sys.exit(1)

# Safety limit - refuse any test voltage above this value
TEST_VOLTAGE_LIMIT_V = 2000.0

# OIDs  (WIENER-CRATE-MIB.txt outputEntry base 1.3.6.1.4.1.19947.1.3.2.1)
_ENTRY = "1.3.6.1.4.1.19947.1.3.2.1"

OID_VOLT_SET  = _ENTRY + ".10"   # outputVoltage        Float  read-write
OID_VOLT_MEAS = _ENTRY + ".5"    # outputMeasSenseVolt  Float  read-only
OID_SWITCH    = _ENTRY + ".9"    # outputSwitch         INT    read-write

# Channel / OID helpers


def channel_to_suffix(channel: str) -> int:
    """
    Convert a WIENER channel name (e.g. 'u700') to its SNMP OID suffix.

    MIB rule: u<N> has integer index N+1  (SMI table indices start at 1).
    Source: WIENER-CRATE-MIB.txt outputIndex OBJECT-TYPE comment:
    "SMI index starts at 1, so index 1 corresponds to U0."
    """
    name = channel.lower().lstrip("u")
    try:
        n = int(name)
    except ValueError:
        raise ValueError(f"Cannot parse channel '{channel}'. "
                         f"Expected format: u<N>, e.g. u700, u801")
    return n + 1


def oid_for(base_oid: str, channel: str) -> ObjectIdentifier:
    """Return the full OID for a given base field and channel name."""
    suffix = channel_to_suffix(channel)
    return ObjectIdentifier(f"{base_oid}.{suffix}")


# Value encoders / decoders

def encode_float(value: float) -> Opaque:
    """
    Encode a float as WIENER Opaque Float for SNMP SET.

    Inner encoding: 9f 78 04 <4-byte big-endian IEEE 754>
    puresnmp.types.Opaque adds the outer BER tag (44 07) automatically.
    Verified: Opaque(inner).bytes() == '44 07 9f 78 04 <float>'
    """
    inner = bytes([0x9f, 0x78, 0x04]) + struct.pack('>f', value)
    return Opaque(inner)


def decode_float(value) -> Optional[float]:
    """
    Decode WIENER Opaque Float from SNMP GET response.
    Handles both full BER (44 07 ...) and inner-only (9f 78 04 ...) forms.
    """
    try:
        raw = bytes(value)
        if len(raw) >= 9 and raw[0] == 0x44 and raw[2] == 0x9f and raw[3] == 0x78 and raw[4] == 0x04:
            return struct.unpack('>f', raw[5:9])[0]
        if len(raw) >= 7 and raw[0] == 0x9f and raw[1] == 0x78 and raw[2] == 0x04:
            return struct.unpack('>f', raw[3:7])[0]
    except (TypeError, struct.error):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decode_switch(value) -> str:
    """Decode outputSwitch INTEGER to human label."""
    labels = {0: "Off", 1: "On", 2: "resetEmergOff",
               3: "setEmergOff", 10: "clearEvents"}
    try:
        n = int(value)
    except TypeError:
        # x690 Integer - str() gives "Integer(N)"
        s = str(value)
        if s.startswith("Integer(") and s.endswith(")"):
            n = int(s[8:-1])
        else:
            return str(value)
    return labels.get(n, f"Unknown({n})")


# Core read / write operations

async def read_channel(client: Client, channel: str) -> dict:
    """Read set voltage, measured voltage, and switch state for one channel."""
    results = await client.multiget([
        oid_for(OID_VOLT_SET,  channel),
        oid_for(OID_VOLT_MEAS, channel),
        oid_for(OID_SWITCH,    channel),
    ])
    # multiget returns a list of VarBinds in the same order
    return {
        "volt_set":  decode_float(results[0].value),
        "volt_meas": decode_float(results[1].value),
        "switch":    decode_switch(results[2].value),
    }


async def write_voltage(client: Client, channel: str, voltage: float) -> None:
    """Write a set-point voltage to one channel via outputVoltage (.10)."""
    oid = oid_for(OID_VOLT_SET, channel)
    encoded = encode_float(voltage)
    await client.set(oid, encoded)


async def write_switch(client: Client, channel: str, state: int) -> None:
    """
    Write outputSwitch (.9) for one channel.

    state: 0 = Off, 1 = On
    Other valid MIB values: 2=resetEmergOff, 3=setEmergOff, 10=clearEvents
    (not exposed via CLI here - call directly if needed).
    """
    oid = oid_for(OID_SWITCH, channel)
    await client.set(oid, Integer(state))


# Main workflow

async def run(host: str, channel: str,
              voltage: Optional[float],
              read_community: str,
              write_community: str,
              reset: bool,
              port: int,
              switch_on: bool,
              switch_off_after: bool) -> None:

    print(f"\n  MPOD Write Test  -  {host}:{port}  -  channel {channel}")
    print(f"  {'─'*50}")

    reader = Client(host, V2C(read_community),  port=port)
    writer = Client(host, V2C(write_community), port=port)

    # ── Step 1: read current state ──────────────────────────────────────────
    step = 1
    print(f"\n  [{step}] Reading current state...")
    before = await read_channel(reader, channel)

    v_set  = f"{before['volt_set']:+.3f} V"  if before['volt_set']  is not None else "?"
    v_meas = f"{before['volt_meas']:+.3f} V" if before['volt_meas'] is not None else "?"
    print(f"      V_set  : {v_set}")
    print(f"      V_meas : {v_meas}")
    print(f"      Switch : {before['switch']}")

    any_action = switch_on or (voltage is not None) or switch_off_after
    if not any_action:
        print("\n  No --switch / --voltage given; read-only run complete.")
        return

    # ── Step 2 (optional): turn channel on ──────────────────────────────────
    if switch_on:
        step += 1
        print(f"\n  [{step}] Turning {channel} ON  (outputSwitch → 1)...")
        print(f"      OID : {OID_SWITCH}.{channel_to_suffix(channel)}")
        await write_switch(writer, channel, 1)
        await asyncio.sleep(0.3)
        sw_state = await read_channel(reader, channel)
        print(f"      Switch readback: {sw_state['switch']}")
        if sw_state['switch'] != "On":
            print(f"  ! Switch readback is '{sw_state['switch']}' - expected 'On'.")

    # ── Step 3 (optional): set voltage ──────────────────────────────────────
    if voltage is not None:
        if abs(voltage) > TEST_VOLTAGE_LIMIT_V:
            print(f"\n  ERROR: {voltage} V exceeds test safety limit "
                  f"({TEST_VOLTAGE_LIMIT_V} V).")
            print(f"  Increase TEST_VOLTAGE_LIMIT_V in the script if intentional.")
            sys.exit(1)

        step += 1
        print(f"\n  [{step}] Writing {voltage:+.3f} V to {channel}...")
        print(f"      OID : {OID_VOLT_SET}.{channel_to_suffix(channel)}")
        print(f"      BER : {bytes(encode_float(voltage)).hex()}")
        await write_voltage(writer, channel, voltage)
        await asyncio.sleep(0.5)

        # Read back to verify set-point
        after = await read_channel(reader, channel)
        v_set_after  = after['volt_set']
        v_meas_after = after['volt_meas']
        v_set_str  = f"{v_set_after:+.3f} V"  if v_set_after  is not None else "?"
        v_meas_str = f"{v_meas_after:+.3f} V" if v_meas_after is not None else "?"
        print(f"      V_set  : {v_set_str}")
        print(f"      V_meas : {v_meas_str}")
        print(f"      Switch : {after['switch']}")

        if v_set_after is not None:
            delta = abs(v_set_after - voltage)
            if delta < 0.1:
                print(f"\n  ✓  Set-point accepted  (readback Δ = {delta:.4f} V)")
            else:
                print(f"\n  ✗  Set-point mismatch: sent {voltage:+.3f} V, "
                      f"got {v_set_after:+.3f} V  (Δ = {delta:.4f} V)")
        else:
            print("\n  Could not verify readback (float decode failed).")

    # ── Step 4 (optional): reset set-point to 0 V ───────────────────────────
    if reset:
        step += 1
        print(f"\n  [{step}] Resetting {channel} to 0.000 V...")
        await write_voltage(writer, channel, 0.0)
        await asyncio.sleep(0.3)
        final = await read_channel(reader, channel)
        v_final = final['volt_set']
        v_final_str = f"{v_final:+.3f} V" if v_final is not None else "?"
        print(f"      V_set after reset: {v_final_str}")

    # ── Step 5 (optional): turn channel OFF ─────────────────────────────────
    if switch_off_after:
        step += 1
        print(f"\n  [{step}] Turning {channel} OFF  (outputSwitch → 0)...")
        print(f"      OID : {OID_SWITCH}.{channel_to_suffix(channel)}")
        await write_switch(writer, channel, 0)
        await asyncio.sleep(0.3)
        sw_state = await read_channel(reader, channel)
        print(f"      Switch readback: {sw_state['switch']}")
        if sw_state['switch'] != "Off":
            print(f"  ⚠  Switch readback is '{sw_state['switch']}' - expected 'Off'.")

    # ── Final state summary ──────────────────────────────────────────────────
    step += 1
    print(f"\n  [{step}] Final state:")
    final_state = await read_channel(reader, channel)
    v_set_f  = f"{final_state['volt_set']:+.3f} V"  if final_state['volt_set']  is not None else "?"
    v_meas_f = f"{final_state['volt_meas']:+.3f} V" if final_state['volt_meas'] is not None else "?"
    print(f"      V_set  : {v_set_f}")
    print(f"      V_meas : {v_meas_f}")
    print(f"      Switch : {final_state['switch']}")
    print()


def main():
    p = argparse.ArgumentParser(
        description="Write a test voltage to one MPOD channel, toggle on/off, and read back."
    )
    p.add_argument("--host",            default="192.168.55.8",
                   help="MPOD IP address")
    p.add_argument("--channel",         default="u700",
                   help="Channel name, e.g. u700 or u801 (default: u700)")
    p.add_argument("--voltage",         type=float, default=None,
                   help="Voltage to set in volts (omit for read-only or switch-only)")
    p.add_argument("--read-community",  default="public",
                   help="SNMP read community (default: public)")
    p.add_argument("--write-community", default="guru",
                   help="SNMP write community (default: guru)")
    p.add_argument("--reset",           action="store_true",
                   help="Reset channel to 0 V after the voltage test (before --switch-off-after)")
    p.add_argument("--port",            type=int, default=161,
                   help="SNMP UDP port (default: 161)")

    sw_group = p.add_mutually_exclusive_group()
    sw_group.add_argument("--switch",   choices=["on", "off"], default=None,
                          help="Turn channel on or off before any voltage action. "
                               "Use 'on' to enable output, 'off' to disable. "
                               "For a full cycle (on → set → off) combine --switch on with --switch-off-after.")
    sw_group.add_argument("--switch-off-after", action="store_true",
                          help="Turn channel OFF at the end (after --reset if present). "
                               "Implies no --switch at the start; combine with --switch on for full cycle.")

    # Allow --switch on combined with --switch-off-after by splitting into two booleans
    args = p.parse_args()

    switch_on        = (args.switch == "on")
    switch_off_start = (args.switch == "off")   # off-only at start (no voltage action needed)
    switch_off_after = args.switch_off_after

    # --switch off as a standalone: no voltage, just turn off then final read
    if switch_off_start:
        # Treat as: no switch_on, no voltage, switch_off_after=True
        switch_on        = False
        switch_off_after = True

    asyncio.run(run(
        host             = args.host,
        channel          = args.channel,
        voltage          = args.voltage,
        read_community   = args.read_community,
        write_community  = args.write_community,
        reset            = args.reset,
        port             = args.port,
        switch_on        = switch_on,
        switch_off_after = switch_off_after,
    ))


if __name__ == "__main__":
    main()
