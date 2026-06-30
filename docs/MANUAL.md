# LSTAR MPOD Control Manual

This is the reference document for actually running the system with all commands,
the channel-map format, troubleshooting, and the standalone diagnostic scripts.

---

## 1. Safety first

Three independent checks run before any voltage reaches the crate, and they exist
to catch *different* mistakes, with none being a substitute for the others.

1. **Spec limits**: (`max_amplitude`, from LSTAR limitations) the physics-valid range
   for each multipole component on each element. Exceeded values are rejected
   unless you pass `--force`, which prints a warning and proceeds anyway.
2. **Hard limit**: (`hard_limit` per element, `HARD_LIMIT_V` as an absolute
   ceiling) a backstop independent of the spec table. This is the last line of
   defense against a typo or a bad config file and is *not* meant to be routinely
   overridden.
3. **Polarity / sign check**: (`_check_voltage_signs`) runs as a preflight before
   any multipole push. Unipolar 0MPV LV modules can only accept set-points ≥ 0, so if
   the computed electrode voltage and the channel's polarity factor would produce a
   negative set-point on a unipolar channel, then the push aborts with the offending
   electrodes listed before anything is written.

**Operating rules of thumb:**

- `probe` and `readback` are always read-only and safe to run at any time.
- `multipole ... --dry-run` prints the full voltage table without touching
  hardware. Use it to sanity-check a configuration before pushing.
- `zero` / `multipole` / `set` never switch a channel on or off. The MPOD channel
  must already be On for a new set-point to take effect on the output. Use
  `switch` explicitly to turn channels on/off.
- `--force` exists for edge cases, not routine use. If you find yourself reaching
  for it often, the spec limit or hard limit is probably wrong, not the situation.

---

## 2. Environment setup

```bash
git clone https://github.com/almondshawarma/LSTAR-MPOD-control
cd lstar-mpod-control

# Create a project-local virtual environment (don't install into your global Python)
python -m venv .venv

# Activate it
source .venv/bin/activate        # macOS/Linux
.venv\Scripts\activate           # Windows cmd/PowerShell

pip install -r requirements.txt
```

In the case of older lab machines (which I encountered), Python 3.11 may be installed but not the
system default. If `python --version` doesn't report 3.11, use the Python launcher
to create the venv against the right interpreter:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If `lstar-ctl`/`lstar-gui` commands have been set up via `pip install -e .`, you can
run them directly instead of `python lstar_mpod_ctl.py ...`. Otherwise, run the
scripts from inside the project directory as shown throughout the manual.

---

## 3. CLI command reference

All commands accept the following global options:

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `192.168.55.8` | MPOD crate IP |
| `--read-community` | `public` | SNMP read community string |
| `--write-community` | `guru` | SNMP write community string |
| `--port` | `161` | SNMP UDP port |
| `--map` | built-in `DEFAULT_CHANNEL_MAP` | Path to a JSON channel-map file, if overriding the built-in map |

### `probe`

Read and display every populated MPOD channel, with the name, set/measured voltage,
set/measured current, switch state, decoded status flags.

```bash
python src/lstar_mpod_ctl.py probe
```

This is always safe, and the first thing to run when you're not sure what state the
crate is in.

### `show-map`

Print the electrode to channel wiring map for every element, including which
elements have no channels wired yet.

```bash
python src/lstar_mpod_ctl.py show-map
```

### `set CHANNEL VOLTAGE`

Write a voltage to a single named channel, then read it back to verify.

```bash
python src/lstar_mpod_ctl.py set u700 10.0
```

Use this for one-off manual testing, not for normal multipole operation.

### `multipole ELEMENT [--Q] [--H] [--O] [--De] [--Do] [--dry-run] [--force]`

Compute per-electrode voltages for an element from physics amplitudes, run the
safety checks, and push to the MPOD.

```bash
# Dry-run, prints the voltage table, writes nothing
python src/lstar_mpod_ctl.py multipole M --Q 100 --H -300 --O 300 --dry-run

# Actually push
python src/lstar_mpod_ctl.py multipole M --Q 100 --H -300 --O 300 --De 100 --Do 100

# Element names with parentheses need shell-quoting
python src/lstar_mpod_ctl.py multipole "(Q+oct)1" --Q -1600 --O -200
```

Only pass the components that element actually supports. (check `LSTAR_ELEMENTS` or
`show-map`) Passing an unsupported component raises an error rather than
silently ignoring it.

### `zero ELEMENT`

Set every mapped channel for an element to 0 V, does not switch channels off.

```bash
python src/lstar_mpod_ctl.py zero M
```

### `readback ELEMENT`

Read back the current measured state of every mapped channel for an element.

```bash
python src/lstar_mpod_ctl.py readback M
```

### `switch`, `limits`

Additional subcommands for toggling channel on/off state directly and inspecting
the active limit table. Run `python lstar_mpod_ctl.py switch --help` /
`limits --help` for the full flag list. Both follow the same `--host`/community
conventions as everything else.

---

## 4. Channel map format

The channel map connects a physics-level electrode index to a physical MPOD
channel name and a polarity factor:

```json
{
  "M":        {"0": "u700", "1": ["u701", -1]},
  "(Q+oct)1": {}
}
```

Each entry is either:

- `"u700"`, shorthand for `("u700", +1)`, electrode wired to the module's U+
  terminal
- `("u700", -1)`, electrode wired to the U− terminal

**The polarity factor controls the conversion from computed electrode voltage to
the actual supply set-point:** `v_set = v_electrode × polarity_factor`.

- **Bipolar iseg HV modules** natively accept any signed set-point, always use
  `+1`.
- **Unipolar 0MPV LV modules** require `v_set ≥ 0`. Use `+1` for electrodes that
  need positive voltage (wired to U+), `-1` for electrodes that need negative
  voltage (wired to U−). This is what lets the same multipole math drive a mix of
  module types without the physics code needing to know which is which.

**Rod numbering convention:** rod 0 is the first rod encountered going
counter-clockwise from 12 o'clock, viewed from the beam-entry face, matching the
DANFYSIK drawings.

**Bussed wiring:** if two physical rods share one MPOD output channel, map both
indices to the same channel entry. The push loop writes the same value to both.
This is idempotent and harmless. Do *not* apply different per-electrode corrections
to physically-bussed rods.

To override the built-in map, edit the `DEFAULT_CHANNEL_MAP` dictionary directly in
`lstar_mpod_ctl.py`, or supply a JSON file via `--map path/to/channel_map.json`.

---

## 5. GUI usage

```bash
python src/lstar_gui.py                    # connect to the default host
python src/lstar_gui.py --host 10.0.0.5     # custom MPOD IP (can also be set in the GUI itself)
python src/lstar_gui.py --dry-run           # never writes to hardware (can be set in GUI)
```

The main window shows a beamline diagram (`BeamlineDiagram`) with every LSTAR
element positioned approximately to scale along the actual beam path. Elements
with channel maps wired up are clickable and open a configuration panel, and elements
without wiring (or the B1/B2 dipoles, which aren't on the MPOD crate at all) are
shown for orientation only, being non-interactive.

Clicking a configured element opens the *Push Dialog*, where you set amplitudes
per component, review the computed per-electrode voltage table, and either dry-run
or push. The host/community fields at the top of the window are shared across the
whole session.

All pushes, zeroes, and switch actions through the GUI are recorded in
`lstar_changelog.log` next to the script, the same as CLI actions.

If `puresnmp` isn't installed or `lstar_mpod_ctl.py` can't be found, the GUI still
launches, falling back to internal stub functions so the diagram and dry-run
mode work standalone. This is the easiest way to look at the tool without any lab
network access.

---

## 6. Troubleshooting

**Crate doesn't respond at all.**
Run `python scripts/sysDescr.py` and enter the host IP. This is the minimal
possible SNMP request (`sysDescr`, OID `1.3.6.1.2.1.1.1.0`) and confirms whether
anything is listening at all before assuming a code-level problem.

**Crate responds to reads but writes fail.**
Run `python scripts/mpod_diagnose.py --host <ip>` checks `sysMainSwitch` and
`sysStatus` and enumerates detected modules. If the crate's main switch is off,
nothing will accept a write regardless of community string correctness. The
`--fix-main-switch` flag is available once you've confirmed that's the actual
issue.

**Write fails with `wrongValue` (SNMP error 10).**
This is almost always the unipolar/polarity issue described in §4, when a negative
set-point sent to a 0MPV LV module. Check `_check_voltage_signs` output, which
should have caught this in preflight. If it didn't, the channel map's polarity
factor for that electrode is probably wrong.

**Wrong community string.**
Default read community is `public`, write is `guru`. These are WIENER/iseg
factory defaults, not necessarily project-specific. If the crate's communities have been
changed from defaults, pass `--read-community`/`--write-community` explicitly.

**Want to test one channel manually before trusting a full multipole push.**
`python scripts/mpod_write_test.py --host <ip> --channel u700 --voltage 5.0`
sets one channel, reads it back, and is independent of the channel-map/multipole
logic entirely. This is good for isolating "is it the hardware/wiring" vs. "is it the
multipole math."

---

## 7. Standalone scripts (`scripts/`)

These predate the consolidated CLI and remain useful as lower-level, independent
tools. Each does one thing with no dependency on `lstar_mpod_ctl.py`:

| Script | Use it when... |
|---|---|
| `sysDescr.py` | You want the absolute minimum "is anything there" SNMP check |
| `wiener_crate_walk.py` | You want to enumerate the entire SNMP subtree the crate exposes, like after a firmware update |
| `mpod_probe.py` | You want a read-only channel table without the full CLI's element/physics layer |
| `mpod_diagnose.py` | The crate looks unresponsive or the main switch state is unclear |
| `mpod_write_test.py` | You want to test one channel in isolation before trusting a multipole push |

---

## 8. Maintenance notes

- **Adding hardware:** when an element's electrodes get physically wired up, fill
  in its entry in `DEFAULT_CHANNEL_MAP` (see §3). No other code changes are
  needed for that element to become fully operational via both CLI and GUI.
- **MIB changes:** OID bases are derived from `docs/WIENER-CRATE-MIB.txt`. If the
  crate firmware/MIB revision changes, recheck the OID constants
  (`OID_NAME`, `OID_VOLT_SET`, etc.) near the top of `lstar_mpod_ctl.py` against
  the new MIB revision number.
- **Changelog:** `lstar_changelog.log` is append-only and records every
  hardware-affecting action with a timestamp, OS username, and before/after state.
  It is intentionally never auto-rotated or cleared. Treat it as a logbook,
  not a debug log.
