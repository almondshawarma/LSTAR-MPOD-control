"""Manual per-channel EPICS IOC for *any* WIENER MPOD crate.

The EPICS equivalent of the GUI's "manual channel control": address raw MPOD
channels by name (u700, u701, ...) on an arbitrary crate. Reuses the 
SNMP primitives from ``lstar_mpod_ctl.py``. 

Command vs readback are separate PVs (like Vset/Vread):
  * ``Vset`` (write) / ``Vread`` (read)   -- set-point vs measured voltage
  * ``Power`` (write) / ``PowerRead`` (read) -- commanded switch vs actual state
This keeps the periodic read-scan from ever fighting a command PV.

Writes are bounded by an absolute ``HARD_LIMIT_V`` backstop and once the scan
has read them, the channel's own crate window ``[MinTermV, MaxTermV]``.

Examples::

    # observe only (safe), watch two channels on the spare crate:
    python ioc/mpod_manual_ioc.py --host 192.168.55.6 --channels u700,u701
    #   caget MPOD:u700:Vread ; camonitor MPOD:u700:PowerRead

    # enable commands:
    python ioc/mpod_manual_ioc.py --host 192.168.55.6 --channels u700 --allow-write
    #   caput MPOD:u700:Power 1 ; caput MPOD:u700:Vset 5
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
    OID_CURR_MEAS,
    OID_SWITCH,
    OID_VOLT_MAX,
    OID_VOLT_MEAS,
    OID_VOLT_MIN,
    OID_VOLT_SET,
    Client,
    V2C,
    decode_float,
    decode_int,
    encode_float,
    encode_int,
    oid_for,
)

_EPS = 1e-6


# ---------------------------------------------------------------------------
# One channel, prefix carries the channel name, e.g. "MPOD:u700:"
# ---------------------------------------------------------------------------
class ManualChannel(PVGroup):
    vset = pvproperty(value=0.0, name="Vset", units="V", precision=3,
                      doc="Commanded set-point [V] (writable only with --allow-write)")
    vread = pvproperty(value=0.0, name="Vread", units="V", precision=3, read_only=True,
                       doc="Measured output voltage (SNMP) [V]")
    iread = pvproperty(value=0.0, name="Iread", units="A", precision=6, read_only=True,
                       doc="Measured output current (SNMP) [A]")
    power = pvproperty(value=False, name="Power",
                       doc="Commanded output switch (writable only with --allow-write)")
    power_read = pvproperty(value=False, name="PowerRead", read_only=True,
                            doc="Actual output switch state (SNMP)")
    max_termv = pvproperty(value=0.0, name="MaxTermV", units="V", precision=1, read_only=True,
                           doc="Crate outputSupervisionMaxTerminalVoltage (.11) [V]")
    min_termv = pvproperty(value=0.0, name="MinTermV", units="V", precision=1, read_only=True,
                           doc="Crate outputSupervisionMinTerminalVoltage (.13) [V]")
    bipolar = pvproperty(value=False, name="Bipolar", read_only=True,
                         doc="Derived: this channel's module is bipolar (MinTermV < 0)")
    name = pvproperty(value="", name="Name", max_length=16, read_only=True,
                      report_as_string=True, doc="Raw MPOD channel name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # prefix like "MPOD:u700:" -> channel "u700"
        self.channel = self.prefix.rstrip(":").rsplit(":", 1)[-1]

    @vset.putter
    async def vset(self, instance, value):
        return await self.parent.write_voltage(self.channel, float(value))

    @power.putter
    async def power(self, instance, value):
        return await self.parent.switch(self.channel, bool(value))


# ---------------------------------------------------------------------------
# Base IOC (global PVs + SNMP logic), concrete class adds the channel SubGroups
# ---------------------------------------------------------------------------
class _ManualMPODBase(PVGroup):
    comm = pvproperty(value=False, name="Comm", read_only=True,
                      doc="Crate reachable over SNMP")
    allow_write = pvproperty(value=False, name="AllowWrite", read_only=True,
                             doc="Writes permitted (set at startup from --allow-write)")
    msg = pvproperty(value="", name="Msg", max_length=200, read_only=True,
                     report_as_string=True, doc="Last status message")

    def __init__(self, *args, channels=(), host="192.168.55.6", read_community="public",
                 write_community="guru", port=161, dry_run=False, allow_write=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels = list(channels)
        self.host, self.port = host, port
        self.allow = bool(allow_write)
        self.dry_run = bool(dry_run)
        self.min_termv: dict[str, float] = {}
        self.max_termv: dict[str, float] = {}
        self._rdr = Client(host, V2C(read_community), port=port)
        self._wtr = Client(host, V2C(write_community), port=port)

    def _sub(self, channel: str):
        return getattr(self, f"ch_{channel}")

    # ---- writes: only ever reached by an EXTERNAL caput to Vset/Power ----
    # (the scan touches read-only PVs only, so it doesn't re-enter these).
    async def write_voltage(self, channel: str, value: float):
        if not self.allow:
            await self.msg.write(f"REFUSED {channel} Vset: read-only (start with --allow-write)")
            raise PermissionError("read-only")
        if abs(value) > HARD_LIMIT_V:
            await self.msg.write(f"REFUSED {channel} Vset={value:+.1f}: exceeds HARD_LIMIT_V {HARD_LIMIT_V} V")
            raise ValueError("hard limit")
        mn, mx = self.min_termv.get(channel), self.max_termv.get(channel)
        if mn is not None and mx is not None and not (mn - _EPS <= value <= mx + _EPS):
            await self.msg.write(f"REFUSED {channel} Vset={value:+.1f}: outside crate window [{mn:+.1f}, {mx:+.1f}]")
            raise ValueError("outside window")
        if self.dry_run:
            await self.msg.write(f"[dry-run] would set {channel} = {value:+.3f} V")
            return value
        try:
            await self._wtr.set(oid_for(OID_VOLT_SET, channel), encode_float(value))
            await self.comm.write(True)
            await self.msg.write(f"set {channel} = {value:+.3f} V at " + datetime.now().strftime("%H:%M:%S"))
            return value
        except Exception as exc:
            await self.comm.write(False)
            await self.msg.write(f"SNMP set {channel} failed: {exc}")
            raise

    async def switch(self, channel: str, on: bool):
        if not self.allow:
            await self.msg.write(f"REFUSED {channel} Power: read-only (start with --allow-write)")
            raise PermissionError("read-only")
        if self.dry_run:
            await self.msg.write(f"[dry-run] would switch {channel} {'On' if on else 'Off'}")
            return on
        try:
            await self._wtr.set(oid_for(OID_SWITCH, channel), encode_int(1 if on else 0))
            await self.comm.write(True)
            await self.msg.write(f"switched {channel} {'On' if on else 'Off'}")
            return on
        except Exception as exc:
            await self.comm.write(False)
            await self.msg.write(f"SNMP switch {channel} failed: {exc}")
            raise

    # ---- startup: fast, no SNMP (no park and stall) ----
    @allow_write.startup
    async def allow_write(self, instance, async_lib):
        await self.allow_write.write(self.allow)
        for c in self.channels:
            await self._sub(c).name.write(c)
        await self.msg.write(("WRITE ENABLED" if self.allow else "READ-ONLY")
                             + f" | host {self.host} | {len(self.channels)} channel(s)")

    # ---- periodic read of every channel (read-only PVs only) ----
    @comm.scan(period=2.0)
    async def comm(self, instance, async_lib):
        try:
            for c in self.channels:
                res = await self._rdr.multiget([
                    oid_for(OID_VOLT_MEAS, c),
                    oid_for(OID_CURR_MEAS, c),
                    oid_for(OID_SWITCH, c),
                    oid_for(OID_VOLT_MAX, c),
                    oid_for(OID_VOLT_MIN, c),
                ])
                sub = self._sub(c)
                vm = decode_float(res[0].value)
                im = decode_float(res[1].value)
                sw = decode_int(res[2].value)
                mx = decode_float(res[3].value)
                mn = decode_float(res[4].value)
                if vm is not None:
                    await sub.vread.write(vm)
                if im is not None:
                    await sub.iread.write(im)
                if sw is not None:
                    await sub.power_read.write(bool(sw))
                if mx is not None:
                    self.max_termv[c] = mx
                    await sub.max_termv.write(mx)
                if mn is not None:
                    self.min_termv[c] = mn
                    await sub.min_termv.write(mn)
                    await sub.bipolar.write(mn < 0)
            await self.comm.write(True)
        except Exception as exc:
            await self.comm.write(False)
            await self.msg.write(f"SNMP read failed: {exc}")


def _make_ioc_class(channels):
    """Build a concrete IOC class with one SubGroup per requested channel."""
    ns = {f"ch_{c}": SubGroup(ManualChannel, prefix=f"{c}:") for c in channels}
    meta = type(_ManualMPODBase)                       # caproto's PVGroup metaclass
    return meta("ManualMPODIOC", (_ManualMPODBase,), ns)


def main() -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--channels", default="", help="comma-separated names, e.g. u700,u701")
    pre.add_argument("--host", default="192.168.55.6")
    pre.add_argument("--read-community", default="public")
    pre.add_argument("--write-community", default="guru")
    pre.add_argument("--port", type=int, default=161)
    pre.add_argument("--dry-run", action="store_true", help="preview writes; send no SNMP")
    pre.add_argument("--allow-write", action="store_true", help="permit Vset/Power writes")
    ours, remaining = pre.parse_known_args()
    sys.argv = [sys.argv[0]] + remaining

    channels = [c.strip() for c in ours.channels.split(",") if c.strip()]
    if not channels:
        sys.exit("Give at least one channel, e.g.  --channels u700,u701")

    ioc_options, run_options = ioc_arg_parser(
        default_prefix="MPOD:",
        desc="Manual per-channel MPOD EPICS IOC (read-only unless --allow-write).",
    )
    ioc_cls = _make_ioc_class(channels)
    ioc = ioc_cls(channels=channels, host=ours.host, read_community=ours.read_community,
                  write_community=ours.write_community, port=ours.port,
                  dry_run=ours.dry_run, allow_write=ours.allow_write, **ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
