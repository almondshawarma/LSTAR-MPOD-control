"""
lstar_mpod_ctl.py  -  LSTAR MPOD unified control CLI
======================================================
Single entry-point CLI for all MPOD operations at LSTAR.

Subcommands
-----------
  probe                        read & display all MPOD channels
  set      CHANNEL VOLTAGE     write a voltage to one channel (+ readback)
  multipole ELEMENT [flags]    compute multipole voltages and push to MPOD
  zero     ELEMENT             set all mapped channels for an element to 0 V
  readback ELEMENT             read back current state of mapped channels
  show-map                     display electrode→channel wiring map

Usage examples
--------------
  # Always safe to probe first, read-only
  python3.11 lstar_mpod_ctl.py probe

  # Dry-run a multipole config (prints table, writes nothing)
  python3.11 lstar_mpod_ctl.py multipole M --Q 100 --H -300 --O 300 --dry-run

  # Push M-element voltages to MPOD
  python3.11 lstar_mpod_ctl.py multipole M --Q 100 --H -300 --O 300 --De 100 --Do 100

  # (Q+oct)1, note: shell-quote element names containing parentheses
  python3.11 lstar_mpod_ctl.py multipole "(Q+oct)1" --Q -1600 --O -200

  # Set one channel manually and verify
  python3.11 lstar_mpod_ctl.py set u700 10.0

  # Zero all mapped M-element channels
  python3.11 lstar_mpod_ctl.py zero M

  # Read back mapped M-element channels
  python3.11 lstar_mpod_ctl.py readback M

  # Show electrode→channel wiring
  python3.11 lstar_mpod_ctl.py show-map

Global options (apply to every subcommand)
------------------------------------------
  --host HOST           MPOD IP address        (default: 192.168.55.8)
  --read-community STR  SNMP read community    (default: public)
  --write-community STR SNMP write community   (default: guru)
  --port N              SNMP UDP port          (default: 161)
  --map FILE            JSON channel map file  (default: built-in DEFAULT_CHANNEL_MAP)

Channel map (DEFAULT_CHANNEL_MAP)
----------------------------------
  Edit the dict near the top of this file to wire electrode indices to MPOD
  channel names, or supply a JSON file via --map.

  JSON format example (channel_map.json):
    {
      "M":        {"0": "u700", "1": "u701", ..., "23": "u???"},
      "(Q+oct)1": {"0": "u???", ...},
      "(Q+oct)2": {}
    }

Safety notes
------------
  - Voltages exceeding per-element LSTAR Table 5 limits trigger an error.
    Use --force to override (with a warning).
  - Any per-electrode voltage exceeding HARD_LIMIT_V (500 V default) is
    always rejected unless --force is also passed.
  - --dry-run prints the full voltage table without touching hardware.
  - Zero/multipole/set do NOT switch channels on or off, the MPOD channel
    must already be On for the output to track the new set-point.

Install
-------
  pip install puresnmp numpy
"""

import argparse
import asyncio
import json
import struct
import sys
from typing import Optional

import numpy as np

try:
    from puresnmp import V2C, Client
    from puresnmp.types import Opaque
    from x690.types import ObjectIdentifier, Integer
    import puresnmp.exc as _snmp_exc   # CommitFailed, NoSuchName, etc.
except ImportError:
    sys.exit("ERROR: puresnmp not installed.  Run:  pip install puresnmp")


# ══════════════════════════════════════════════════════════════════════════════
#  §1  Constants & SNMP OID table
# ══════════════════════════════════════════════════════════════════════════════

# WIENER-CRATE-MIB.txt outputEntry base  (rev 7280, 2025-12-10)
_ENTRY = "1.3.6.1.4.1.19947.1.3.2.1"

# String OID bases used by oid_for() to build per-channel OIDs
OID_NAME      = f"{_ENTRY}.2"    # outputName                DisplayString
OID_STATUS    = f"{_ENTRY}.4"    # outputStatus              BITS
OID_VOLT_MEAS = f"{_ENTRY}.5"    # outputMeasurementSenseV   Float [V]
OID_CURR_MEAS = f"{_ENTRY}.7"    # outputMeasurementCurrent  Float [A]
OID_SWITCH    = f"{_ENTRY}.9"    # outputSwitch              INTEGER  (0=Off, 1=On)
OID_VOLT_SET  = f"{_ENTRY}.10"   # outputVoltage             Float [V]  set-point
OID_VOLT_MAX  = f"{_ENTRY}.11"   # outputSupervisionMaxTermV Float [V]  hardware ceiling
OID_CURR_SET  = f"{_ENTRY}.12"   # outputCurrent             Float [A]  current limit
OID_VOLT_MIN  = f"{_ENTRY}.13"   # outputSupervisionMinTermV Float [V]  lower bound
                                  # If MinTermV = 0, the module is unipolar and negative
                                  # voltages will fail with wrongValue (SNMP error 10).

# ObjectIdentifier versions for SNMP subtree walk (discovery)
_WALK = {k: ObjectIdentifier(v) for k, v in {
    "name":      OID_NAME,
    "status":    OID_STATUS,
    "volt_meas": OID_VOLT_MEAS,
    "curr_meas": OID_CURR_MEAS,
    "switch":    OID_SWITCH,
    "volt_set":  OID_VOLT_SET,
    "curr_set":  OID_CURR_SET,
}.items()}

SWITCH_LABELS = {
    0: "Off", 1: "On", 2: "resetEmergOff", 3: "setEmergOff", 10: "clearEvents",
}
SWITCH_ON  = 1
SWITCH_OFF = 0

STATUS_BITS = {
    0: "On",             1: "Inhibit",        2:  "FailMinSenseV",
    3: "FailMaxSenseV",  4: "FailMaxTermV",   5:  "FailMaxCurrent",
    6: "FailMaxTemp",    7: "FailMaxPower",   8:  "FailCacheUpdate",
    9: "FailTimeout",   10: "CurrentLimited", 11: "RampUp",
   12: "RampDown",      13: "EnableKill",     14: "EmergencyOff",
   15: "Adjusting",     16: "ConstantVoltage",
}

# Absolute per-channel voltage ceiling, always enforced
HARD_LIMIT_V = 3000.0

# Throttle between successive SNMP SETs (seconds), preventing crate overload
_WRITE_PAUSE = 0.05


# ══════════════════════════════════════════════════════════════════════════════
#  §2  Multipole physics
#      Ref: LSTAR spec (Melconian et al., v03.10.2023)
# ══════════════════════════════════════════════════════════════════════════════

MULTIPOLE_ORDERS: dict[str, int] = {
    'Q': 2,    # Quadrupole
    'H': 3,    # Hexapole (sextupole)
    'O': 4,    # Octupole
    'De': 5,   # Decapole
    'Do': 6,   # Dodecapole
}

# ─── LSTAR_ELEMENTS ──────────────────────────────────────────────────────────
# Elements are listed in beam-path order: Object/Start → Image/Focal plane.
# B1 and B2 are magnetic dipoles on a separate current supply (not here).
#
# Each entry:
#   n_rods        : number of independently-wired electrodes
#   components    : list of multipole orders this element supports
#   max_amplitude : LSTAR Table 5 spec limit for each component [V]
#   hard_limit    : absolute per-electrode voltage ceiling [V]
#                   This is a final backstop; check_amplitudes uses max_amplitude.
#                   For pure single-component elements, hard_limit ≥ max_amplitude.
#                   For squirrel-cage elements with multiple simultaneous components,
#                   hard_limit can be set to the realistic per-electrode maximum.
#   description   : human-readable label
#
# Voltage computation for all elements uses the same triangular_basis():
#   V_k = Σ_n  A_n · f(k, n, N)
# For pure elements this reduces to the classic alternating pattern:
#   4-rod quad:  V_k = A_Q · [+1, -1, +1, -1]
#   6-rod hex:   V_k = A_H · [+1, -1, +1, -1, +1, -1]
#
# Bussed wiring note:
#   If two physical rods share a feedthrough channel, list both in the channel
#   map pointing to the same MPOD channel name.  The push loop will write the
#   same value twice. This is idempotent and harmless.  Do NOT apply different
#   per-electrode corrections to physically-bussed rods.

LSTAR_ELEMENTS: dict[str, dict] = {

    # ── Q1 ─────────────────────────────────────────────────────────────────
    # Pure electrostatic quadrupole.  4 rods at 0°/90°/180°/270°.
    # Spec: Table 5, aperture 30 mm, EFL 200 mm.
    'Q1': {
        'n_rods':        4,
        'components':    ['Q'],
        'max_amplitude': {'Q': 2000},   # ±2.0 kV (Table 5)
        'hard_limit':    2200,          # 10 % above spec max as final backstop
        'description':   '4-rod pure quadrupole Q1 (30 mm aperture, 200 mm EFL)',
    },

    # ── Q2 ─────────────────────────────────────────────────────────────────
    # Pure electrostatic quadrupole.  4 rods.
    # Spec: Table 5, aperture 50 mm, EFL 200 mm.
    'Q2': {
        'n_rods':        4,
        'components':    ['Q'],
        'max_amplitude': {'Q': 2000},   # ±2.0 kV (Table 5)
        'hard_limit':    2200,
        'description':   '4-rod pure quadrupole Q2 (50 mm aperture, 200 mm EFL)',
    },

    # ── (Q+oct)1 ────────────────────────────────────────────────────────────
    # Squirrel-cage quad+octupole.  24 rods.
    # Spec: Table 5, aperture 60 mm, EFL 240 mm.
    '(Q+oct)1': {
        'n_rods':        24,
        'components':    ['Q', 'O'],
        'max_amplitude': {'Q': 1600, 'O': 200},   # Table 5
        'hard_limit':    1800,   # max per-electrode = Q_max + O_max = 1800 V
        'description':   '24-rod quad+octupole #1 (60 mm aperture, 240 mm EFL)',
    },

    # ── S1 ──────────────────────────────────────────────────────────────────
    # Pure electrostatic hexapole (sextupole).  6 rods at 0°/60°/…/300°.
    # Spec: Table 5, aperture 50 mm, EFL 120 mm.
    'S1': {
        'n_rods':        6,
        'components':    ['H'],
        'max_amplitude': {'H': 400},   # ±0.4 kV (Table 5)
        'hard_limit':    500,
        'description':   '6-rod pure hexapole S1 (50 mm aperture, 120 mm EFL)',
    },

    # ── M ───────────────────────────────────────────────────────────────────
    # Squirrel-cage central multipole.  24 rods.
    # Spec: Table 5, aperture 160 mm, EFL 300 mm.
    'M': {
        'n_rods':        24,
        'components':    ['Q', 'H', 'O', 'De', 'Do'],
        'max_amplitude': {'Q': 100, 'H': 300, 'O': 300, 'De': 100, 'Do': 100},
        'hard_limit':    500,   # all components at max simultaneously ≈ 900 V;
                                # 500 V is conservative, lower if needed
        'description':   '24-rod central multipole M (160 mm aperture, 300 mm EFL)',
    },

    # ── (Q+oct)2 ────────────────────────────────────────────────────────────
    '(Q+oct)2': {
        'n_rods':        24,
        'components':    ['Q', 'O'],
        'max_amplitude': {'Q': 1600, 'O': 200},
        'hard_limit':    1800,
        'description':   '24-rod quad+octupole #2 (60 mm aperture, 240 mm EFL)',
    },

    # ── S2 ──────────────────────────────────────────────────────────────────
    'S2': {
        'n_rods':        6,
        'components':    ['H'],
        'max_amplitude': {'H': 400},
        'hard_limit':    500,
        'description':   '6-rod pure hexapole S2 (50 mm aperture, 120 mm EFL)',
    },

    # ── Q3 ─────────────────────────────────────────────────────────────────
    'Q3': {
        'n_rods':        4,
        'components':    ['Q'],
        'max_amplitude': {'Q': 2000},
        'hard_limit':    2200,
        'description':   '4-rod pure quadrupole Q3 (50 mm aperture, 200 mm EFL)',
    },

    # ── Q4 ─────────────────────────────────────────────────────────────────
    'Q4': {
        'n_rods':        4,
        'components':    ['Q'],
        'max_amplitude': {'Q': 2000},
        'hard_limit':    2200,
        'description':   '4-rod pure quadrupole Q4 (30 mm aperture, 200 mm EFL)',
    },
}


def triangular_basis(k: int, n: int, N: int) -> float:
    """
    Normalized triangular-wave coefficient for rod k, multipole order n,
    in a cage of N rods.  Range: [−1, +1].

    Used for both squirrel-cage elements (M, Q+oct) and traditional
    pure-order elements (Q1–Q4, S1–S2).  For a pure quadrupole (n=2, N=4)
    this produces exactly [+1, -1, +1, -1], i.e. the standard alternating
    electrode pattern.  For a pure hexapole (n=3, N=6) it gives
    [+1, -1, +1, -1, +1, -1].

    Verified against the LSTAR spreadsheet.
    """
    t = (n * k / N) % 1.0
    return 2.0 * abs(2.0 * t - 1.0) - 1.0


def compute_voltages(element: str,
                     amplitudes: dict[str, float]) -> np.ndarray:
    """
    Compute per-electrode voltages for any LSTAR electrostatic element.

    Superposition: V_k = Σ_n  A_n · f(k, n, N)

    Works for all element types:
      - Squirrel-cage multipoles (M, (Q+oct)1, (Q+oct)2): multi-component
      - Pure quadrupoles (Q1–Q4):  single component Q, N=4
      - Pure hexapoles  (S1–S2):   single component H, N=6

    Parameters
    ----------
    element    : key in LSTAR_ELEMENTS, e.g. 'M', 'Q1', 'S2'
    amplitudes : {component: amplitude_V}, e.g. {'Q': -1500}
                 Omitted components default to zero.

    Returns
    -------
    np.ndarray of shape (n_rods,), units volts
    """
    info  = LSTAR_ELEMENTS[element]
    N     = info['n_rods']
    volts = np.zeros(N)

    for comp, amp in amplitudes.items():
        if comp not in MULTIPOLE_ORDERS:
            raise ValueError(
                f"Unknown component '{comp}'.  Valid: {list(MULTIPOLE_ORDERS)}")
        if comp not in info['components']:
            raise ValueError(
                f"'{comp}' not valid for element '{element}'.  "
                f"Available: {info['components']}")
        n = MULTIPOLE_ORDERS[comp]
        volts += amp * np.array([triangular_basis(k, n, N) for k in range(N)])

    return volts


def check_amplitudes(element: str, amplitudes: dict[str, float],
                     force: bool = False) -> bool:
    """
    Validate amplitudes against LSTAR spec limits.
    Returns True if all within spec, prints errors and returns False otherwise.
    Warnings are printed instead when force=True.
    """
    limits = LSTAR_ELEMENTS[element]['max_amplitude']
    ok = True
    for comp, amp in amplitudes.items():
        lim = limits.get(comp)
        if lim and abs(amp) > lim:
            tag = "WARNING (--force bypassed)" if force else "ERROR"
            print(f"  {tag}: [{element}] '{comp}' = {amp:+.1f} V "
                  f"exceeds spec limit ±{lim} V")
            if not force:
                ok = False
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  §3  Channel map
#      Maps {element_name: {electrode_index: channel_entry}}
#      ─────────────────────────────────────────────────────────
#      EDIT THIS SECTION as hardware gets wired up.
#      Empty dicts  = element not yet connected to MPOD.
#
#      Channel entry format
#      ────────────────────
#      Each entry can be either:
#        'uXXX'          shorthand for ('uXXX', +1), electrode wired to U+
#        ('uXXX', +1)    electrode wired to U+ output terminal of the supply
#        ('uXXX', -1)    electrode wired to U− output terminal of the supply
#
#      The polarity factor controls how the software converts the computed
#      electrode voltage into a supply set-point:
#        v_set = v_electrode × polarity_factor
#
#      For BIPOLAR iseg HV modules: always use +1.
#        The module natively accepts any signed outputVoltage set-point.
#      For UNIPOLAR 0MPV LV modules: v_set must be ≥ 0.
#        Use +1 for electrodes needing positive voltage (wired to U+).
#        Use -1 for electrodes needing negative voltage (wired to U−).
#
#      Rod numbering convention
#      ────────────────────────
#      Rod 0 is the first rod encountered going counter-clockwise from 12 o'clock
#      (as viewed from the beam-entry face), matching given drawings.
#
#      Bussed wiring
#      ─────────────
#      If two physical rods share one MPOD output, map both indices to the same
#      channel entry.  The push loop writes the same voltage twice (idempotent).
#      Do NOT apply different per-electrode corrections to bussed rods.
#      Example for a bussed 4-rod quad (2 outputs instead of 4):
#        'Q1': {0: ('uXXX', +1), 1: ('uYYY', -1),
#               2: ('uXXX', +1), 3: ('uYYY', -1)}
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CHANNEL_MAP: dict[str, dict[int, object]] = {

    # Polarity factors below are ALL +1 as a placeholder, update once
    # you know which terminal (U+ or U−) each rod is physically wired to.
    # For bipolar iseg HV modules (final setup): leave all at +1.
    # For unipolar 0MPV LV modules: set -1 for rods wired to U−.

    # ── Q1  (4 rods: rod 0 = +V, rod 1 = −V, rod 2 = +V, rod 3 = −V) ──────
    'Q1': {
        # TODO: fill as {0: ('uXXX', +1), 1: ('uXXX', -1), 2: ('uXXX', +1), 3: ('uXXX', -1)}
        #       or bussed: {0: ('uXXX', +1), 1: ('uYYY', -1), 2: ('uXXX', +1), 3: ('uYYY', -1)}
    },

    # ── Q2  (4 rods, same polarity pattern as Q1) ────────────────────────────
    'Q2': {
        # TODO: fill as {0: ('uXXX', +1), 1: ('uXXX', -1), 2: ('uXXX', +1), 3: ('uXXX', -1)}
    },

    # ── S1  (6 rods: +V, −V, +V, −V, +V, −V) ───────────────────────────────
    'S1': {
        # TODO: fill as {0: ('uXXX', +1), 1: ('uXXX', -1), 2: ('uXXX', +1),
        #                3: ('uXXX', -1), 4: ('uXXX', +1), 5: ('uXXX', -1)}
    },

    # ── (Q+oct)1  (24-rod squirrel-cage) ────────────────────────────────────
    '(Q+oct)1': {
        # TODO: add entries when hardware is installed
        # Suggest using +1 for all rods if bipolar HV modules are used,
        # or set per-rod polarity based on expected Q-dominant wiring otherwise.
    },

    # ── M  (24-rod squirrel-cage) ───────────────────────────────
    'M': {
        # ── Slot 5  (module s5, channels c00–c07) ── electrodes 0–7
        0: ('u500', +1),  1: ('u501', +1),  2: ('u502', -1),  3: ('u503', -1),
        4: ('u504', -1),  5: ('u505', -1),  6: ('u506', +1),  7: ('u507', +1),
        # ── Slot 6  (module s6, channels c00–c07) ── electrodes 8–15
        8:  ('u600', +1),  9: ('u601', -1), 10: ('u602', -1), 11: ('u603', +1),
        12: ('u604', +1), 13: ('u605', +1), 14: ('u606', -1), 15: ('u607', -1),
        # ── Slot 7  (module s7, channels c00–c07) ── electrodes 16–23
        16: ('u700', +1), 17: ('u701', +1), 18: ('u702', +1), 19: ('u703', -1),
        20: ('u704', -1), 21: ('u705', -1), 22: ('u706', -1), 23: ('u707', +1),
    },

    # ── (Q+oct)2  (24-rod squirrel-cage) ────────────────────────────────────
    '(Q+oct)2': {
        # TODO: add entries when hardware is installed
    },

    # ── S2  (6 rods, same polarity pattern as S1) ───────────────────────────
    'S2': {
        # TODO: fill as {0: ('uXXX', +1), ..., 5: ('uXXX', -1)}
    },

    # ── Q3  (4 rods, same polarity pattern as Q1/Q2) ────────────────────────
    'Q3': {
        # TODO: fill as {0: ('uXXX', +1), 1: ('uXXX', -1), 2: ('uXXX', +1), 3: ('uXXX', -1)}
    },

    # ── Q4  (4 rods, same polarity pattern as Q1/Q2) ────────────────────────
    'Q4': {
        # TODO: fill as {0: ('uXXX', +1), 1: ('uXXX', -1), 2: ('uXXX', +1), 3: ('uXXX', -1)}
    },
}


def load_channel_map(path: Optional[str]) -> dict[str, dict[int, str]]:
    """Return the channel map from a JSON file or the built-in default."""
    if path is None:
        return DEFAULT_CHANNEL_MAP
    with open(path) as fh:
        raw = json.load(fh)
    # JSON keys are always strings, convert electrode keys to int
    return {elem: {int(k): ch for k, ch in cmap.items()}
            for elem, cmap in raw.items()}


# ══════════════════════════════════════════════════════════════════════════════
#  §4  SNMP helpers
# ══════════════════════════════════════════════════════════════════════════════

def channel_to_suffix(channel: str) -> int:
    """
    Convert WIENER channel name to SNMP OID suffix.
    MIB rule: u<N>  →  OID suffix N+1  (SMI table indices start at 1).
    Example:  'u700' → 701,  'u800' → 801
    """
    try:
        return int(channel.lower().lstrip('u')) + 1
    except ValueError:
        raise ValueError(
            f"Invalid channel name '{channel}'.  Expected format: u<N>  e.g. u700")


def oid_for(base_oid_str: str, channel: str) -> ObjectIdentifier:
    """Build a per-channel ObjectIdentifier from a string OID base and channel name."""
    return ObjectIdentifier(f"{base_oid_str}.{channel_to_suffix(channel)}")


def encode_float(value: float) -> Opaque:
    """
    Encode a Python float as WIENER Opaque Float for SNMP SET.
    Inner encoding: 9f 78 04 <4-byte big-endian IEEE 754>
    puresnmp.types.Opaque adds the outer BER tag (44 07) automatically.
    """
    return Opaque(bytes([0x9f, 0x78, 0x04]) + struct.pack('>f', value))


def encode_int(value: int) -> Integer:
    """
    Encode a Python int as x690 Integer for SNMP SET.
    Used for outputSwitch (OID .9): 0=Off, 1=On, 2=resetEmergOff, 3=setEmergOff.
    """
    return Integer(value)


def resolve_ch(entry) -> tuple[str, int]:
    """
    Parse a channel-map entry into (channel_name, polarity_factor).

    Accepted formats
    ----------------
    'u500'         →  ('u500', +1)   string shorthand: electrode wired to U+
    ('u500', +1)   →  ('u500', +1)   explicit U+ wiring
    ('u500', -1)   →  ('u500', -1)   explicit U− wiring

    Polarity factor
    ---------------
    +1  Electrode physically wired to the U+ output terminal of the supply.
        The code computes:   v_set = v_electrode × (+1) = v_electrode.
        Bipolar iseg HV:     v_set can be any sign — the module handles it.
        Unipolar 0MPV LV:    v_electrode must be ≥ 0; negative → wrongValue (code 10).

    -1  Electrode physically wired to the U− output terminal of the supply.
        The code computes:   v_set = v_electrode × (−1) = −v_electrode ≥ 0.
        The supply outputs v_set between its terminals.
        The electrode sits at −v_set relative to the return line.
        Unipolar 0MPV LV:    v_electrode must be ≤ 0 so that v_set ≥ 0.

    Usage notes
    -----------
    • For bipolar iseg HV modules (final LSTAR setup), always use +1.
      The supply natively accepts signed voltages via outputVoltage.
    • For unipolar 0MPV LV test modules, set +1 / -1 according to the
      physical wiring so the code sends only non-negative set-points.
    • For squirrel-cage elements (M, Q+oct), the sign of each electrode's
      voltage depends on the amplitude combination.  Wire based on the
      expected dominant configuration and the code will warn if a combination
      would flip an electrode's sign beyond what the wiring supports.
    """
    if isinstance(entry, str):
        return entry, +1
    try:
        ch, pol = entry
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Channel-map entry must be a str or (str, ±1) tuple; got {entry!r}"
        ) from exc
    if pol not in (+1, -1):
        raise ValueError(f"Polarity factor must be +1 or -1; got {pol!r}")
    return ch, pol


def _check_voltage_signs(
    voltages: "np.ndarray",
    ch_map:   dict,
) -> list[tuple]:
    """
    Return a list of (k, ch, polarity, v_electrode, v_set) for every mapped
    electrode where v_set = v_electrode × polarity would be negative.

    A negative v_set means the wiring configuration in the channel map cannot
    produce the required electrode voltage on a UNIPOLAR supply.
    On bipolar iseg HV modules a negative v_set is valid. These entries are
    informational only until you know which module type is installed.
    """
    issues: list[tuple] = []
    for k, v in enumerate(voltages):
        raw = ch_map.get(k)
        if raw is None:
            continue
        ch, pol = resolve_ch(raw)
        v_set = float(v) * pol
        if v_set < -1e-6:
            issues.append((k, ch, pol, float(v), v_set))
    return issues


def _print_sign_issues(issues: list[tuple]) -> None:
    """Print a formatted warning block for each polarity mismatch."""
    W = 66
    print(f"\n  {'─'*W}")
    print(f"  ⚠   POLARITY MISMATCH  —  {len(issues)} electrode(s)")
    print(f"  {'─'*W}")
    for k, ch, pol, v_el, v_set in issues:
        terminal = "U+" if pol == +1 else "U\u2212"
        print(f"    electrode {k:>2d}  channel {ch}  [wired to {terminal}]")
        print(f"      electrode target : {v_el:+.4f} V")
        print(f"      supply set-point : {v_set:+.4f} V  \u2190 NEGATIVE")
    print(f"\n  On UNIPOLAR supplies (0MPV 8xxx) this will return wrongValue (code 10).")
    print(f"  On BIPOLAR iseg HV modules this is fine. They accept signed voltages.")
    print(f"  To fix for unipolar: flip polarity factor in channel map (+1 \u2194 -1)")
    print(f"  or re-wire that electrode to the opposite supply terminal.")
    print(f"  Use --force to push anyway (useful when testing with bipolar HV).")
    print(f"  {'─'*W}\n")


def decode_float(raw) -> Optional[float]:
    """
    Decode WIENER Opaque Float from SNMP GET response.
    Handles both full BER wrapper (44 07 9f 78 04 …) and inner-only form.
    """
    try:
        b = bytes(raw)
        if len(b) >= 9 and b[0] == 0x44 and b[2] == 0x9f and b[3] == 0x78:
            return struct.unpack('>f', b[5:9])[0]
        if len(b) >= 7 and b[0] == 0x9f and b[1] == 0x78 and b[2] == 0x04:
            return struct.unpack('>f', b[3:7])[0]
    except (TypeError, struct.error):
        pass
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def decode_int(raw) -> Optional[int]:
    """Convert x690 Integer, plain int, or 'Integer(N)' string to Python int."""
    if raw is None:
        return None
    try:
        return int(raw)
    except TypeError:
        s = str(raw)
        if s.startswith("Integer(") and s.endswith(")"):
            return int(s[8:-1])
        try:
            return int(s)
        except ValueError:
            return None


def decode_str(raw) -> str:
    """Decode an SNMP DisplayString / OctetString to plain ASCII text."""
    try:
        return bytes(raw).decode("ascii", errors="replace").strip("\x00")
    except TypeError:
        s = str(raw)
        if "OctetString" in s:
            try:
                return s.split("b'")[1].rstrip("')")
            except IndexError:
                pass
        return s.strip()


def decode_status(raw) -> str:
    """Decode outputStatus BITS (SMIv2 BITS encoded as OCTET STRING)."""
    try:
        b = bytes(raw)
    except TypeError:
        return str(raw)
    flags = [
        name for bit, name in STATUS_BITS.items()
        if (bit // 8) < len(b) and (b[bit // 8] >> (7 - bit % 8)) & 1
    ]
    return ", ".join(flags) if flags else "Idle"


def suffix_to_label(idx: int) -> tuple[str, int, int]:
    """OID suffix → ('u<N>', slot, channel_in_slot)."""
    n = idx - 1
    return f"u{n}", n // 100, n % 100


async def walk_oid(client: Client, oid: ObjectIdentifier) -> dict[int, object]:
    """
    Walk one SNMP OID subtree.
    Returns {oid_suffix_int: raw_value} for every varbind returned.
    """
    out = {}
    try:
        async for vb in client.walk(oid):
            suffix = int(str(vb.oid).rsplit(".", 1)[-1])
            out[suffix] = vb.value
    except Exception as exc:
        print(f"  [WARN] walk({oid}): {exc}")
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  §4b  Write-error helper & pre-flight
# ══════════════════════════════════════════════════════════════════════════════

# SNMP SET error-status codes (RFC 1905) that appear in puresnmp exceptions
_SNMP_STATUS = {
    1:  "tooBig",            2:  "noSuchName",      3:  "badValue",
    4:  "readOnly",          5:  "genErr",           6:  "noAccess",
    7:  "wrongType",         8:  "wrongLength",      9:  "wrongEncoding",
    10: "wrongValue",        11: "noCreation",       12: "inconsistentValue",
    13: "resourceUnavailable", 14: "commitFailed",   15: "undoFailed",
    16: "authorizationError",  17: "notWritable",    18: "inconsistentName",
}

# Hints shown after a write failure keyed by status code
_STATUS_HINTS = {
    10: ("Value rejected by MPOD agent (wrongValue).\n"
         "       For NEGATIVE voltages this almost always means:\n"
         "         → outputSupervisionMinTerminalVoltage (.13) is set to 0 V,\n"
         "           so the module treats this channel as UNIPOLAR.\n"
         "         → Run:  python lstar_mpod_ctl.py limits {element}\n"
         "           to confirm.  If MinTermV = 0 you have two options:\n"
         "             a) Set MinTermV to a negative value in isegControl\n"
         "                (e.g. -500 V) to enable bipolar operation, or\n"
         "             b) Confirm the physical module actually supports\n"
         "                negative output (needs iseg EHS bipolar module).\n"
         "       For POSITIVE voltages: |voltage| exceeds MaxTermV (.11)\n"
         "         → run with --preflight"),
    14: ("The MPOD accepted the packet but refused the write.\n"
         "       Most likely causes (check in order):\n"
         "         1. Requested voltage exceeds the module hardware limit\n"
         "            → run with --preflight to see per-channel ceilings\n"
         "         2. Channel is in a fault state (FailMaxCurrent / Adjusting)\n"
         "            → cycle the fault via isegControl or MPOD front panel\n"
         "         3. Wrong write community (current: '{comm}')\n"
         "            → try --write-community guru"),
    16: "Authentication error - write community is wrong or access is denied.",
    17: "OID is not writable in the current device state.",
     4: "OID is flagged read-only by the agent.",
}


def _decode_snmp_status(exc: Exception) -> tuple[int | None, str]:
    """
    Extract SNMP status code and name from a puresnmp exception.
    Returns (status_int_or_None, human_label).
    """
    # puresnmp ≥ 5: exception carries .error_status
    code = getattr(exc, 'error_status', None)
    if code is None:
        # Fall back: parse 'status-code: N' from the string representation
        s = str(exc)
        if "status-code:" in s:
            try:
                code = int(s.split("status-code:")[1].split(")")[0].strip())
            except (ValueError, IndexError):
                pass
    label = _SNMP_STATUS.get(code, f"code {code}") if code is not None else str(exc)
    return code, label


def _report_write_error(k: int, ch: str, v: float,
                        exc: Exception, write_comm: str = "guru",
                        element: str = "ELEMENT") -> None:
    """Print a clear per-channel write failure message with a diagnostic hint."""
    code, label = _decode_snmp_status(exc)
    print(f"    electrode {k:2d}  ({ch})  →  {v:+9.4f} V  ✗  [{label}]")
    hint = _STATUS_HINTS.get(code, "")
    if hint:
        formatted = hint.format(comm=write_comm, element=element)
        print(f"       Hint: {formatted}")


async def preflight_limits(client: Client,
                           elem_map: dict[int, str]) -> dict[str, float | None]:
    """
    Read outputSupervisionMaxTerminalVoltage (.11) for every mapped channel.
    Returns {channel_name: max_v_or_None}.
    Channels where the OID is absent or unreadable return None.
    """
    limits: dict[str, float | None] = {}
    for ch in elem_map.values():
        try:
            r   = await client.multiget([oid_for(OID_VOLT_MAX, ch)])
            lim = decode_float(r[0].value)
            limits[ch] = lim
        except Exception:
            limits[ch] = None
    return limits




async def cmd_probe(args) -> None:
    """Read and display all MPOD channels (read-only)."""
    print(f"\n  WIENER MPOD Channel Probe")
    print(f"  {'─'*44}")
    print(f"  Host      : {args.host}:{args.port}")
    print(f"  Community : {args.read_community}")
    print()

    c = Client(args.host, V2C(args.read_community), port=args.port)

    print("  Walking outputName (.2) ...", end=" ", flush=True)
    names = await walk_oid(c, _WALK["name"])
    print(f"{len(names)} channel(s) found.")

    if not names:
        print("\n  No channels found.  Checklist:")
        print("    • Can you ping the MPOD?")
        print("    • Is the read community correct? (default: public)")
        print("    • Is UDP 161 unblocked in the host firewall?")
        print("    • Is the MPOD powered on with modules installed?")
        return

    print("  Fetching all fields ...", end=" ", flush=True)
    v_set  = await walk_oid(c, _WALK["volt_set"])
    v_meas = await walk_oid(c, _WALK["volt_meas"])
    c_set  = await walk_oid(c, _WALK["curr_set"])
    c_meas = await walk_oid(c, _WALK["curr_meas"])
    sw_all = await walk_oid(c, _WALK["switch"])
    st_all = await walk_oid(c, _WALK["status"])
    print("done.\n")

    cols = [16, 10, 12, 12, 12, 12, 14, 36]
    hdrs = ["Channel", "Name", "V_set (V)", "V_meas (V)",
            "I_lim (mA)", "I_meas (mA)", "Switch", "Status"]
    sep  = "  ".join("─" * w for w in cols)
    hdr  = "  ".join(h.ljust(w) for h, w in zip(hdrs, cols))

    print(f"  {sep}")
    print(f"  {hdr}")
    print(f"  {sep}")

    for idx in sorted(names):
        ch_name, slot, ch_no = suffix_to_label(idx)
        label = f"{ch_name} (s{slot}c{ch_no:02d})"
        name  = decode_str(names[idx])

        vs_f  = decode_float(v_set.get(idx))
        vm_f  = decode_float(v_meas.get(idx))
        cs_f  = decode_float(c_set.get(idx))
        cm_f  = decode_float(c_meas.get(idx))
        sw_i  = decode_int(sw_all.get(idx))
        st_r  = st_all.get(idx)

        row = [
            label, name,
            f"{vs_f:+.3f}"     if vs_f is not None else "?",
            f"{vm_f:+.3f}"     if vm_f is not None else "?",
            f"{cs_f*1e3:.4f}"  if cs_f is not None else "?",
            f"{cm_f*1e3:.4f}"  if cm_f is not None else "?",
            SWITCH_LABELS.get(sw_i, str(sw_i)) if sw_i is not None else "?",
            decode_status(st_r) if st_r is not None else "?",
        ]
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, cols)))

    print(f"  {sep}")

    on_count = sum(
        1 for idx in names
        if st_all.get(idx)
        and len(bytes(st_all[idx])) > 0
        and (bytes(st_all[idx])[0] & 0x80)   # bit 0 = outputOn = MSB of byte 0
    )
    print(f"\n  Total: {len(names)}   On: {on_count}   "
          f"Off: {len(names) - on_count}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  §6  Subcommand: set
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_set(args) -> None:
    """Write a voltage to one MPOD channel and read it back."""
    channel = args.channel
    voltage = args.voltage

    if abs(voltage) > HARD_LIMIT_V and not args.force:
        sys.exit(f"  ERROR: {voltage} V exceeds HARD_LIMIT_V "
                 f"({HARD_LIMIT_V} V).  Use --force to override.")

    print(f"\n  Set  {channel}  →  {voltage:+.3f} V")
    print(f"  {'─'*44}")

    rdr = Client(args.host, V2C(args.read_community),  port=args.port)
    wtr = Client(args.host, V2C(args.write_community), port=args.port)

    # ── Read current state ────────────────────────────────────────────────
    before = await rdr.multiget([
        oid_for(OID_VOLT_SET,  channel),
        oid_for(OID_VOLT_MEAS, channel),
        oid_for(OID_SWITCH,    channel),
    ])
    vs_b = decode_float(before[0].value)
    vm_b = decode_float(before[1].value)
    sw_b = SWITCH_LABELS.get(decode_int(before[2].value), "?")

    vs_b_s = f"{vs_b:+.3f} V" if vs_b is not None else "?"
    vm_b_s = f"{vm_b:+.3f} V" if vm_b is not None else "?"
    print(f"  Before  V_set={vs_b_s}   V_meas={vm_b_s}   Switch={sw_b}")

    if args.dry_run:
        print(f"  [DRY RUN] Would write {voltage:+.3f} V to {channel} "
              f"- nothing sent.\n")
        return

    # ── Write ─────────────────────────────────────────────────────────────
    try:
        await wtr.set(oid_for(OID_VOLT_SET, channel), encode_float(voltage))
    except Exception as exc:
        _report_write_error(0, channel, voltage, exc,
                            write_comm=args.write_community)
        print(f"  Set failed.  The channel state was not changed.\n")
        return
    await asyncio.sleep(0.5)    # let the MPOD process the set-point

    # ── Read back ─────────────────────────────────────────────────────────
    after = await rdr.multiget([
        oid_for(OID_VOLT_SET,  channel),
        oid_for(OID_VOLT_MEAS, channel),
    ])
    vs_a  = decode_float(after[0].value)
    vm_a  = decode_float(after[1].value)
    delta = abs(vs_a - voltage) if vs_a is not None else float('inf')
    mark  = "✓" if delta < 0.1 else "✗"

    vs_a_s = f"{vs_a:+.3f} V" if vs_a is not None else "?"
    vm_a_s = f"{vm_a:+.3f} V" if vm_a is not None else "?"
    print(f"  After   V_set={vs_a_s}   V_meas={vm_a_s}   "
          f"{mark} (set-point Δ = {delta:.4f} V)\n")


# ══════════════════════════════════════════════════════════════════════════════
#  §7  Subcommand: multipole
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_multipole(args) -> None:
    """Compute per-electrode voltages and push them to mapped MPOD channels."""
    element = args.element
    ch_map  = load_channel_map(args.map)

    # ── Collect amplitudes from CLI flags ─────────────────────────────────
    amplitudes: dict[str, float] = {
        comp: getattr(args, comp)
        for comp in ['Q', 'H', 'O', 'De', 'Do']
        if getattr(args, comp, None) is not None
    }
    if not amplitudes:
        sys.exit("  ERROR: No component amplitudes given.  "
                 "Use --Q, --H, --O, --De, and/or --Do.")

    # ── Validate against LSTAR spec limits ───────────────────────────────
    if not check_amplitudes(element, amplitudes, force=args.force):
        print("  (Pass --force to override spec limits at your own risk.)")
        sys.exit(1)

    # ── Compute electrode voltages ────────────────────────────────────────
    voltages  = compute_voltages(element, amplitudes)
    elem_map  = ch_map.get(element, {})
    n_rods    = len(voltages)

    # ── Print voltage table ───────────────────────────────────────────────
    W = 74
    info = LSTAR_ELEMENTS[element]
    print(f"\n{'─'*W}")
    print(f"  Element : {element}  -  {info['description']}")
    print(f"  Rods    : {n_rods}  |  Mapped channels: {len(elem_map)}"
          f"  |  Unmapped: {n_rods - len(elem_map)}")
    print()
    for comp, amp in amplitudes.items():
        print(f"  {comp:>3} = {amp:+.3f} V  "
              f"(order {MULTIPOLE_ORDERS[comp]}, "
              f"spec limit ±{info['max_amplitude'].get(comp, '-')} V)")
    print(f"{'─'*W}")
    print(f"  {'El':>4}  {'Electrode V':>12}  {'Supply V':>12}  "
          f"{'Channel [pol]':>16}  Note")
    print(f"  {'─'*(W-2)}")

    unmapped    = []
    hard_over   = []
    sign_issues = []
    elem_hard_limit = LSTAR_ELEMENTS[element].get('hard_limit', HARD_LIMIT_V)

    for k, v in enumerate(voltages):
        raw = elem_map.get(k)
        if raw is not None:
            ch, pol  = resolve_ch(raw)
            v_set    = v * pol
            pol_sym  = "+" if pol == +1 else "\u2212"
            ch_str   = f"{ch} [{pol_sym}]"
        else:
            ch, pol, v_set = None, +1, v
            ch_str = "- not mapped -"

        note = ""
        if abs(v_set) > elem_hard_limit:
            note = f"⚠ EXCEEDS HARD LIMIT ({elem_hard_limit} V)"
            hard_over.append(k)
        if ch is None:
            unmapped.append(k)
        elif v_set < -1e-6:
            sign_issues.append((k, ch, pol, float(v), float(v_set)))
            note = note + ("  " if note else "") + "⚠ NEGATIVE SET-POINT"

        print(f"  {k:>4}  {v:>+12.4f}  {v_set:>+12.4f}  {ch_str:>16}  {note}")

    print(f"{'─'*W}")
    print(f"  Max electrode V: {voltages.max():+.4f} V    "
          f"Min electrode V: {voltages.min():+.4f} V    "
          f"P-P: {voltages.max() - voltages.min():.4f} V")

    if unmapped:
        print(f"\n  NOTE: {len(unmapped)} electrode(s) not in channel map "
              f"(indices {unmapped}).")
        print(f"        They will be skipped.  Edit DEFAULT_CHANNEL_MAP "
              f"or use --map to add them.")

    if sign_issues:
        _print_sign_issues(sign_issues)
        if not args.force:
            sys.exit(
                "  Stopping.  Fix the polarity factors in the channel map,\n"
                "  re-wire the electrodes, or use --force to push anyway.\n"
                "  (--force is safe if your modules are bipolar HV; it will\n"
                "  result in wrongValue errors on unipolar 0MPV supplies.)")

    if hard_over:
        if not args.force:
            sys.exit(
                f"\n  ERROR: {len(hard_over)} electrode(s) exceed the hard limit "
                f"for {element} ({elem_hard_limit} V).\n"
                f"         Use --force to override.")
        else:
            print(f"\n  WARNING (--force): {len(hard_over)} electrode(s) exceed "
                  f"hard limit ({elem_hard_limit} V) - proceeding anyway.")

    if args.dry_run:
        print(f"\n  [DRY RUN]  Table printed above.  Nothing was sent.\n")
        return

    # ── Optional pre-flight: read hardware voltage ceilings ───────────────
    rdr = Client(args.host, V2C(args.read_community), port=args.port)
    hw_limits: dict[str, float | None] = {}

    if args.preflight:
        print(f"\n  Pre-flight: reading outputSupervisionMaxTerminalVoltage (.11) ...")
        hw_limits = await preflight_limits(rdr, elem_map)
        any_over = False
        for k, v in enumerate(voltages):
            raw = elem_map.get(k)
            if raw is None:
                continue
            ch, pol   = resolve_ch(raw)
            v_set     = v * pol
            ceiling   = hw_limits.get(ch)
            if ceiling is not None and abs(v_set) > ceiling:
                print(f"  ⚠  electrode {k:2d}  ({ch})  set-point {v_set:+.4f} V  "
                      f">  module ceiling {ceiling:+.4f} V")
                any_over = True
        if any_over:
            print(f"\n  Some set-points exceed module ceilings.  The MPOD will likely")
            print(f"  reject those channels with CommitFailed / notWritable (14).")
        else:
            print(f"  All set-points are within module ceilings.  Proceeding.\n")

    # ── Push to MPOD ──────────────────────────────────────────────────────
    wtr    = Client(args.host, V2C(args.write_community), port=args.port)
    sent   = 0
    failed: list[tuple[int, str, float, Exception]] = []
    print(f"\n  Pushing to MPOD @ {args.host} ...")

    for k, v in enumerate(voltages):
        raw = elem_map.get(k)
        if raw is None:
            continue
        ch, pol = resolve_ch(raw)
        v_set   = v * pol
        try:
            await wtr.set(oid_for(OID_VOLT_SET, ch), encode_float(v_set))
            pol_sym = "+" if pol == +1 else "\u2212"
            print(f"    electrode {k:2d}  ({ch} [{pol_sym}])  "
                  f"electrode {v:+9.4f} V  →  supply {v_set:+9.4f} V  ✓")
            sent += 1
        except Exception as exc:
            _report_write_error(k, ch, v_set, exc,
                                write_comm=args.write_community, element=element)
            failed.append((k, ch, v_set, exc))
        await asyncio.sleep(_WRITE_PAUSE)

    n_unmapped = n_rods - len(elem_map)
    print(f"\n  {'─'*44}")
    print(f"  Sent    : {sent}")
    print(f"  Failed  : {len(failed)}")
    print(f"  Skipped : {n_unmapped}  (unmapped electrodes)")
    print(f"  {'─'*44}")

    if failed:
        print(f"\n  Failed channels:")
        for k, ch, v, exc in failed:
            code, label = _decode_snmp_status(exc)
            print(f"    electrode {k:2d}  ({ch})  {v:+.4f} V  →  [{label}]")
        print(f"\n  Tip: run with --preflight to check hardware ceilings first.")
        print(f"       run with --dry-run to review the table without writing.\n")
    else:
        print()


# ══════════════════════════════════════════════════════════════════════════════
#  §8  Subcommand: zero
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_zero(args) -> None:
    """Set all mapped channels for an element to 0.000 V."""
    element = args.element
    ch_map  = load_channel_map(args.map)
    em      = ch_map.get(element, {})

    if not em:
        print(f"\n  No channels mapped for '{element}' - nothing to zero.\n")
        return

    print(f"\n  Zero  '{element}'  ({len(em)} mapped channel(s))")
    print(f"  {'─'*44}")

    if args.dry_run:
        for k, raw in sorted(em.items()):
            ch, pol = resolve_ch(raw)
            pol_sym = "+" if pol == +1 else "\u2212"
            print(f"  [DRY RUN]  electrode {k:2d}  ({ch} [{pol_sym}])  →  0.000 V")
        print()
        return

    wtr   = Client(args.host, V2C(args.write_community), port=args.port)
    sent  = 0
    failed: list[tuple[int, str, Exception]] = []
    for k, raw in sorted(em.items()):
        ch, pol = resolve_ch(raw)
        pol_sym = "+" if pol == +1 else "\u2212"
        try:
            await wtr.set(oid_for(OID_VOLT_SET, ch), encode_float(0.0))
            print(f"  electrode {k:2d}  ({ch} [{pol_sym}])  →  0.000 V  ✓")
            sent += 1
        except Exception as exc:
            _report_write_error(k, ch, 0.0, exc, write_comm=args.write_community)
            failed.append((k, ch, exc))
        await asyncio.sleep(_WRITE_PAUSE)

    print(f"\n  Zeroed: {sent}  |  Failed: {len(failed)}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  §9  Subcommand: readback
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_readback(args) -> None:
    """Read set voltage, measured voltage, and switch state for all mapped channels."""
    element = args.element
    ch_map  = load_channel_map(args.map)
    em      = ch_map.get(element, {})

    if not em:
        print(f"\n  No channels mapped for '{element}'.\n")
        return

    print(f"\n  Readback  -  Element: {element}  ({len(em)} channel(s))")
    print(f"  {'─'*68}")
    print(f"  {'El':>4}  {'Channel [pol]':>14}  "
          f"{'V_set (V)':>10}  {'V_meas (V)':>10}  {'Switch':>12}")
    print(f"  {'─'*68}")

    rdr = Client(args.host, V2C(args.read_community), port=args.port)
    for k, raw in sorted(em.items()):
        ch, pol = resolve_ch(raw)
        pol_sym = "+" if pol == +1 else "\u2212"
        ch_label = f"{ch} [{pol_sym}]"
        try:
            r  = await rdr.multiget([
                oid_for(OID_VOLT_SET,  ch),
                oid_for(OID_VOLT_MEAS, ch),
                oid_for(OID_SWITCH,    ch),
            ])
            vs = decode_float(r[0].value)
            vm = decode_float(r[1].value)
            sw = SWITCH_LABELS.get(decode_int(r[2].value), "?")
            vs_s = f"{vs:+.3f}" if vs is not None else "?"
            vm_s = f"{vm:+.3f}" if vm is not None else "?"
        except Exception as exc:
            vs_s = vm_s = sw = f"ERROR: {exc}"
        print(f"  {k:>4}  {ch_label:>14}  {vs_s:>10}  {vm_s:>10}  {sw:>12}")

    print(f"  {'─'*68}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  §10  Subcommand: show-map
# ══════════════════════════════════════════════════════════════════════════════

def cmd_show_map(args) -> None:
    """Display the electrode-to-channel wiring map for all elements."""
    ch_map = load_channel_map(args.map)

    source = args.map if args.map else "built-in DEFAULT_CHANNEL_MAP"
    print(f"\n  Electrode → MPOD Channel Map")
    print(f"  Source: {source}")
    print(f"  {'─'*42}")

    for elem_name, em in ch_map.items():
        info    = LSTAR_ELEMENTS.get(elem_name, {})
        n_rods  = info.get('n_rods', '?')
        n_wired = len(em)
        n_open  = (n_rods - n_wired) if isinstance(n_rods, int) else '?'

        print(f"\n  [{elem_name}]  {info.get('description', '')}  "
              f"({n_rods} rods  -  {n_wired} wired,  {n_open} unmapped)")

        if em:
            # Two-column layout so that 24-rod tables don't scroll forever
            items = sorted(em.items())
            pairs = [(items[i], items[i + 1] if i + 1 < len(items) else None)
                     for i in range(0, len(items), 2)]
            for left, right in pairs:
                lch, lpol = resolve_ch(left[1])
                lsym = "+" if lpol == +1 else "\u2212"
                left_s  = f"  electrode {left[0]:2d}  \u2192  {lch} [{lsym}]"
                if right:
                    rch, rpol = resolve_ch(right[1])
                    rsym  = "+" if rpol == +1 else "\u2212"
                    right_s = f"      electrode {right[0]:2d}  \u2192  {rch} [{rsym}]"
                else:
                    right_s = ""
                print(f"    {left_s}{right_s}")
        else:
            print("    (no channels mapped yet)")

    print()


# ══════════════════════════════════════════════════════════════════════════════
#  §10b  Subcommand: switch
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_switch(args) -> None:
    """
    Turn on or off the outputSwitch for mapped channels of an element.

    state choices: on | off
    --electrode N  : operate on one electrode only (default: all mapped)
    --channel NAME : operate on one raw MPOD channel name (e.g. u700)
    """
    element = args.element
    state   = SWITCH_ON if args.state.lower() == "on" else SWITCH_OFF
    label   = "On" if state == SWITCH_ON else "Off"
    ch_map  = load_channel_map(args.map)
    em      = dict(ch_map.get(element, {}))      # copy so we can filter

    if not em:
        print(f"\n  No channels mapped for '{element}' - nothing to switch.\n")
        return

    # ── Optional per-electrode or per-channel filter ─────────────────────
    if args.electrode is not None:
        if args.electrode not in em:
            sys.exit(f"  ERROR: electrode {args.electrode} not in channel map "
                     f"for '{element}'.\n"
                     f"  Mapped electrodes: {sorted(em.keys())}")
        em = {args.electrode: em[args.electrode]}
    elif args.channel is not None:
        # Filter by channel name (ignore polarity, compare name only)
        match = {k: raw for k, raw in em.items()
                 if resolve_ch(raw)[0] == args.channel}
        if not match:
            sys.exit(f"  ERROR: channel '{args.channel}' not found in map "
                     f"for '{element}'.")
        em = match

    print(f"\n  Switch  '{element}'  →  {label}  ({len(em)} channel(s))")
    print(f"  {'─'*44}")

    if args.dry_run:
        for k, raw in sorted(em.items()):
            ch, pol = resolve_ch(raw)
            pol_sym = "+" if pol == +1 else "\u2212"
            print(f"  [DRY RUN] electrode {k:2d}  ({ch} [{pol_sym}])  →  Switch {label}")
        print()
        return

    wtr  = Client(args.host, V2C(args.write_community), port=args.port)
    ok   = 0
    failed: list[tuple[int, str, Exception]] = []
    for k, raw in sorted(em.items()):
        ch, pol = resolve_ch(raw)
        pol_sym = "+" if pol == +1 else "\u2212"
        try:
            await wtr.set(oid_for(OID_SWITCH, ch), encode_int(state))
            print(f"  electrode {k:2d}  ({ch} [{pol_sym}])  →  {label}  ✓")
            ok += 1
        except Exception as exc:
            code, lbl = _decode_snmp_status(exc)
            print(f"  electrode {k:2d}  ({ch} [{pol_sym}])  →  {label}  ✗  [{lbl}]")
            failed.append((k, ch, exc))
        await asyncio.sleep(_WRITE_PAUSE)

    print(f"\n  Switched: {ok}  |  Failed: {len(failed)}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  §10c  Subcommand: limits
#         Reads outputSupervisionMinTermV (.13) and MaxTermV (.11) per channel.
#         Use this first when you see wrongValue (code 10) on negative voltages.
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_limits(args) -> None:
    """
    Read supervision voltage limits and switch state for all mapped channels.

    This is the diagnostic to run when you see wrongValue (status-code: 10)
    on negative voltage SET operations.  If MinTermV = 0.0 V for a channel,
    the module is configured as unipolar and will reject all negative voltages.
    Fix: set MinTermV to a negative value (e.g. -500 V) in isegControl, OR
    confirm the physical module supports negative output (bipolar iseg EHS).
    """
    element = args.element
    ch_map  = load_channel_map(args.map)
    em      = ch_map.get(element, {})

    if not em:
        print(f"\n  No channels mapped for '{element}'.\n")
        return

    W = 76
    print(f"\n{'─'*W}")
    print(f"  Supervision Limits  -  Element: {element}  "
          f"({len(em)} mapped channels)")
    print(f"  Host: {args.host}")
    print(f"{'─'*W}")
    print(f"  {'El':>4}  {'Channel [pol]':>14}  {'MinTermV (V)':>13}  "
          f"{'MaxTermV (V)':>13}  {'Switch':>8}  Note")
    print(f"  {'─'*68}")

    rdr     = Client(args.host, V2C(args.read_community), port=args.port)
    any_unipolar = False
    for k, raw in sorted(em.items()):
        ch, pol = resolve_ch(raw)
        pol_sym  = "+" if pol == +1 else "\u2212"
        ch_label = f"{ch} [{pol_sym}]"
        try:
            r = await rdr.multiget([
                oid_for(OID_VOLT_MIN, ch),
                oid_for(OID_VOLT_MAX, ch),
                oid_for(OID_SWITCH,   ch),
            ])
            vmin  = decode_float(r[0].value)
            vmax  = decode_float(r[1].value)
            sw    = SWITCH_LABELS.get(decode_int(r[2].value), "?")
            vmin_s = f"{vmin:+.1f}" if vmin is not None else "?"
            vmax_s = f"{vmax:+.1f}" if vmax is not None else "?"
            note = ""
            if vmin is not None and vmin >= 0.0:
                note = "\u2190 UNIPOLAR (negative V_set will fail)"
                any_unipolar = True
        except Exception as exc:
            vmin_s = vmax_s = sw = f"ERR({exc!s:.30})"
            note = ""
        print(f"  {k:>4}  {ch_label:>14}  {vmin_s:>13}  "
              f"{vmax_s:>13}  {sw:>8}  {note}")

    print(f"{'─'*W}")
    if any_unipolar:
        print(f"\n  ⚠  One or more channels have MinTermV \u2265 0 (UNIPOLAR mode).")
        print(f"     Supply set-points must be \u2265 0.  Negative set-points return")
        print(f"     wrongValue (code 10).")
        print(f"     For electrodes needing negative voltage on a unipolar supply:")
        print(f"       \u2022 Wire the electrode to the U\u2212 terminal and set polarity = -1")
        print(f"         in the channel map (v_set = v_electrode \u00d7 (\u22121) \u2265 0).")
        print(f"       \u2022 Or replace with bipolar iseg HV modules — then all")
        print(f"         polarity factors can be +1 and the map stays simple.")
    else:
        print(f"\n  All channels appear to be in bipolar mode (MinTermV < 0).")
    print()


# ══════════════════════════════════════════════════════════════════════════════
#  §11  CLI / argument parser / entry-point
# ══════════════════════════════════════════════════════════════════════════════

def _global_parent() -> argparse.ArgumentParser:
    """Shared flags inherited by every subcommand via parents=[...]."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--host",            default="192.168.55.8",
                   metavar="IP",
                   help="MPOD IP address  (default: 192.168.55.8)")
    p.add_argument("--read-community",  default="public",
                   metavar="STR",
                   help="SNMP read community  (default: public)")
    p.add_argument("--write-community", default="guru",
                   metavar="STR",
                   help="SNMP write community  (default: guru)")
    p.add_argument("--port",            default=161, type=int,
                   help="SNMP UDP port  (default: 161)")
    p.add_argument("--map",             default=None,
                   metavar="FILE",
                   help="JSON channel map file  (default: built-in)")
    return p


def build_parser() -> argparse.ArgumentParser:
    g = _global_parent()

    root = argparse.ArgumentParser(
        prog="lstar_mpod_ctl",
        description=(
            "LSTAR MPOD control - probe, set voltages, apply multipole configs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python lstar_mpod_ctl.py probe\n"
            "  python lstar_mpod_ctl.py multipole M --Q 100 --H -300 --O 300 --dry-run\n"
            "  python lstar_mpod_ctl.py multipole M --Q 100 --H -300 --O 300\n"
            '  python lstar_mpod_ctl.py multipole "(Q+oct)1" --Q -1600 --O -200\n'
            "  python lstar_mpod_ctl.py set u700 10.0\n"
            "  python lstar_mpod_ctl.py zero M\n"
            "  python lstar_mpod_ctl.py readback M\n"
            "  python lstar_mpod_ctl.py show-map\n"
            "  python lstar_mpod_ctl.py switch M on          # turn on all M channels\n"
            "  python lstar_mpod_ctl.py switch M off         # turn off all M channels\n"
            "  python lstar_mpod_ctl.py switch M on --electrode 3  # one electrode\n"
            "  python lstar_mpod_ctl.py switch M on --channel u703 # by channel name\n"
            "  python lstar_mpod_ctl.py limits M             # diagnose wrongValue (code 10)\n"
        ),
    )
    sub = root.add_subparsers(dest="command", title="subcommands", required=True)

    # ── probe ──────────────────────────────────────────────────────────────
    sub.add_parser(
        "probe", parents=[g],
        help="Read and display all MPOD channels (read-only).")

    # ── set ────────────────────────────────────────────────────────────────
    p_set = sub.add_parser(
        "set", parents=[g],
        help="Write a voltage to one MPOD channel and verify with readback.")
    p_set.add_argument("channel",
                       help="MPOD channel name, e.g. u700")
    p_set.add_argument("voltage", type=float,
                       help="Target voltage in volts")
    p_set.add_argument("--dry-run", action="store_true",
                       help="Print intent; do not write to hardware")
    p_set.add_argument("--force",   action="store_true",
                       help="Bypass HARD_LIMIT_V safety check")

    # ── multipole ──────────────────────────────────────────────────────────
    p_mp = sub.add_parser(
        "multipole", parents=[g],
        help="Compute squirrel-cage multipole voltages and push to MPOD.")
    p_mp.add_argument(
        "element",
        choices=list(LSTAR_ELEMENTS),
        help="LSTAR element name  (M | (Q+oct)1 | (Q+oct)2)")
    p_mp.add_argument("--Q",  type=float, default=None, metavar="V",
                      help="Quadrupole amplitude [V]  (order 2)")
    p_mp.add_argument("--H",  type=float, default=None, metavar="V",
                      help="Hexapole amplitude [V]    (order 3, M only)")
    p_mp.add_argument("--O",  type=float, default=None, metavar="V",
                      help="Octupole amplitude [V]    (order 4)")
    p_mp.add_argument("--De", type=float, default=None, metavar="V",
                      help="Decapole amplitude [V]    (order 5, M only)")
    p_mp.add_argument("--Do", type=float, default=None, metavar="V",
                      help="Dodecapole amplitude [V]  (order 6, M only)")
    p_mp.add_argument("--dry-run", action="store_true",
                      help="Print voltage table; do not write to hardware")
    p_mp.add_argument("--force",    action="store_true",
                      help="Override LSTAR spec-limit and HARD_LIMIT_V checks")
    p_mp.add_argument("--preflight", action="store_true",
                      help="Read module hardware ceilings (OID .11) before writing "
                           "and warn on any channels that would exceed them")

    # ── zero ───────────────────────────────────────────────────────────────
    p_zero = sub.add_parser(
        "zero", parents=[g],
        help="Set all mapped channels for an element to 0.000 V.")
    p_zero.add_argument("element", choices=list(LSTAR_ELEMENTS))
    p_zero.add_argument("--dry-run", action="store_true",
                        help="Print what would be zeroed; do not write")

    # ── readback ───────────────────────────────────────────────────────────
    p_rb = sub.add_parser(
        "readback", parents=[g],
        help="Read back set and measured voltages for all mapped channels.")
    p_rb.add_argument("element", choices=list(LSTAR_ELEMENTS))

    # ── show-map ───────────────────────────────────────────────────────────
    sub.add_parser(
        "show-map", parents=[g],
        help="Display electrode → MPOD channel wiring map.")

    # ── switch ─────────────────────────────────────────────────────────────
    p_sw = sub.add_parser(
        "switch", parents=[g],
        help="Turn on or off all (or one) mapped channel(s) for an element.")
    p_sw.add_argument("element", choices=list(LSTAR_ELEMENTS),
                      help="LSTAR element name")
    p_sw.add_argument("state", choices=["on", "off"],
                      help="Target switch state")
    p_sw.add_argument("--electrode", type=int, default=None, metavar="N",
                      help="Operate on a single electrode by index (default: all)")
    p_sw.add_argument("--channel", default=None, metavar="NAME",
                      help="Operate on a single channel by name, e.g. u703")
    p_sw.add_argument("--dry-run", action="store_true",
                      help="Print intent; do not write to hardware")

    # ── limits ─────────────────────────────────────────────────────────────
    p_lim = sub.add_parser(
        "limits", parents=[g],
        help="Read supervision voltage limits (MinTermV/MaxTermV) per channel.  "
             "Run this when you see wrongValue (code 10) on negative voltage SETs.")
    p_lim.add_argument("element", choices=list(LSTAR_ELEMENTS),
                       help="LSTAR element name")

    return root


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    dispatch = {
        "probe":     lambda: asyncio.run(cmd_probe(args)),
        "set":       lambda: asyncio.run(cmd_set(args)),
        "multipole": lambda: asyncio.run(cmd_multipole(args)),
        "zero":      lambda: asyncio.run(cmd_zero(args)),
        "readback":  lambda: asyncio.run(cmd_readback(args)),
        "show-map":  cmd_show_map,
        "switch":    lambda: asyncio.run(cmd_switch(args)),
        "limits":    lambda: asyncio.run(cmd_limits(args)),
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return

    # show-map is synchronous, everything else is async via asyncio.run()
    if args.command == "show-map":
        fn(args)
    else:
        fn()


if __name__ == "__main__":
    main()
