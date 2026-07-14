"""EPICS IOC for one LSTAR multipole element on the WIENER MPOD crate.

A thin adapter, mapping EPICS PVs onto the existing primitives in ``lstar_mpod_ctl.py``. 

Safety stays server-side, so a ``Push`` runs the same three checks the CLI does
(spec ``check_amplitudes`` -> ``HARD_LIMIT`` -> polarity ``_check_voltage_signs``)
before any SNMP write.

Run it (dry-run)::

    python ioc/mpod_ioc.py --element M --dry-run --list-pvs
    # then:
    caput LSTAR:M:Q 50
    caput LSTAR:M:O 100
    caput LSTAR:M:Push 1
    caget LSTAR:M:Status LSTAR:M:CH0:Vset
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run  # noqa: E402

from lstar_mpod_ctl import (  # noqa: E402
    HARD_LIMIT_V,
    LSTAR_ELEMENTS,
    MULTIPOLE_ORDERS,
    OID_CURR_MEAS,
    OID_SWITCH,
    OID_VOLT_MAX,
    OID_VOLT_MEAS,
    OID_VOLT_MIN,
    OID_VOLT_SET,
    Client,
    V2C,
    check_amplitudes,
    compute_voltages,
    decode_float,
    decode_int,
    encode_float,
    load_channel_map,
    oid_for,
    resolve_ch,
)

_COMPONENTS = ["Q", "H", "O", "De", "Do"]   # superset of MULTIPOLE_ORDERS keys
_MAX_RODS = 24


def element_to_pvname(element: str) -> str:
    """PV-safe element name: '(Q+oct)1' -> 'QO1' (EPICS names can't hold ()+ )."""
    return {"(Q+oct)1": "QO1", "(Q+oct)2": "QO2"}.get(element, element)


# ---------------------------------------------------------------------------
# Per-rod PV group, with one per pre-declared rod CH0..CH23.
# ---------------------------------------------------------------------------
class RodGroup(PVGroup):
    vset = pvproperty(value=0.0, name="Vset", units="V", precision=3, read_only=True,
                      doc="Last commanded supply set-point for this rod [V]")
    vread = pvproperty(value=0.0, name="Vread", units="V", precision=3, read_only=True,
                       doc="Measured output voltage (SNMP) [V]")
    iread = pvproperty(value=0.0, name="Iread", units="A", precision=6, read_only=True,
                       doc="Measured output current (SNMP) [A]")
    power = pvproperty(value=False, name="Power", read_only=True,
                       doc="Channel output switch On/Off (SNMP)")
    chan = pvproperty(value="", name="Chan", max_length=16, read_only=True,
                      report_as_string=True, doc="Mapped MPOD channel, e.g. u700")
    max_termv = pvproperty(value=0.0, name="MaxTermV", units="V", precision=1,
                           read_only=True,
                           doc="Crate outputSupervisionMaxTerminalVoltage (.11) [V]")
    min_termv = pvproperty(value=0.0, name="MinTermV", units="V", precision=1,
                           read_only=True,
                           doc="Crate outputSupervisionMinTerminalVoltage (.13) [V]")
    bipolar = pvproperty(value=False, name="Bipolar", read_only=True,
                         doc="Derived: this channel's module is bipolar (MinTermV < 0)")


# ---------------------------------------------------------------------------
# Top-level IOC (for one element)
# ---------------------------------------------------------------------------
class MPODIOC(PVGroup):
    # Component setpoints (superset, only the element's own components are used).
    Q = pvproperty(value=0.0, units="V", precision=3, doc="Quadrupole amplitude [V]")
    H = pvproperty(value=0.0, units="V", precision=3, doc="Hexapole amplitude [V]")
    O = pvproperty(value=0.0, units="V", precision=3, doc="Octupole amplitude [V]")
    De = pvproperty(value=0.0, units="V", precision=3, doc="Decapole amplitude [V]")
    Do = pvproperty(value=0.0, units="V", precision=3, doc="Dodecapole amplitude [V]")

    push = pvproperty(value=False, name="Push",
                      doc="Compute -> safety-check -> SNMP-set all mapped rods")
    zero = pvproperty(value=False, name="Zero", doc="Set all mapped rods to 0 V")
    status = pvproperty(value="INIT", name="Status", max_length=20, read_only=True,
                        report_as_string=True,
                        doc="OK / SPEC_LIMIT / HARD_LIMIT / POLARITY_ERR / SNMP_ERR / OFFLINE / DRY_RUN")
    msg = pvproperty(value="", name="Msg", max_length=200, read_only=True,
                     report_as_string=True, doc="Last status message")
    comm = pvproperty(value=False, name="Comm", read_only=True,
                      doc="Crate reachable over SNMP")
    hard_limit = pvproperty(value=HARD_LIMIT_V, name="HardLimit", units="V", precision=1,
                            read_only=True, doc="Per-rod absolute ceiling for this element [V]")
    element_name = pvproperty(value="", name="Element", max_length=20, read_only=True,
                              report_as_string=True, doc="LSTAR element this IOC controls")
    force = pvproperty(value=False, name="Force",
                       doc="Bypass spec/hard/window checks (mirrors CLI --force). Also "
                           "required to Push when crate limits are unreadable (offline).")

    for _k in range(_MAX_RODS):
        locals()[f"ch{_k}"] = SubGroup(RodGroup, prefix=f"CH{_k}:")
    del _k

    def __init__(self, *args, element="M", host="192.168.55.8", read_community="public",
                 write_community="guru", port=161, dry_run=False, map_file=None, **kwargs):
        super().__init__(*args, **kwargs)
        if element not in LSTAR_ELEMENTS:
            raise SystemExit(f"Unknown element {element!r}. "
                             f"Valid: {list(LSTAR_ELEMENTS)}")
        self.element = element
        self.info = LSTAR_ELEMENTS[element]
        self.n_rods = self.info["n_rods"]
        self.components = self.info["components"]
        self.elem_hard = self.info.get("hard_limit", HARD_LIMIT_V)
        self.dry_run = bool(dry_run)
        self.host, self.port = host, port
        self.read_community, self.write_community = read_community, write_community
        self.ch_map = load_channel_map(map_file).get(element, {})
        # per-rod crate limits populated by scan, None until read from the crate
        self.max_termv = [None] * _MAX_RODS
        self.min_termv = [None] * _MAX_RODS
        # SNMP clients (constructing is cheap and does no I/O until a call is made)
        self._rdr = Client(host, V2C(read_community), port=port)
        self._wtr = Client(host, V2C(write_community), port=port)

    # ---- gather setpoints for the components this element supports ----
    def _amplitudes(self) -> dict[str, float]:
        vals = {"Q": self.Q, "H": self.H, "O": self.O, "De": self.De, "Do": self.Do}
        return {c: float(vals[c].value) for c in self.components if c in MULTIPOLE_ORDERS}

    # ---- server-side safety, per-channel against the crate's own limits ----
    #
    # !!! HARDWARE-VERIFY !!!  This path has only been exercised in
    # dry-run. ASSUMES: (a) outputSupervisionMinTermV
    # (.13) < 0 iff the module is bipolar, (b) MinTermV (.13) / MaxTermV (.11)
    # decode as plain WIENER floats via decode_float, (c) a single [min, max]
    # window fully bounds a valid set-point. 

    def _safety_check(self, amps, volts, force):
        """Return (ok, status, message).

        force=True mirrors the CLI's --force: bypass spec, hard-limit, and the
        per-channel window (used deliberately, or when the crate is offline so
        limits can't be read). Otherwise each mapped rod's supply set-point must
        sit inside that channel's crate-declared [MinTermV, MaxTermV] window --
        which honors a MIXED unipolar/bipolar crate automatically, per channel.
        """
        # spec limits (check_amplitudes warns + passes when force=True)
        if not check_amplitudes(self.element, amps, force=force):
            return False, "SPEC_LIMIT", "Amplitude exceeds LSTAR spec limit"
        if force:
            return True, "FORCED", "checks bypassed (Force on)"
        eps = 1e-6
        # absolute hard-limit backstop, crate-independent, applies in dry-run too
        for k in range(self.n_rods):
            if k not in self.ch_map:
                continue
            v_set = float(volts[k]) * resolve_ch(self.ch_map[k])[1]
            if abs(v_set) > self.elem_hard:
                return False, "HARD_LIMIT", (f"rod {k} = {v_set:+.1f} V exceeds hard "
                                             f"limit {self.elem_hard} V")
        if self.dry_run:
            return True, "OK", "dry-run: spec + hard-limit OK (crate window not checked)"
        # per-channel window against crate's own MinTermV/MaxTermV (live only)
        unknown, out = [], []
        for k in range(self.n_rods):
            if k not in self.ch_map:
                continue
            v_set = float(volts[k]) * resolve_ch(self.ch_map[k])[1]
            mn, mx = self.min_termv[k], self.max_termv[k]
            if mn is None or mx is None:
                unknown.append(k)
            elif not (mn - eps <= v_set <= mx + eps):
                out.append(f"rod {k}={v_set:+.1f} outside [{mn:+.1f}, {mx:+.1f}]")
        if out:
            return False, "LIMIT_ERR", "; ".join(out)
        if unknown:
            return False, "LIMITS_UNKNOWN", (f"crate limits unread for rods {unknown} "
                                             "(crate offline?); set Force to override")
        return True, "OK", "within per-channel crate limits"

    @push.putter
    async def push(self, instance, value):
        if not value:
            return False
        amps = self._amplitudes()
        volts = compute_voltages(self.element, amps)
        ok, status, message = self._safety_check(amps, volts, bool(self.force.value))
        if not ok:
            await self.status.write(status)
            await self.msg.write(message)
            raise ValueError(message)      # refuse the push
        if status == "FORCED":
            await self.msg.write("WARNING: Force is ON -- safety checks bypassed")
        # write the computed set-points to the Vset mirror
        for k in range(self.n_rods):
            if k in self.ch_map:
                pol = resolve_ch(self.ch_map[k])[1]
                await getattr(self, f"ch{k}").vset.write(float(volts[k]) * pol)
        if self.dry_run:
            await self.status.write("DRY_RUN")
            await self.msg.write(f"[dry-run] computed {self.n_rods} rods, nothing sent")
            return False
        # SNMP write each mapped rod
        try:
            for k in range(self.n_rods):
                if k not in self.ch_map:
                    continue
                ch, pol = resolve_ch(self.ch_map[k])
                await self._wtr.set(oid_for(OID_VOLT_SET, ch), encode_float(float(volts[k]) * pol))
            await self.comm.write(True)
            await self.status.write("OK")
            await self.msg.write(f"Pushed {len(self.ch_map)} rods at "
                                 + datetime.now().strftime("%H:%M:%S"))
        except Exception as exc:  # SNMP failure -> report, don't crash IOC
            await self.comm.write(False)
            await self.status.write("SNMP_ERR")
            await self.msg.write(f"SNMP write failed: {exc}")
            raise
        return False

    @zero.putter
    async def zero(self, instance, value):
        if not value:
            return False
        for k in range(self.n_rods):
            if k in self.ch_map:
                await getattr(self, f"ch{k}").vset.write(0.0)
        if self.dry_run:
            await self.status.write("DRY_RUN")
            await self.msg.write("[dry-run] would zero all mapped rods")
            return False
        try:
            for k in range(self.n_rods):
                if k in self.ch_map:
                    ch = resolve_ch(self.ch_map[k])[0]
                    await self._wtr.set(oid_for(OID_VOLT_SET, ch), encode_float(0.0))
            await self.comm.write(True)
            await self.status.write("OK")
            await self.msg.write("Zeroed all mapped rods")
        except Exception as exc:
            await self.comm.write(False)
            await self.status.write("SNMP_ERR")
            await self.msg.write(f"SNMP zero failed: {exc}")
            raise
        return False

    # ---- prime static readbacks at startup ----
    @push.startup
    async def push(self, instance, async_lib):
        await self.element_name.write(self.element)
        await self.hard_limit.write(float(self.elem_hard))
        for k in range(self.n_rods):
            ch = getattr(self, f"ch{k}")
            if k in self.ch_map:
                await ch.chan.write(resolve_ch(self.ch_map[k])[0])
        await self.status.write("DRY_RUN" if self.dry_run else "READY")
        await self.msg.write(f"IOC ready for element {self.element} "
                             f"({self.n_rods} rods, {len(self.ch_map)} mapped)")

    # ---- poll measured voltage / current / switch for mapped rods ----
    @comm.scan(period=2.0)
    async def comm(self, instance, async_lib):
        if self.dry_run or not self.ch_map:
            return
        try:
            for k in range(self.n_rods):
                if k not in self.ch_map:
                    continue
                ch = resolve_ch(self.ch_map[k])[0]
                res = await self._rdr.multiget([
                    oid_for(OID_VOLT_MEAS, ch),
                    oid_for(OID_CURR_MEAS, ch),
                    oid_for(OID_SWITCH, ch),
                    oid_for(OID_VOLT_MAX, ch),   # .11 upper ceiling
                    oid_for(OID_VOLT_MIN, ch),   # .13 lower bound (< 0 => bipolar)
                ])
                rod = getattr(self, f"ch{k}")
                vm = decode_float(res[0].value)
                im = decode_float(res[1].value)
                sw = decode_int(res[2].value)
                mx = decode_float(res[3].value)
                mn = decode_float(res[4].value)
                if vm is not None:
                    await rod.vread.write(vm)
                if im is not None:
                    await rod.iread.write(im)
                if sw is not None:
                    await rod.power.write(bool(sw))
                if mx is not None:
                    self.max_termv[k] = mx
                    await rod.max_termv.write(mx)
                if mn is not None:
                    self.min_termv[k] = mn
                    await rod.min_termv.write(mn)
                    await rod.bipolar.write(mn < 0)
            await self.comm.write(True)
        except Exception as exc:
            await self.comm.write(False)
            await self.status.write("OFFLINE")
            await self.msg.write(f"SNMP read failed: {exc}")


def main() -> None:
    # Parse our own options first, then hand the rest to caproto
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--element", default="M")
    pre.add_argument("--host", default="192.168.55.8")
    pre.add_argument("--read-community", default="public")
    pre.add_argument("--write-community", default="guru")
    pre.add_argument("--port", type=int, default=161)
    pre.add_argument("--map", dest="map_file", default=None)
    pre.add_argument("--dry-run", action="store_true")
    ours, remaining = pre.parse_known_args()
    sys.argv = [sys.argv[0]] + remaining

    prefix = f"LSTAR:{element_to_pvname(ours.element)}:"
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=prefix,
        desc=f"LSTAR MPOD EPICS IOC for element {ours.element}.",
    )
    ioc = MPODIOC(element=ours.element, host=ours.host,
                  read_community=ours.read_community, write_community=ours.write_community,
                  port=ours.port, dry_run=ours.dry_run, map_file=ours.map_file,
                  **ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
