"""
lstar_gui.py  -  LSTAR Multipole Control GUI
=============================================
Tkinter front-end for the LSTAR squirrel-cage multipole elements
at the TAMU Cyclotron Institute.

Requires:
  lstar_mpod_ctl.py  (same directory)
  libraries listed in requirements.txt

Usage
-----
  python lstar_gui.py                   # open to default IP
  python lstar_gui.py --host 10.0.0.5   # custom MPOD IP (can be configured in GUI as well)
  python lstar_gui.py --dry-run         # don't write to hardware (can be configured in GUI as well)

Swapping the beamline diagram
-----------------------------
  The diagram is drawn programmatically in BeamlineDiagram.draw_programmatic().
  To replace it with a real image (e.g. exported from the CAD drawings):
    1. Replace the draw_programmatic() body with image-loading code:
         from PIL import Image, ImageTk
         img = Image.open("lstar_layout.png").resize((CANVAS_W, CANVAS_H))
         self._photo = ImageTk.PhotoImage(img)
         self.create_image(0, 0, image=self._photo, anchor='nw', tags='bg')
    2. Update ELEMENT_LAYOUT (cx, cy, radius) to match element centers in
       your image.
    3. The hit-box layer (_draw_hitboxes) is drawn on top and unchanged, so
       clickable regions work with any background.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import getpass
import math
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# ─── Core logic import ────────────────────────────────────────────────────────
# Tries to import from lstar_mpod_ctl.py (must be in the same directory).
# Falls back to stubs so the GUI loads and the diagram works even without
# the CLI file or puresnmp installed.
# ─────────────────────────────────────────────────────────────────────────────
_SNMP_OK = False
try:
    from lstar_mpod_ctl import (
        compute_voltages, check_amplitudes,
        LSTAR_ELEMENTS, MULTIPOLE_ORDERS, DEFAULT_CHANNEL_MAP,
        load_channel_map, HARD_LIMIT_V,
        encode_float, encode_int, oid_for,
        OID_VOLT_SET, OID_VOLT_MEAS, OID_SWITCH,
        SWITCH_LABELS, SWITCH_ON, SWITCH_OFF,
        decode_float, decode_int, decode_str, decode_status,
        resolve_ch, _check_voltage_signs,
        walk_oid, _WALK, suffix_to_label,
    )
    try:
        from lstar_mpod_ctl import _WRITE_PAUSE
    except ImportError:
        _WRITE_PAUSE = 0.05
    from puresnmp import V2C, Client as _SNMPClient   # noqa: F401
    _SNMP_OK = True
except ImportError as _exc:
    print(f"[GUI] Note: {str(_exc)[:100]}")
    print("[GUI] SNMP unavailable, dry-run / diagram-only mode.")

    _WRITE_PAUSE  = 0.05
    HARD_LIMIT_V  = 500.0
    SWITCH_ON  = 1
    SWITCH_OFF = 0
    SWITCH_LABELS = {0: "Off", 1: "On"}
    MULTIPOLE_ORDERS = {'Q': 2, 'H': 3, 'O': 4, 'De': 5, 'Do': 6}
    LSTAR_ELEMENTS = {
        'Q1': {
            'n_rods': 4, 'components': ['Q'],
            'max_amplitude': {'Q': 2000}, 'hard_limit': 2200,
            'description': '4-rod pure quadrupole Q1',
        },
        'Q2': {
            'n_rods': 4, 'components': ['Q'],
            'max_amplitude': {'Q': 2000}, 'hard_limit': 2200,
            'description': '4-rod pure quadrupole Q2',
        },
        '(Q+oct)1': {
            'n_rods': 24, 'components': ['Q', 'O'],
            'max_amplitude': {'Q': 1600, 'O': 200}, 'hard_limit': 1800,
            'description': '24-rod quad+octupole (Q+Oct)1',
        },
        'S1': {
            'n_rods': 6, 'components': ['H'],
            'max_amplitude': {'H': 400}, 'hard_limit': 500,
            'description': '6-rod pure hexapole S1',
        },
        'M': {
            'n_rods': 24, 'components': ['Q', 'H', 'O', 'De', 'Do'],
            'max_amplitude': {'Q': 100, 'H': 300, 'O': 300, 'De': 100, 'Do': 100},
            'hard_limit': 500,
            'description': '24-rod central multipole M',
        },
        '(Q+oct)2': {
            'n_rods': 24, 'components': ['Q', 'O'],
            'max_amplitude': {'Q': 1600, 'O': 200}, 'hard_limit': 1800,
            'description': '24-rod quad+octupole (Q+Oct)2',
        },
        'S2': {
            'n_rods': 6, 'components': ['H'],
            'max_amplitude': {'H': 400}, 'hard_limit': 500,
            'description': '6-rod pure hexapole S2',
        },
        'Q3': {
            'n_rods': 4, 'components': ['Q'],
            'max_amplitude': {'Q': 2000}, 'hard_limit': 2200,
            'description': '4-rod pure quadrupole Q3',
        },
        'Q4': {
            'n_rods': 4, 'components': ['Q'],
            'max_amplitude': {'Q': 2000}, 'hard_limit': 2200,
            'description': '4-rod pure quadrupole Q4',
        },
    }
    DEFAULT_CHANNEL_MAP: dict = {
        'Q1': {}, 'Q2': {}, '(Q+oct)1': {}, 'S1': {},
        'M':  {k: (f'u{500+k}' if k < 8 else
                   f'u{600+k-8}' if k < 16 else
                   f'u{700+k-16}', +1) for k in range(24)},
        '(Q+oct)2': {}, 'S2': {}, 'Q3': {}, 'Q4': {},
    }

    def resolve_ch(entry):
        """parse (channel, polarity) from a channel-map entry."""
        if isinstance(entry, str):
            return entry, +1
        try:
            ch, pol = entry
            return ch, (pol if pol in (+1, -1) else +1)
        except Exception:
            return str(entry), +1

    def _check_voltage_signs(voltages, ch_map):
        """return list of (k, ch, pol, v_el, v_set) for sign mismatches."""
        issues = []
        for k, v in enumerate(voltages):
            raw = ch_map.get(k)
            if raw is None:
                continue
            ch, pol = resolve_ch(raw)
            v_set = float(v) * pol
            if v_set < -1e-6:
                issues.append((k, ch, pol, float(v), v_set))
        return issues

    def _tri(k: int, n: int, N: int) -> float:
        t = (n * k / N) % 1.0
        return 2.0 * abs(2.0 * t - 1.0) - 1.0

    def compute_voltages(element: str, amplitudes: dict) -> np.ndarray:
        info = LSTAR_ELEMENTS[element]
        N = info['n_rods']
        v = np.zeros(N)
        for comp, amp in amplitudes.items():
            n = MULTIPOLE_ORDERS[comp]
            v += amp * np.array([_tri(k, n, N) for k in range(N)])
        return v

    def check_amplitudes(element: str, amplitudes: dict, force: bool = False) -> bool:
        lims = LSTAR_ELEMENTS[element]['max_amplitude']
        return all(abs(a) <= lims.get(c, 1e9) for c, a in amplitudes.items())

    def load_channel_map(path):
        return DEFAULT_CHANNEL_MAP

    def decode_float(raw):
        try: return float(raw)
        except: return None   # noqa: E722

    def decode_int(raw):
        try: return int(raw)
        except: return None   # noqa: E722

    def encode_float(v): return v   # not called without SNMP
    def encode_int(v):   return v   # not called without SNMP
    def oid_for(b, c):   return f"{b}.{c}"
    OID_VOLT_SET = OID_VOLT_MEAS = OID_SWITCH = "0.0.0"

    def decode_str(raw):
        return str(raw)

    def decode_status(raw):
        return "?"

    def suffix_to_label(idx):
        n = idx - 1
        return f"u{n}", n // 100, n % 100

    async def walk_oid(client, oid):
        return {}

    _WALK = {k: None for k in
             ("name", "status", "volt_meas", "curr_meas",
              "switch", "volt_set", "curr_set")}


# ─── Changelog ────────────────────────────────────────────────────────────────
# Persistent, append-only record of every hardware-affecting action taken from
# this GUI (push, zero, switch), Travels with the rest of the
# LSTAR control tools and is visible to anyone with access to
# this directory.
CHANGELOG_PATH = Path(__file__).resolve().parent / "lstar_changelog.log"


# ══════════════════════════════════════════════════════════════════════════════
#  §1  Beamline element layout
#  ─────────────────────────────────────────────────────────────────────────────
#  These constants define element positions on the canvas.
#  If swapping the diagram for a real image, update cx/cy/radius values
#  to match the actual element positions in the image.
# ══════════════════════════════════════════════════════════════════════════════

CANVAS_W, CANVAS_H = 520, 500

# Tuple layout:
#   (display_label, element_key, cx, cy, radius, fill_color, tooltip_text)
#
#   element_key = None,         shown on diagram but not interactive
#   element_key in LSTAR_ELEMENTS, click to configure via this GUI
ELEMENT_LAYOUT: list[tuple] = [
    # ─────────────────────────────────────────────────────────────────────────
    # element_key = None,          non-interactive (drawn for reference only)
    # element_key in LSTAR_ELEMENTS, clickable, opens config panel
    #
    # To reposition elements when swapping to a real diagram image:
    #   change cx, cy, radius to match the new image coordinates.
    # ─────────────────────────────────────────────────────────────────────────

    # ── B1, B2: magnetic dipoles are a separate current supply, not MPOD ───
    ("B1", None, 139, 334, 20, "#888899",
     "Dipole B1  |  62.5\u00b0 bend  |  \u03c1 = 500 mm\n"
     "Bmax = 0.52 T  |  80 mm full vertical gap\n"
     "Controlled via current supply \u2014 not in MPOD."),
    ("B2", None, 186, 428, 20, "#888899",
     "Dipole B2  |  62.5\u00b0 bend  |  \u03c1 = 500 mm\n"
     "Bmax = 0.52 T  |  80 mm full vertical gap\n"
     "Controlled via current supply \u2014 not in MPOD."),

    # ── Quadrupoles: 4-rod pure quad───────────────────────────
    ("Q1", "Q1", 308, 100, 16, "#5B8DB8",
     "Quad Q1  |  4 rods  |  30 mm aperture  |  200 mm EFL\n"
     "Pure quadrupole  |  max \u00b12000 V\n\u25ba Click to configure"),
    ("Q2", "Q2", 261, 165, 16, "#5B8DB8",
     "Quad Q2  |  4 rods  |  50 mm aperture  |  200 mm EFL\n"
     "Pure quadrupole  |  max \u00b12000 V\n\u25ba Click to configure"),
    ("Q3", "Q3", 324, 428, 16, "#5B8DB8",
     "Quad Q3  |  4 rods  |  50 mm aperture  |  200 mm EFL\n"
     "Pure quadrupole  |  max \u00b12000 V\n\u25ba Click to configure"),
    ("Q4", "Q4", 366, 428, 16, "#5B8DB8",
     "Quad Q4  |  4 rods  |  30 mm aperture  |  200 mm EFL\n"
     "Pure quadrupole  |  max \u00b12000 V\n\u25ba Click to configure"),

    # ── Hexapoles: 6-rod pure hexapole───────────────────────────
    ("S1", "S1", 214, 230, 16, "#5BAD7A",
     "Hexapole S1  |  6 rods  |  50 mm aperture  |  120 mm EFL\n"
     "Pure hexapole (sextupole)  |  max \u00b1400 V\n\u25ba Click to configure"),
    ("S2", "S2", 279, 428, 16, "#5BAD7A",
     "Hexapole S2  |  6 rods  |  50 mm aperture  |  120 mm EFL\n"
     "Pure hexapole (sextupole)  |  max \u00b1400 V\n\u25ba Click to configure"),

    # ── Squirrel-cage multipoles ─────────────────────────────────────────────
    ("(Q+oct)1", "(Q+oct)1", 167, 295, 21, "#E8922A",
     "(Q+oct)\u2081  \u2014  Quad + Octupole\n"
     "24 rods  |  60 mm aperture  |  240 mm EFL\n"
     "Q: \u00b11600 V   O: \u00b1200 V\n\u25ba Click to configure"),
    ("M",        "M",        162.5, 381, 28, "#C84B31",
     "Central Multipole M\n"
     "24 rods  |  160 mm aperture  |  300 mm EFL\n"
     "Q / H / O / De / Do  (squirrel-cage)\n\u25ba Click to configure"),
    ("(Q+oct)2", "(Q+oct)2", 231, 428, 21, "#E8922A",
     "(Q+oct)\u2082  \u2014  Quad + Octupole\n"
     "24 rods  |  60 mm aperture  |  240 mm EFL\n"
     "Q: \u00b11600 V   O: \u00b1200 V\n\u25ba Click to configure"),
]

# Beam path waypoints used only by draw_programmatic().
# Matches the approximate topology of Figure 1 (top-view, Cave 5).
BEAM_PATH = [
    (355, 35),   # Object/Start
    (308, 100),  # through Q1
    (261, 165),  # through Q2
    (214, 230),  # through S1          
    (167, 295),  # through (Q+oct)1
    (139, 334),  # through B1  (62.5° bend)
    (162.5, 381),  # through M
    (186, 428),  # through B2  (62.5° bend)
    (231, 428),  # through (Q+oct)2
    (279, 428),  # through S2
    (324, 428),  # through Q3
    (366, 428),  # through Q4
    (406, 428),  # Image / Focal plane
]

# ─── Palette & fonts ─────────────────────────────────────────────────────────
_C_FG     = "#E8E4DA"
_C_ACCENT = "#E88C2A"
_C_GREEN  = "#4CAF50"
_C_RED    = "#E53935"
_C_AMBER  = "#FFC107"
_C_DIM    = "#666677"
_F_MONO   = ("Courier", 9)
_F_BODY   = ("Helvetica", 10)
_F_BOLD   = ("Helvetica", 10, "bold")
_F_SMALL  = ("Helvetica", 8)
_F_TITLE  = ("Helvetica", 12, "bold")


# ══════════════════════════════════════════════════════════════════════════════
#  §2  Tooltip
# ══════════════════════════════════════════════════════════════════════════════

class _Tooltip:
    """Hover tooltip for Canvas items."""

    def __init__(self, root: tk.Widget):
        self._root = root
        self._win:  Optional[tk.Toplevel] = None
        self._job:  Optional[str]          = None

    def show(self, text: str, rx: int, ry: int) -> None:
        self.hide()
        def _create():
            self._win = w = tk.Toplevel(self._root)
            w.wm_overrideredirect(True)
            w.wm_geometry(f"+{rx + 16}+{ry + 8}")
            tk.Label(w, text=text, justify="left", relief="solid",
                     borderwidth=1, bg="#FAFAD2", fg="#111111",
                     font=("Helvetica", 8), padx=5, pady=4).pack()
        self._job = self._root.after(450, _create)

    def hide(self) -> None:
        if self._job:
            self._root.after_cancel(self._job)
            self._job = None
        if self._win:
            self._win.destroy()
            self._win = None


# ══════════════════════════════════════════════════════════════════════════════
#  §3  Beamline diagram Canvas
# ══════════════════════════════════════════════════════════════════════════════

class BeamlineDiagram(tk.Canvas):
    """
    Schematic LSTAR beamline drawn on a Tk Canvas.

    Diagram swap
    ============
    Replace draw_programmatic() with image-loading code. The hit-box
    layer (_draw_hitboxes) is always drawn on top, remaining unchanged.

    Example (PIL):
        from PIL import Image, ImageTk
        img = Image.open("lstar_cave5_top.png").resize((CANVAS_W, CANVAS_H))
        self._photo = ImageTk.PhotoImage(img)
        self.create_image(0, 0, image=self._photo, anchor='nw', tags='bg')
    """

    def __init__(self, parent: tk.Widget,
                 on_select: Callable[[str], None], **kw):
        super().__init__(parent, bg="#0E0E18", highlightthickness=0, **kw)
        self._on_select      = on_select
        self._tooltip        = _Tooltip(self)
        self._bodies:   dict[str, int]  = {}
        self._selected: Optional[str]   = None
        self._pending_redraw: Optional[str] = None

        # Defer first draw until widget has been laid out with a real size
        self.after(20, self._redraw)
        self.bind("<Configure>", self._on_configure)

    # ── Public ─────────────────────────────────────────────────────────────

    def set_selected(self, key: Optional[str]) -> None:
        """Highlight/deselect an element."""
        if self._selected and self._selected in self._bodies:
            self.itemconfig(self._bodies[self._selected],
                            outline="white", width=2)
        self._selected = key
        if key and key in self._bodies:
            self.itemconfig(self._bodies[key], outline=_C_ACCENT, width=3)

    # ── Resize support ──────────────────────────────────────────────────────

    def _scale(self) -> tuple[float, float]:
        """Return (sx, sy) scale factors from reference CANVAS_W × CANVAS_H."""
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return 1.0, 1.0
        return w / CANVAS_W, h / CANVAS_H

    def _on_configure(self, event: tk.Event) -> None:
        """Debounced redraw on resize."""
        if self._pending_redraw:
            self.after_cancel(self._pending_redraw)
        self._pending_redraw = self.after(80, self._redraw)

    def _redraw(self) -> None:
        """Full redraw, restoring selection state."""
        self._pending_redraw = None
        saved = self._selected
        self.delete("all")
        self._bodies.clear()
        self._selected = None
        self.draw_programmatic()
        self._draw_hitboxes()
        if saved and saved in self._bodies:
            self._selected = saved
            self.itemconfig(self._bodies[saved], outline=_C_ACCENT, width=3)

    # ── Programmatic drawing ────────────────────────────────────────────────

    def draw_programmatic(self) -> None:
        """
        Render the beamline schematic directly on the canvas.

        REPLACE THIS METHOD to use a real image.  Steps:
          1. Delete this body.
          2. Load image: self._photo = ImageTk.PhotoImage(...)
          3. Place it:   self.create_image(0, 0, image=self._photo,
                                           anchor='nw', tags='bg')
          4. Update ELEMENT_LAYOUT coordinates.
          5. _draw_hitboxes() will automatically overlay the hit regions.
        """
        self.delete("bg")
        scx, scy = self._scale()
        sm = min(scx, scy)          # uniform factor for radii and fonts

        def X(v): return v * scx    # scale canvas x-coordinate
        def Y(v): return v * scy    # scale canvas y-coordinate
        def FS(s): return max(6, int(s * sm))  # scale font size

        W, H = X(CANVAS_W), Y(CANVAS_H)

        # dark bg
        self.create_rectangle(0, 0, W, H,
                              fill="#0E0E18", outline="", tags="bg")
        self.create_text(W / 2, Y(11),
                         text="LSTAR Beamline - TAMUTRAP, Cyclotron Institute",
                         fill="#3A3A55", font=("Helvetica", FS(8)), tags="bg")
        self.create_text(X(12), H - Y(10), text="CAVE 5",
                         anchor="sw", fill="#1C1C34",
                         font=("Helvetica", FS(20), "bold"), tags="bg")

        # Beam path: shadow + line
        pts = [c for pt in BEAM_PATH for c in (X(pt[0]), Y(pt[1]))]
        self.create_line(*pts, fill="#1A1A3A", width=max(1, int(9 * sm)),
                         smooth=True, tags="bg")
        self.create_line(*pts, fill="#3A5A99", width=max(1, int(2 * sm)),
                         smooth=True, tags="bg")

        # Arrowhead at Image/Focal plane
        (rx1, ry1), (rx2, ry2) = BEAM_PATH[-2], BEAM_PATH[-1]
        ax1, ay1 = X(rx1), Y(ry1)
        ax2, ay2 = X(rx2), Y(ry2)
        dx, dy = ax2 - ax1, ay2 - ay1
        L = math.hypot(dx, dy) or 1
        ux, uy = dx / L, dy / L
        px, py = -uy, ux
        arr = int(11 * sm)
        self.create_polygon(
            ax2, ay2,
            ax2 - arr*ux + 5*px, ay2 - arr*uy + 5*py,
            ax2 - arr*ux - 5*px, ay2 - arr*uy - 5*py,
            fill="#3A5A99", outline="", tags="bg",
        )

        # Object / Start marker
        osx, osy = X(BEAM_PATH[0][0]), Y(BEAM_PATH[0][1])
        r5 = max(3, int(5 * sm))
        self.create_oval(osx - r5, osy - r5, osx + r5, osy + r5,
                         fill=_C_ACCENT, outline="", tags="bg")
        self.create_text(osx + X(9), osy - Y(9), text="Object / Start",
                         anchor="sw", fill=_C_ACCENT,
                         font=("Helvetica", FS(8)), tags="bg")

        # Image / Focal plane line + label
        oex, oey = X(BEAM_PATH[-1][0]), Y(BEAM_PATH[-1][1])
        self.create_line(oex, oey - Y(20), oex, oey + Y(20),
                         fill="#7777AA", width=max(1, int(2 * sm)),
                         dash=(4, 3), tags="bg")
        self.create_text(oex + X(5), oey + Y(24), text="Image /\nFocal plane",
                         anchor="n", fill="#7777AA",
                         font=("Helvetica", FS(8)), tags="bg")

        # Legend
        legend = [
            ("#5B8DB8", "Quad (Q1\u2013Q4)  \u25ba clickable"),
            ("#5BAD7A", "Hexapole (S1, S2)  \u25ba clickable"),
            ("#888899", "Dipole (B1, B2)  \u25ba current supply"),
            ("#E8922A", "(Q+oct)  \u25ba clickable"),
            ("#C84B31", "Multipole M  \u25ba clickable"),
        ]
        lx, ly = X(8), Y(25)
        step = Y(14)
        self.create_text(lx, ly, text="Legend", anchor="w",
                         fill="#555566", font=("Helvetica", FS(7), "bold"),
                         tags="bg")
        ly += step
        for color, label in legend:
            r4 = max(2, int(4 * sm))
            r8 = max(4, int(8 * sm))
            self.create_oval(lx, ly - r4, lx + r8, ly + r4,
                             fill=color, outline="#444455", width=1,
                             tags="bg")
            self.create_text(lx + X(13), ly, text=label, anchor="w",
                             fill="#888899", font=("Helvetica", FS(7)),
                             tags="bg")
            ly += step

    # ── Hitbox layer ───────────────────────────────────────────────────────

    def _draw_hitboxes(self) -> None:
        """
        Place clickable/hoverable circles on top of the background.
        Called once after draw_programmatic() and works with any background.
        Positions are scaled from the reference ELEMENT_LAYOUT coordinates.
        """
        self.delete("elem")
        self._bodies.clear()
        scx, scy = self._scale()
        sm = min(scx, scy)

        for (label, key, cx, cy, r, color, tip) in ELEMENT_LAYOUT:
            cx = cx * scx
            cy = cy * scy
            r  = r  * sm
            tag = f"el_{key or label}"

            # Body oval
            body_id = self.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                fill=color,
                outline="white" if key else "#444455",
                width=max(1, int(2 * sm)) if key else 1,
                tags=("elem", tag),
            )
            # Text label
            fs = max(6, int((7 if len(label) > 6 else 8) * sm))
            self.create_text(cx, cy, text=label, fill="white",
                             font=("Helvetica", fs, "bold"),
                             tags=("elem", tag))

            if key:
                self._bodies[key] = body_id
                self.tag_bind(tag, "<Enter>",
                              lambda e, k=key, t=tip: self._on_enter(e, k, t))
                self.tag_bind(tag, "<Leave>",
                              lambda e, k=key: self._on_leave(k))
                self.tag_bind(tag, "<Button-1>",
                              lambda e, k=key: self._on_click(k))
                self.tag_bind(tag, "<Enter>",
                              lambda e: self.config(cursor="hand2"), add=True)
                self.tag_bind(tag, "<Leave>",
                              lambda e: self.config(cursor=""), add=True)

    # ── Event handlers ──────────────────────────────────────────────────────

    def _on_enter(self, event: tk.Event, key: str, tip: str) -> None:
        self._tooltip.show(tip, event.x_root, event.y_root)
        if key != self._selected:
            self.itemconfig(self._bodies[key], outline=_C_ACCENT, width=2)

    def _on_leave(self, key: str) -> None:
        self._tooltip.hide()
        if key != self._selected:
            self.itemconfig(self._bodies[key], outline="white", width=2)

    def _on_click(self, key: str) -> None:
        self.set_selected(key)
        self._on_select(key)


# ══════════════════════════════════════════════════════════════════════════════
#  §4  Amplitude input row
# ══════════════════════════════════════════════════════════════════════════════

class _AmpRow(ttk.Frame):
    """Single row: component name | order | Entry | V | spec limit | status."""

    _ORDER_NAMES = {2: "quadrupole", 3: "hexapole", 4: "octupole",
                    5: "decapole",   6: "dodecapole"}

    def __init__(self, parent: tk.Widget, comp: str, limit: float,
                 on_change: Callable, **kw):
        super().__init__(parent, **kw)
        self._comp     = comp
        self._limit    = limit
        self._var      = tk.StringVar(value="0")
        self._stat_var = tk.StringVar(value="")

        order      = MULTIPOLE_ORDERS.get(comp, '?')
        order_name = self._ORDER_NAMES.get(order, f"order {order}")

        ttk.Label(self, text=f"{comp:>3}", width=4,
                  font=_F_BOLD).pack(side="left")
        ttk.Label(self, text=f"({order_name})", width=13,
                  font=_F_SMALL, foreground=_C_DIM).pack(side="left")
        ttk.Entry(self, textvariable=self._var, width=10,
                  justify="right").pack(side="left", padx=(2, 2))
        ttk.Label(self, text="V", font=_F_BODY).pack(side="left")
        ttk.Label(self, text=f"   lim \u00b1{limit} V", width=13,
                  font=_F_SMALL, foreground=_C_DIM).pack(side="left")
        self._stat_lbl = ttk.Label(self, textvariable=self._stat_var,
                                   width=10, font=_F_SMALL)
        self._stat_lbl.pack(side="left")

        # Fire on every keystroke, enabling live voltage table updates
        self._var.trace_add("write", lambda *_: on_change())

    def value(self) -> Optional[float]:
        try:
            return float(self._var.get())
        except ValueError:
            return None

    def set_status(self, within_spec: bool, within_hard: bool) -> None:
        if not within_hard:
            self._stat_var.set("\u26a0 >HardLim")
            self._stat_lbl.configure(foreground=_C_RED)
        elif not within_spec:
            self._stat_var.set("\u26a0 >SpecLim")
            self._stat_lbl.configure(foreground=_C_AMBER)
        else:
            self._stat_var.set("\u2713 OK")
            self._stat_lbl.configure(foreground=_C_GREEN)

    def clear_status(self) -> None:
        self._stat_var.set("")


# ══════════════════════════════════════════════════════════════════════════════
#  §5  Voltage table
# ══════════════════════════════════════════════════════════════════════════════

class VoltageTable(ttk.Frame):
    """
    Scrollable table:  Sw | El | Channel | Computed (V) | ΔCorr (V) | Final (V)

    - Sw column: per-row ON/OFF toggle button.  Shows '?' until readback updates
      the state, clicking issues a switch command via _sw_cb(electrode, new_state).
    - ΔCorr column is editable when corrections are enabled.
    - Corrections are preserved by update_computed(), and only populate() clears them.
    - Column alignment: All columns (header + data) use _F_MONO (Courier 9) and
      identical width= values so that character-cell widths agree exactly.
    """

    # Column indices for reference
    _COL_SW, _COL_EL, _COL_CH, _COL_CMP, _COL_COR, _COL_FIN = 0, 1, 2, 3, 4, 5

    # Header text and character widths must match populate() widget widths
    _HDRS   = ("Sw",  "El", "Channel", "Computed (V)", "\u0394Corr (V)", "Final (V)")
    _WIDTHS = ( 4,     4,    8,          12,              12,              12)

    def __init__(self, parent: tk.Widget, **kw):
        super().__init__(parent, **kw)
        self._rows:    list[dict]       = []
        self._entries: list[ttk.Entry]  = []   # ΔCorr entries for enable/disable
        self._sw_btns: list[tk.Button]  = []   # per-row switch buttons
        self._corr_on  = False
        self._sw_cb:   Optional[Callable[[int, int], None]] = None  # (electrode, state)
        self._build()

    def set_switch_callback(self, cb: Callable[[int, int], None]) -> None:
        """cb(electrode_index, new_state)  called when a per-row Sw button is clicked."""
        self._sw_cb = cb

    def _build(self) -> None:
        # ── Column headers use _F_MONO to match data rows exactly ──────────
        hf = ttk.Frame(self)
        hf.pack(fill="x")
        for txt, w in zip(self._HDRS, self._WIDTHS):
            ttk.Label(hf, text=txt, width=w, anchor="center",
                      font=_F_MONO).pack(side="left", padx=1)

        # ── Scrollable body ───────────────────────────────────────────────────
        self._cv = tk.Canvas(self, highlightthickness=0, bg="#161620")
        sb = ttk.Scrollbar(self, orient="vertical", command=self._cv.yview)
        self._inner = ttk.Frame(self._cv)
        self._inner.bind("<Configure>", lambda e:
                         self._cv.configure(scrollregion=self._cv.bbox("all")))
        self._cv.create_window((0, 0), window=self._inner, anchor="nw")
        self._cv.configure(yscrollcommand=sb.set)
        self._cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._cv.bind("<MouseWheel>",
                      lambda e: self._cv.yview_scroll(-(e.delta // 120), "units"))
        self._cv.bind("<Button-4>", lambda e: self._cv.yview_scroll(-1, "units"))
        self._cv.bind("<Button-5>", lambda e: self._cv.yview_scroll( 1, "units"))

    # ── Data methods ─────────────────────────────────────────────────────────

    def populate(self, voltages: np.ndarray, ch_map: dict) -> None:
        """Full rebuild.  Clears corrections and switch states.  On element change only."""
        for w in self._inner.winfo_children():
            w.destroy()
        self._rows.clear()
        self._entries.clear()
        self._sw_btns.clear()

        for k, v in enumerate(voltages):
            raw = ch_map.get(k)
            if raw is not None:
                ch_name, pol = resolve_ch(raw)
                pol_sym  = "+" if pol == +1 else "\u2212"
                ch_label = f"{ch_name} [{pol_sym}]"
                ch_color = "#8EADCC"
            else:
                ch_name, pol = "\u2014", +1
                ch_label = "\u2014"
                ch_color = _C_DIM

            corr_var = tk.StringVar(value="0")
            final_sv = tk.StringVar(value=f"{v:+.4f}")
            sw_var   = tk.StringVar(value="?")

            row = {'k': k, 'ch': ch_name, 'pol': pol, 'v': float(v),
                   'corr': corr_var, 'final': final_sv,
                   'sw': sw_var, 'sw_state': None}
            self._rows.append(row)

            rf = ttk.Frame(self._inner)
            rf.pack(fill="x")

            sw_btn = tk.Button(
                rf, textvariable=sw_var, width=self._WIDTHS[0],
                font=("Courier", 8), relief="flat", borderwidth=1,
                bg="#2A2A3A", fg="#888888", activebackground="#2A2A3A",
                command=lambda k=k: self._on_sw_click(k),
            )
            sw_btn.pack(side="left", padx=1)
            self._sw_btns.append(sw_btn)

            ttk.Label(rf, text=f"{k:2d}", width=self._WIDTHS[1],
                      anchor="center", font=_F_MONO).pack(side="left", padx=1)
            ttk.Label(rf, text=ch_label, width=self._WIDTHS[2], anchor="center",
                      font=_F_MONO, foreground=ch_color,
                      ).pack(side="left", padx=1)
            ttk.Label(rf, text=f"{v:+.4f}", width=self._WIDTHS[3],
                      anchor="e", font=_F_MONO).pack(side="left", padx=1)

            ent = ttk.Entry(rf, textvariable=corr_var,
                            width=self._WIDTHS[4], justify="right",
                            font=_F_MONO,
                            state="normal" if self._corr_on else "readonly")
            ent.pack(side="left", padx=1)
            self._entries.append(ent)

            # Final (V) label turns amber when v_set = Final × polarity < 0
            fin_lbl = ttk.Label(rf, textvariable=final_sv,
                                width=self._WIDTHS[5], anchor="e", font=_F_MONO)
            fin_lbl.pack(side="left", padx=1)

            def _upd(*_, row=row, fin_lbl=fin_lbl):
                try:
                    fin   = row['v'] + float(row['corr'].get())
                    row['final'].set(f"{fin:+.4f}")
                    v_set = fin * row['pol']
                    fin_lbl.configure(
                        foreground=_C_RED if v_set < -1e-6 else _C_GREEN)
                except ValueError:
                    row['final'].set("?")
                    fin_lbl.configure(foreground=_C_GREEN)

            row['_upd'] = _upd
            corr_var.trace_add("write", _upd)
            _upd()   # set initial colour

    def _on_sw_click(self, k: int) -> None:
        """Toggle switch state for electrode k."""
        if k >= len(self._rows):
            return
        row = self._rows[k]
        ch  = row['ch']
        if ch == "\u2014":
            return   # unmapped, nothing to switch
        # Toggle: if currently On then Off, otherwise On
        cur = row['sw_state']
        new = SWITCH_OFF if cur == SWITCH_ON else SWITCH_ON
        if self._sw_cb:
            self._sw_cb(k, new)

    def update_computed(self, voltages: np.ndarray) -> None:
        """Refresh computed column only. Preserves corrections and switch states."""
        for i, v in enumerate(voltages):
            if i >= len(self._rows):
                break
            row = self._rows[i]
            row['v'] = float(v)
            if '_upd' in row:
                row['_upd']()   # updates Final value AND sign-mismatch colour
            else:
                try:
                    row['final'].set(
                        f"{row['v'] + float(row['corr'].get()):+.4f}")
                except ValueError:
                    row['final'].set("?")

    def set_switch_state(self, k: int, state: int) -> None:
        """Update the Sw button for electrode k (0=Off, 1=On, -1=unknown)."""
        if k >= len(self._rows):
            return
        row = self._rows[k]
        row['sw_state'] = state
        btn = self._sw_btns[k]
        if state == SWITCH_ON:
            row['sw'].set("ON")
            btn.configure(bg="#1A4A1A", fg=_C_GREEN, activebackground="#1A4A1A")
        elif state == SWITCH_OFF:
            row['sw'].set("OFF")
            btn.configure(bg="#3A1A1A", fg="#CC4444", activebackground="#3A1A1A")
        else:
            row['sw'].set("?")
            btn.configure(bg="#2A2A3A", fg="#888888", activebackground="#2A2A3A")

    def clear_switch_states(self) -> None:
        for i in range(len(self._rows)):
            self.set_switch_state(i, -1)

    def enable_corrections(self, enabled: bool) -> None:
        self._corr_on = enabled
        state = "normal" if enabled else "readonly"
        for ent in self._entries:
            ent.configure(state=state)

    def reset_corrections(self) -> None:
        for row in self._rows:
            row['corr'].set("0")

    def get_final_voltages(self) -> list[float]:
        result = []
        for row in self._rows:
            try:
                result.append(row['v'] + float(row['corr'].get()))
            except ValueError:
                result.append(row['v'])
        return result

    def get_nonzero_corrections(self) -> dict[int, float]:
        out = {}
        for row in self._rows:
            try:
                c = float(row['corr'].get())
                if abs(c) > 1e-9:
                    out[row['k']] = c
            except ValueError:
                pass
        return out

    def is_empty(self) -> bool:
        return len(self._rows) == 0


# ══════════════════════════════════════════════════════════════════════════════
#  §6  Control panel (right side)
# ══════════════════════════════════════════════════════════════════════════════

class ControlPanel(ttk.Frame):
    """Amplitude inputs, voltage table, and action buttons for an element."""

    def __init__(self, parent: tk.Widget,
                 log_fn: Callable[[str], None], **kw):
        super().__init__(parent, **kw)
        self._log       = log_fn
        self._element:   Optional[str]       = None
        self._ch_map:    dict                = {}
        self._voltages:  Optional[np.ndarray] = None
        self._amp_rows:  dict[str, _AmpRow]  = {}
        self._action_cb: Optional[Callable]  = None   # injected
        self._build_placeholder()

    # ── Public API ──────────────────────────────────────────────────────────

    def set_action_callback(self, cb: Callable) -> None:
        """
        cb(action: str, payload=None)
          action in {'push', 'readback', 'zero'}
          payload = (element, final_voltages, ch_map) for 'push', else None
        """
        self._action_cb = cb

    def load_element(self, key: str) -> None:
        self._element  = key
        self._ch_map   = load_channel_map(None).get(key, {})
        self._voltages = None
        self._build_element_ui()

    def get_push_payload(self) -> Optional[tuple]:
        if self._element is None or self._voltages is None:
            return None
        return (self._element,
                self._volt_table.get_final_voltages(),
                self._ch_map)

    def get_amplitudes(self) -> dict[str, float]:
        """Current numeric amplitude entries, {component: value}."""
        out = {}
        for comp, arow in self._amp_rows.items():
            v = arow.value()
            if v is not None:
                out[comp] = v
        return out

    def get_corrections(self) -> dict[int, float]:
        """Current nonzero per-electrode ΔCorr entries, {electrode: value}."""
        if not hasattr(self, '_volt_table'):
            return {}
        return self._volt_table.get_nonzero_corrections()

    # ── Placeholder ─────────────────────────────────────────────────────────

    def _build_placeholder(self) -> None:
        ttk.Label(
            self,
            text=(
                "\u2190 Click any element on the diagram\n"
                "to configure it.\n\n"
                "Clickable  (MPOD-controlled):\n"
                "  Q1   Q2   (Q+oct)1   S1\n"
                "  M\n"
                "  (Q+oct)2   S2   Q3   Q4\n\n"
                "B1, B2 are magnetic dipoles,\n"
                "separate current supply only."
            ),
            font=("Helvetica", 9, "italic"),
            foreground=_C_DIM,
            justify="center",
        ).pack(expand=True)

    # ── Element UI ──────────────────────────────────────────────────────────

    def _build_element_ui(self) -> None:
        for w in self.winfo_children():
            w.destroy()
        self._amp_rows.clear()

        key  = self._element
        info = LSTAR_ELEMENTS[key]
        n_mapped = sum(1 for ch in self._ch_map.values() if ch)
        n_total  = info['n_rods']

        # Title
        hf = ttk.Frame(self)
        hf.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(hf, text=key, font=_F_TITLE,
                  foreground=_C_ACCENT).pack(side="left")
        ttk.Label(hf, text=f"  {info['description']}",
                  font=_F_SMALL, foreground=_C_DIM).pack(side="left")
        map_color = _C_GREEN if n_mapped == n_total else _C_RED
        ttk.Label(hf, text=f"{n_mapped}/{n_total} ch mapped",
                  font=_F_SMALL, foreground=map_color).pack(side="right")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=4, pady=4)

        # Amplitude inputs
        ttk.Label(self, text="Multipole amplitudes:",
                  font=_F_BOLD).pack(anchor="w", padx=8)
        af = ttk.Frame(self)
        af.pack(fill="x", padx=8, pady=(2, 6))
        for comp in info['components']:
            limit = info['max_amplitude'][comp]
            row   = _AmpRow(af, comp, limit, on_change=self._on_amp_change)
            row.pack(fill="x", pady=1)
            self._amp_rows[comp] = row

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=4, pady=4)

        # Corrections toggle
        cf = ttk.Frame(self)
        cf.pack(fill="x", padx=8)
        self._corr_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            cf,
            text="Enable per-electrode corrections  (\u0394Corr column becomes editable)",
            variable=self._corr_var,
            command=self._toggle_corrections,
        ).pack(side="left")
        ttk.Button(cf, text="Reset \u0394", width=7,
                   command=self._reset_corrections).pack(side="right")

        # Voltage table
        ttk.Label(self, text="Electrode voltages:",
                  font=_F_BOLD).pack(anchor="w", padx=8, pady=(4, 0))
        self._volt_table = VoltageTable(self)
        self._volt_table.pack(fill="both", expand=True, padx=4, pady=(2, 0))
        self._volt_table.set_switch_callback(self._on_sw_toggle)
        # Initialise with zeros so table structure exists immediately
        self._volt_table.populate(np.zeros(n_total), self._ch_map)

        # Summary line
        self._summary_var = tk.StringVar(
            value="Enter amplitudes above to compute electrode voltages.")
        ttk.Label(self, textvariable=self._summary_var,
                  font=_F_SMALL, foreground=_C_DIM).pack(anchor="w", padx=8)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=4, pady=4)

        # Action buttons
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(bf, text="Readback", width=10,
                   command=lambda: self._dispatch("readback")).pack(
            side="left", padx=2)
        ttk.Button(bf, text="Zero All", width=8,
                   command=lambda: self._dispatch("zero")).pack(
            side="left", padx=2)

        # Switch group buttons
        sw_frame = ttk.Frame(bf)
        sw_frame.pack(side="left", padx=(8, 2))
        ttk.Label(sw_frame, text="All:", font=_F_SMALL,
                  foreground=_C_DIM).pack(side="left")
        tk.Button(sw_frame, text="ON", width=4, font=("Courier", 8, "bold"),
                  bg="#1A4A1A", fg=_C_GREEN, relief="raised", borderwidth=1,
                  command=lambda: self._dispatch("switch_on")).pack(
            side="left", padx=1)
        tk.Button(sw_frame, text="OFF", width=4, font=("Courier", 8, "bold"),
                  bg="#3A1A1A", fg="#CC4444", relief="raised", borderwidth=1,
                  command=lambda: self._dispatch("switch_off")).pack(
            side="left", padx=1)

        tk.Button(bf, text="\u25b6  PUSH TO MPOD",
                  bg=_C_RED, fg="white",
                  font=("Helvetica", 10, "bold"),
                  relief="raised", borderwidth=2,
                  command=lambda: self._dispatch("push"),
                  ).pack(side="right", padx=2, ipady=2)

    # ── Callbacks ───────────────────────────────────────────────────────────

    def _on_amp_change(self) -> None:
        if self._element is None:
            return
        info             = LSTAR_ELEMENTS[self._element]
        elem_hard_limit  = info.get('hard_limit', HARD_LIMIT_V)
        amps: dict[str, float] = {}

        for comp, arow in self._amp_rows.items():
            v = arow.value()
            if v is None:
                arow.clear_status()
                continue
            amps[comp] = v
            arow.set_status(
                within_spec=abs(v) <= info['max_amplitude'].get(comp, 1e9),
                within_hard=abs(v) <= elem_hard_limit,
            )

        try:
            self._voltages = compute_voltages(self._element, amps)
        except (ValueError, KeyError) as exc:
            self._summary_var.set(f"Compute error: {exc}")
            return

        # update_computed preserves user corrections, populate would clear them
        if not self._volt_table.is_empty():
            self._volt_table.update_computed(self._voltages)
        else:
            self._volt_table.populate(self._voltages, self._ch_map)
        self._volt_table.enable_corrections(self._corr_var.get())

        vmax, vmin = self._voltages.max(), self._voltages.min()
        self._summary_var.set(
            f"Peak: {vmax:+.4f} V   Min: {vmin:+.4f} V   "
            f"P\u2013P: {vmax - vmin:.4f} V"
        )

    def _toggle_corrections(self) -> None:
        if hasattr(self, '_volt_table'):
            self._volt_table.enable_corrections(self._corr_var.get())

    def _reset_corrections(self) -> None:
        if hasattr(self, '_volt_table'):
            self._volt_table.reset_corrections()

    def _on_sw_toggle(self, electrode: int, new_state: int) -> None:
        """When a per-row Sw button is clicked, switch one electrode."""
        raw = self._ch_map.get(electrode)
        if raw and self._action_cb:
            ch, _pol = resolve_ch(raw)
            self._action_cb("switch_channel",
                            (self._element, electrode, ch, new_state))

    def _dispatch(self, action: str) -> None:
        if not self._action_cb:
            return
        if action == "push":
            payload = self.get_push_payload()
            if payload is None:
                messagebox.showwarning(
                    "Nothing to push",
                    "Enter amplitude values first to compute electrode voltages.")
                return
            self._action_cb("push", payload)
        elif action in ("switch_on", "switch_off"):
            state = SWITCH_ON if action == "switch_on" else SWITCH_OFF
            self._action_cb("switch_group",
                            (self._element, self._ch_map, state))
        else:
            self._action_cb(action)


# ══════════════════════════════════════════════════════════════════════════════
#  §7  Confirmation / preview dialog
# ══════════════════════════════════════════════════════════════════════════════

class PushDialog(tk.Toplevel):
    """
    Modal dialog showing the full voltage table and asks for confirmation
    before writing to MPOD or performing a dry run.
    """

    def __init__(self, parent: tk.Widget, element: str,
                 final: list[float], ch_map: dict,
                 host: str, dry_run: bool):
        super().__init__(parent)
        self.title(f"Confirm Push \u25ba {element}")
        self.resizable(False, False)
        self.grab_set()
        self.confirmed = False

        n_mapped   = sum(1 for k in range(len(final)) if ch_map.get(k))
        n_unmapped = len(final) - n_mapped
        elem_info       = LSTAR_ELEMENTS.get(element, {})
        elem_hard_limit = elem_info.get('hard_limit', HARD_LIMIT_V)

        # Pre-compute supply set-points and detect sign issues
        v_sets: list[Optional[float]] = []
        sign_issues: list[int] = []
        for k, v in enumerate(final):
            raw = ch_map.get(k)
            if raw is None:
                v_sets.append(None)
            else:
                _, pol = resolve_ch(raw)
                v_set  = v * pol
                v_sets.append(v_set)
                if v_set < -1e-6:
                    sign_issues.append(k)

        any_over = any(abs(vs) > elem_hard_limit
                       for vs in v_sets if vs is not None)

        # ── Header ─────────────────────────────────────────────────────────
        ttk.Label(self, text=f"  Element: {element}", font=_F_TITLE,
                  ).pack(anchor="w", padx=10, pady=(10, 2))
        dest = "DRY RUN  (nothing will be written)" if dry_run \
               else f"MPOD @ {host}"
        ttk.Label(self, text=f"  Destination: {dest}", font=_F_BODY,
                  foreground=_C_AMBER if dry_run else _C_FG,
                  ).pack(anchor="w", padx=10)
        ttk.Label(self,
                  text=f"  Channels: {n_mapped} will be written, "
                       f"{n_unmapped} unmapped (skipped)",
                  font=_F_BODY).pack(anchor="w", padx=10)
        if sign_issues:
            ttk.Label(self,
                      text=f"  \u26a0  {len(sign_issues)} channel(s) have negative "
                           f"supply set-point (amber) will fail on unipolar supplies",
                      font=_F_BODY, foreground=_C_AMBER,
                      ).pack(anchor="w", padx=10)
        if any_over:
            ttk.Label(self, text="  \u26a0  Some set-points exceed element hard limit!",
                      font=_F_BODY, foreground=_C_RED,
                      ).pack(anchor="w", padx=10)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=6, pady=6)

        # ── Voltage table (electrode V to supply set-point) ──────────────────
        txt = tk.Text(self, width=56, height=min(len(final) + 3, 24),
                      font=_F_MONO, relief="flat",
                      bg="#111118", fg=_C_FG, state="normal")
        txt.pack(padx=10, fill="x")
        txt.tag_configure("warn_amber", foreground=_C_AMBER)
        txt.tag_configure("warn_red",   foreground=_C_RED)
        txt.insert("end",
                   f"{'El':>4}  {'Channel [pol]':>14}  "
                   f"{'Electrode V':>11}  {'Supply V':>11}\n")
        txt.insert("end", "\u2500" * 50 + "\n")
        for k, v in enumerate(final):
            raw = ch_map.get(k)
            if raw is None:
                ch_disp = "\u2014"
                v_set   = v
                tag     = ""
            else:
                ch_name, pol = resolve_ch(raw)
                pol_sym  = "+" if pol == +1 else "\u2212"
                ch_disp  = f"{ch_name} [{pol_sym}]"
                v_set    = v_sets[k] if v_sets[k] is not None else v
                tag      = "warn_amber" if v_set < -1e-6 else (
                           "warn_red"   if abs(v_set) > elem_hard_limit else "")
            line = (f"{k:>4}  {ch_disp:>14}  "
                    f"{v:>+11.4f}  {v_set:>+11.4f}\n")
            txt.insert("end", line, tag if tag else "")
        txt.configure(state="disabled")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=6, pady=6)

        # ── Buttons ─────────────────────────────────────────────────────────
        bf = ttk.Frame(self)
        bf.pack(pady=(0, 10))
        lbl = "Run Dry-Run (log only)" if dry_run else "\u2713  Confirm Push"
        tk.Button(bf, text=lbl,
                  bg=_C_AMBER if dry_run else _C_RED,
                  fg="black" if dry_run else "white",
                  font=_F_BOLD,
                  command=self._confirm,
                  ).pack(side="left", padx=8, ipadx=6, ipady=3)
        ttk.Button(bf, text="Cancel",
                   command=self.destroy).pack(side="left", padx=8)

    def _confirm(self) -> None:
        self.confirmed = True
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
#  §7b  Manual channel control (raw, element-independent)
# ══════════════════════════════════════════════════════════════════════════════

class ChannelControlPanel(ttk.Frame):
    """
    Probe the MPOD for every live channel and let the user set a voltage or
    flip the switch on any single channel directly independent of the
    LSTAR_ELEMENTS / channel-map machinery used by the Beamline tab.

    Host / read-community / write-community / dry-run are shared with the
    rest of the app via the connection bar, this panel just reads those
    Tk variables off `app` instead of duplicating them.
    """

    _COLS   = ("channel", "name", "vset", "vmeas", "iset", "imeas", "switch", "status")
    _HDRS   = {"channel": "Channel", "name": "Name", "vset": "V_set (V)",
               "vmeas": "V_meas (V)", "iset": "I_lim (mA)", "imeas": "I_meas (mA)",
               "switch": "Switch", "status": "Status"}
    _WIDTHS = {"channel": 70, "name": 90, "vset": 80, "vmeas": 80,
               "iset": 80, "imeas": 80, "switch": 70, "status": 180}

    def __init__(self, parent: tk.Widget, app: "LSTARApp", **kw):
        super().__init__(parent, **kw)
        self._app  = app
        self._rows: dict[str, dict] = {}
        self._sort_state: dict[str, bool] = {}
        self._build()

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4, pady=(6, 2))
        ttk.Button(bar, text="⟳ Refresh (Probe MPOD)",
                   command=self._refresh).pack(side="left")
        ttk.Label(bar, text="  uses Host / Read / Write community fields above",
                  font=_F_SMALL, foreground=_C_DIM).pack(side="left", padx=(4, 16))
        ttk.Label(bar, text="Filter:", font=_F_SMALL).pack(side="left")
        self._filter_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self._filter_var, width=14).pack(
            side="left", padx=(2, 0))
        self._filter_var.trace_add("write", lambda *_: self._refresh_tree_view())

        self._status_var = tk.StringVar(value="Not yet probed.")
        ttk.Label(self, textvariable=self._status_var,
                  font=_F_SMALL, foreground=_C_DIM).pack(
            anchor="w", padx=8, pady=(0, 4))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # ── Channel list ───────────────────────────────────────────────────
        tf = ttk.Frame(body)
        tf.pack(side="left", fill="both", expand=True)
        self._tree = ttk.Treeview(tf, columns=self._COLS, show="headings",
                                  selectmode="browse")
        for c in self._COLS:
            self._tree.heading(c, text=self._HDRS[c],
                               command=lambda c=c: self._sort_by(c))
            self._tree.column(c, width=self._WIDTHS[c], anchor="center")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self._tree.tag_configure("on",  foreground=_C_GREEN)
        self._tree.tag_configure("off", foreground=_C_RED)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        # ── Selected-channel control ─────────────────────────────────────────
        cf = ttk.LabelFrame(body, text="Selected Channel", padding=8)
        cf.pack(side="left", fill="y", padx=(8, 0))

        self._sel_var = tk.StringVar(value="(none selected)")
        ttk.Label(cf, textvariable=self._sel_var, font=_F_BOLD).pack(anchor="w")

        self._sel_detail_var = tk.StringVar(value="")
        ttk.Label(cf, textvariable=self._sel_detail_var, font=_F_SMALL,
                  foreground=_C_DIM, justify="left").pack(
            anchor="w", pady=(2, 10))

        vf = ttk.Frame(cf)
        vf.pack(fill="x", pady=2)
        ttk.Label(vf, text="Voltage (V):", font=_F_BODY).pack(side="left")
        self._volt_var = tk.StringVar(value="0.0")
        volt_entry = ttk.Entry(vf, textvariable=self._volt_var,
                               width=10, justify="right")
        volt_entry.pack(side="left", padx=4)
        volt_entry.bind("<Return>", lambda e: self._apply_voltage())
        ttk.Button(vf, text="Apply", width=7,
                  command=self._apply_voltage).pack(side="left")

        sf = ttk.Frame(cf)
        sf.pack(fill="x", pady=6)
        tk.Button(sf, text="Switch ON", width=9, font=("Courier", 9, "bold"),
                  bg="#1A4A1A", fg=_C_GREEN, relief="raised", borderwidth=1,
                  command=lambda: self._apply_switch(SWITCH_ON)).pack(
            side="left", padx=(0, 4))
        tk.Button(sf, text="Switch OFF", width=9, font=("Courier", 9, "bold"),
                  bg="#3A1A1A", fg="#CC4444", relief="raised", borderwidth=1,
                  command=lambda: self._apply_switch(SWITCH_OFF)).pack(side="left")

        ttk.Separator(cf, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(cf, text="Read this channel",
                  command=self._read_selected).pack(fill="x", pady=2)
        ttk.Button(cf, text="Zero this channel (0 V)",
                  command=self._zero_selected).pack(fill="x", pady=2)

    # ── Shared connection state (lives on the main app) ───────────────────────

    def _conn(self) -> tuple[str, str, bool]:
        return (self._app._host_var.get(),
                self._app._wcomm_var.get(),
                self._app._dry_var.get())

    def _launch(self, fn, *args) -> bool:
        """Run fn in a background thread via the app's busy tracker.Returns
        False (and warns) if an operation is already in progress."""
        if self._app._busy:
            messagebox.showwarning("Busy",
                                   "An operation is already in progress.")
            return False
        self._app._start(fn, *args)
        return True

    # ── Selection ────────────────────────────────────────────────────────────

    def _selected_channel(self) -> Optional[str]:
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("No selection",
                                "Select a channel from the list first.")
            return None
        return sel[0]

    def _on_select(self, event=None) -> None:
        sel = self._tree.selection()
        if not sel:
            self._sel_var.set("(none selected)")
            self._sel_detail_var.set("")
            return
        ch  = sel[0]
        row = self._rows.get(ch, {})
        self._sel_var.set(ch)
        self._sel_detail_var.set(
            f"Name:    {row.get('name') or '?'}\n"
            f"V_set:   {self._fmt(row.get('vset'))} V\n"
            f"V_meas:  {self._fmt(row.get('vmeas'))} V\n"
            f"Switch:  {SWITCH_LABELS.get(row.get('switch'), '?')}\n"
            f"Status:  {row.get('status') or '?'}"
        )
        if row.get('vset') is not None:
            self._volt_var.set(f"{row['vset']:.4f}")

    # ── Refresh / probe ──────────────────────────────────────────────────────

    def _refresh(self) -> None:
        if not _SNMP_OK:
            messagebox.showwarning(
                "SNMP unavailable",
                "lstar_mpod_ctl.py / puresnmp not available, cannot probe.")
            return
        host, _wc, _dry = self._conn()
        read_comm = self._app._rcomm_var.get()
        self._launch(self._refresh_worker, host, read_comm)

    def _refresh_worker(self, host: str, read_comm: str) -> None:
        self.after(0, self._app.log, f"Probing MPOD @ {host} ...")

        async def _run():
            from puresnmp import V2C, Client
            try:
                c     = Client(host, V2C(read_comm), port=161)
                names = await walk_oid(c, _WALK["name"])
                if not names:
                    self.after(0, self._app.log, "  No channels found.")
                    self.after(0, self._status_var.set,
                              "No channels found — check host/community.")
                    return
                v_set  = await walk_oid(c, _WALK["volt_set"])
                v_meas = await walk_oid(c, _WALK["volt_meas"])
                c_set  = await walk_oid(c, _WALK["curr_set"])
                c_meas = await walk_oid(c, _WALK["curr_meas"])
                sw_all = await walk_oid(c, _WALK["switch"])
                st_all = await walk_oid(c, _WALK["status"])
            except Exception as exc:
                self.after(0, self._app.log, f"  Probe failed: {exc}")
                self.after(0, self._status_var.set, f"Probe failed: {exc}")
                return

            rows = []
            for idx in sorted(names):
                ch_name, slot, ch_no = suffix_to_label(idx)
                rows.append({
                    'channel': ch_name,
                    'name':    decode_str(names[idx]),
                    'vset':    decode_float(v_set.get(idx)),
                    'vmeas':   decode_float(v_meas.get(idx)),
                    'iset':    decode_float(c_set.get(idx)),
                    'imeas':   decode_float(c_meas.get(idx)),
                    'switch':  decode_int(sw_all.get(idx)),
                    'status':  decode_status(st_all[idx]) if idx in st_all else "?",
                })
            self.after(0, self._populate_rows, rows)
            self.after(0, self._app.log,
                      f"  Probe done: {len(rows)} channel(s) found.")

        asyncio.run(_run())
        self.after(0, self._app._finish)

    def _populate_rows(self, rows: list[dict]) -> None:
        self._rows = {row['channel']: row for row in rows}
        self._refresh_tree_view()
        n_on = sum(1 for r in self._rows.values() if r.get('switch') == SWITCH_ON)
        ts   = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_var.set(
            f"{len(self._rows)} channel(s)  |  {n_on} On  |  last refresh {ts}")
        self._on_select()

    # ── Tree rendering ───────────────────────────────────────────────────────

    @staticmethod
    def _fmt(v, scale: float = 1.0) -> str:
        return "?" if v is None else f"{v * scale:+.4f}"

    def _insert_row(self, row: dict) -> None:
        ch  = row['channel']
        sw  = row.get('switch')
        tag = "on" if sw == SWITCH_ON else ("off" if sw == SWITCH_OFF else "")
        vals = (
            ch,
            row.get('name') or '',
            self._fmt(row.get('vset')),
            self._fmt(row.get('vmeas')),
            self._fmt(row.get('iset'), scale=1e3),
            self._fmt(row.get('imeas'), scale=1e3),
            SWITCH_LABELS.get(sw, '?'),
            row.get('status') or '',
        )
        if self._tree.exists(ch):
            self._tree.item(ch, values=vals, tags=(tag,))
        else:
            self._tree.insert("", "end", iid=ch, values=vals, tags=(tag,))

    def _refresh_tree_view(self, order: Optional[list] = None) -> None:
        sel = self._tree.selection()
        self._tree.delete(*self._tree.get_children())
        flt = self._filter_var.get().strip().lower()
        chans = order if order is not None else sorted(self._rows.keys())
        for ch in chans:
            row = self._rows.get(ch)
            if row is None:
                continue
            if flt and flt not in ch.lower() and flt not in (row.get('name') or '').lower():
                continue
            self._insert_row(row)
        if sel and self._tree.exists(sel[0]):
            self._tree.selection_set(sel[0])

    def _sort_by(self, col: str) -> None:
        asc = self._sort_state.get(col, True)
        self._sort_state[col] = not asc

        def key(ch):
            v = self._rows[ch].get(col)
            if v is None:
                return (1, 0)
            if isinstance(v, (int, float)):
                return (0, v)
            return (0, str(v).lower())

        order = sorted(self._rows.keys(), key=key, reverse=not asc)
        self._refresh_tree_view(order=order)

    # ── Apply voltage ────────────────────────────────────────────────────────

    def _apply_voltage(self) -> None:
        ch = self._selected_channel()
        if ch is None:
            return
        try:
            v = float(self._volt_var.get())
        except ValueError:
            messagebox.showerror("Invalid voltage", "Enter a numeric voltage value.")
            return
        if abs(v) > HARD_LIMIT_V:
            if not messagebox.askyesno(
                    "Exceeds hard limit",
                    f"{v:+.3f} V exceeds the {HARD_LIMIT_V:.0f} V hard limit.\n"
                    f"Send anyway?"):
                return
        host, write_comm, dry_run = self._conn()
        if not messagebox.askyesno(
                "Confirm",
                f"Set {ch} → {v:+.3f} V?\n\n" +
                ("[DRY RUN — nothing will change]" if dry_run
                 else f"Target: {host}")):
            return
        self._app._changelog(
            f"MANUAL SET requested: channel={ch}, voltage={v:+.4f}, "
            f"host={host}, dry_run={dry_run}")
        self._launch(self._apply_voltage_worker, ch, v, host, write_comm, dry_run)

    def _apply_voltage_worker(self, ch: str, v: float, host: str,
                              write_comm: str, dry_run: bool) -> None:
        self.after(0, self._app.log, f"Manual set: {ch} → {v:+.4f} V")

        async def _run():
            if dry_run or not _SNMP_OK:
                tag = "[DRY RUN]" if dry_run else "[NO-SNMP]"
                self.after(0, self._app.log, f"  {tag} {ch} → {v:+.4f} V")
                self.after(0, self._app._changelog,
                          f"MANUAL SET result: channel={ch}, voltage={v:+.4f}, "
                          f"dry_run=True")
                return
            from puresnmp import V2C, Client
            wtr = Client(host, V2C(write_comm), port=161)
            try:
                await wtr.set(oid_for(OID_VOLT_SET, ch), encode_float(v))
                self.after(0, self._app.log, f"  ✓ {ch} → {v:+.4f} V")
                self.after(0, self._update_row_field, ch, 'vset', v)
                self.after(0, self._app._changelog,
                          f"MANUAL SET result: channel={ch}, voltage={v:+.4f}, "
                          f"ok=True, host={host}")
            except Exception as exc:
                self.after(0, self._app.log, f"  ✗ {ch}: {exc}")
                self.after(0, self._app._changelog,
                          f"MANUAL SET result: channel={ch}, voltage={v:+.4f}, "
                          f"ok=False, error={exc}")

        asyncio.run(_run())
        self.after(0, self._app._finish)

    def _zero_selected(self) -> None:
        self._volt_var.set("0.0")
        self._apply_voltage()

    # ── Switch on/off ────────────────────────────────────────────────────────

    def _apply_switch(self, state: int) -> None:
        ch = self._selected_channel()
        if ch is None:
            return
        label = "ON" if state == SWITCH_ON else "OFF"
        host, write_comm, dry_run = self._conn()
        if not messagebox.askyesno(
                f"Switch {label}",
                f"Turn {ch} {label}?\n\n" +
                ("[DRY RUN]" if dry_run else f"Target: {host}")):
            return
        self._app._changelog(
            f"MANUAL SWITCH {label} requested: channel={ch}, "
            f"host={host}, dry_run={dry_run}")
        self._launch(self._apply_switch_worker, ch, state, host, write_comm, dry_run)

    def _apply_switch_worker(self, ch: str, state: int, host: str,
                             write_comm: str, dry_run: bool) -> None:
        label = "ON" if state == SWITCH_ON else "OFF"
        self.after(0, self._app.log, f"Manual switch: {ch} → {label}")

        async def _run():
            if dry_run or not _SNMP_OK:
                tag = "[DRY RUN]" if dry_run else "[NO-SNMP]"
                self.after(0, self._app.log, f"  {tag} {ch} → {label}")
                self.after(0, self._app._changelog,
                          f"MANUAL SWITCH {label} result: channel={ch}, "
                          f"dry_run=True")
                return
            from puresnmp import V2C, Client
            wtr = Client(host, V2C(write_comm), port=161)
            try:
                await wtr.set(oid_for(OID_SWITCH, ch), encode_int(state))
                self.after(0, self._app.log, f"  ✓ {ch} → {label}")
                self.after(0, self._update_row_field, ch, 'switch', state)
                self.after(0, self._app._changelog,
                          f"MANUAL SWITCH {label} result: channel={ch}, "
                          f"ok=True, host={host}")
            except Exception as exc:
                self.after(0, self._app.log, f"  ✗ {ch}: {exc}")
                self.after(0, self._app._changelog,
                          f"MANUAL SWITCH {label} result: channel={ch}, "
                          f"ok=False, error={exc}")

        asyncio.run(_run())
        self.after(0, self._app._finish)

    def _update_row_field(self, ch: str, field: str, value) -> None:
        if ch in self._rows:
            self._rows[ch][field] = value
            self._insert_row(self._rows[ch])
            self._on_select()

    # ── Single-channel read ──────────────────────────────────────────────────

    def _read_selected(self) -> None:
        ch = self._selected_channel()
        if ch is None:
            return
        if not _SNMP_OK:
            messagebox.showwarning("SNMP unavailable",
                                   "lstar_mpod_ctl.py / puresnmp not available.")
            return
        host, _wc, _dry = self._conn()
        read_comm = self._app._rcomm_var.get()
        self._launch(self._read_one_worker, ch, host, read_comm)

    def _read_one_worker(self, ch: str, host: str, read_comm: str) -> None:
        self.after(0, self._app.log, f"Reading {ch} ...")

        async def _run():
            from puresnmp import V2C, Client
            rdr = Client(host, V2C(read_comm), port=161)
            try:
                r  = await rdr.multiget([
                    oid_for(OID_VOLT_SET,  ch),
                    oid_for(OID_VOLT_MEAS, ch),
                    oid_for(OID_SWITCH,    ch),
                ])
                vs, vm, sw = (decode_float(r[0].value),
                             decode_float(r[1].value),
                             decode_int(r[2].value))
                self.after(0, self._update_row_field, ch, 'vset', vs)
                self.after(0, self._update_row_field, ch, 'vmeas', vm)
                self.after(0, self._update_row_field, ch, 'switch', sw)
                self.after(0, self._app.log,
                          f"  {ch}: V_set={self._fmt(vs)}  V_meas={self._fmt(vm)}  "
                          f"Switch={SWITCH_LABELS.get(sw, '?')}")
            except Exception as exc:
                self.after(0, self._app.log, f"  Read {ch} failed: {exc}")

        asyncio.run(_run())
        self.after(0, self._app._finish)


# ══════════════════════════════════════════════════════════════════════════════
#  §8  Main application window
# ══════════════════════════════════════════════════════════════════════════════

class LSTARApp(tk.Tk):

    def __init__(self, host: str = "192.168.55.8",
                 write_comm: str = "guru",
                 read_comm:  str = "public",
                 dry_run:    bool = False):
        super().__init__()
        self.title("LSTAR Multipole Control - TAMUTRAP, Cyclotron Institute")
        self.minsize(930, 640)

        self._host       = host
        self._write_comm = write_comm
        self._read_comm  = read_comm
        self._dry_run    = dry_run or (not _SNMP_OK)
        self._busy       = False

        self._build_menu()
        self._build_conn_bar()
        self._build_main_area()
        self._build_log()
        self._ctrl.set_action_callback(self._on_action)

        self.log(
            f"GUI ready.  MPOD: {host}  |  "
            f"SNMP: {'OK' if _SNMP_OK else 'unavailable'}  |  "
            f"Mode: {'DRY RUN' if self._dry_run else 'Hardware'}"
        )
        if not _SNMP_OK:
            self.log("lstar_mpod_ctl.py or puresnmp not found \u2014 "
                     "all pushes will be dry-run only.")

        self._changelog(
            f"SESSION START: host={host}, SNMP={'OK' if _SNMP_OK else 'unavailable'}, "
            f"dry_run={self._dry_run}")

    # ── Menu ────────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        mb = tk.Menu(self)
        self.config(menu=mb)

        fm = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="File", menu=fm)
        self._dry_menu_var = tk.BooleanVar(value=self._dry_run)
        fm.add_checkbutton(label="Dry Run (doesn't write to hardware)",
                           variable=self._dry_menu_var,
                           command=self._sync_dry_run)
        fm.add_separator()
        fm.add_command(label="Clear Log", command=self._clear_log)
        fm.add_separator()
        fm.add_command(label="Quit", command=self.quit)

        hm = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Help", menu=hm)
        hm.add_command(label="About", command=self._show_about)

    def _sync_dry_run(self) -> None:
        self._dry_run = self._dry_menu_var.get()
        if hasattr(self, '_dry_var'):
            self._dry_var.set(self._dry_run)
        self.log(f"Dry run: {'ON' if self._dry_run else 'OFF'}")


    def _show_about(self) -> None:
        messagebox.showinfo(
            "About",
            "LSTAR Multipole Control GUI\n"
            "TAMU Cyclotron Institute\n\n"
            "Controls squirrel-cage multipole elements\n"
            "in LSTAR (except magnetic dipoles) via WIENER MPOD SNMP.\n\n"
            "Companion to lstar_mpod_ctl.py.\n\n"
            "Manual Channel Control tab: probe the MPOD directly\n"
            "and set/switch any single raw channel (e.g. u700),\n"
            "independent of the element channel map.\n\n"
            "To swap the beamline diagram, replace the body of\n"
            "BeamlineDiagram.draw_programmatic() with PIL image-\n"
            "loading code and update ELEMENT_LAYOUT positions.\n\n"
            "Every push/zero/switch is appended to a persistent\n"
            "changelog:\n"
            f"  {CHANGELOG_PATH}"
        )

    # ── Connection bar ──────────────────────────────────────────────────────

    def _build_conn_bar(self) -> None:
        bar = ttk.LabelFrame(self, text="MPOD Connection", padding=3)
        bar.pack(fill="x", padx=6, pady=(4, 0))

        ttk.Label(bar, text="Host:", font=_F_BODY).pack(side="left")
        self._host_var = tk.StringVar(value=self._host)
        ttk.Entry(bar, textvariable=self._host_var, width=16).pack(
            side="left", padx=(2, 12))

        ttk.Label(bar, text="Write community:", font=_F_BODY).pack(side="left")
        self._wcomm_var = tk.StringVar(value=self._write_comm)
        ttk.Entry(bar, textvariable=self._wcomm_var, width=8).pack(
            side="left", padx=(2, 12))

        ttk.Label(bar, text="Read community:", font=_F_BODY).pack(side="left")
        self._rcomm_var = tk.StringVar(value=self._read_comm)
        ttk.Entry(bar, textvariable=self._rcomm_var, width=8).pack(
            side="left", padx=(2, 8))

        # Dry-run checkbox, kept in sync with File menu item
        self._dry_var = tk.BooleanVar(value=self._dry_run)
        def _cb_sync():
            self._dry_run = self._dry_var.get()
            self._dry_menu_var.set(self._dry_run)
        ttk.Checkbutton(bar, text="Dry run",
                        variable=self._dry_var,
                        command=_cb_sync).pack(side="right", padx=8)

        self._status_lbl = ttk.Label(
            bar, text="\u25cf  Ready",
            foreground=_C_GREEN if _SNMP_OK else _C_AMBER,
            font=_F_BODY)
        self._status_lbl.pack(side="right", padx=8)

    # ── Main area + log (single resizeable layout) ───────────────────────────

    def _build_main_area(self) -> None:
        # ── Outer vertical splitter: top = tabs, bottom = log ──────────────
        outer = ttk.PanedWindow(self, orient="vertical")
        outer.pack(fill="both", expand=True, padx=6, pady=4)

        notebook = ttk.Notebook(outer)

        # ── Tab 1: Beamline Control (diagram | element config) ────────────
        beamline_tab = ttk.Frame(notebook)
        top = ttk.PanedWindow(beamline_tab, orient="horizontal")
        top.pack(fill="both", expand=True)

        # Left: beamline diagram (fills its frame, scales on resize)
        df = ttk.LabelFrame(top, text="Beamline Diagram", padding=2)
        self._diagram = BeamlineDiagram(df, on_select=self._on_elem_selected)
        self._diagram.pack(fill="both", expand=True)
        top.add(df, weight=1)

        # Right: element configuration panel
        cf = ttk.LabelFrame(top, text="Element Configuration", padding=4)
        self._ctrl = ControlPanel(cf, log_fn=self.log)
        self._ctrl.pack(fill="both", expand=True)
        top.add(cf, weight=2)

        notebook.add(beamline_tab, text="Beamline Control")

        # ── Tab 2: Manual Channel Control (raw probe / set / switch) ──────
        chan_tab = ttk.Frame(notebook)
        self._chan_ctrl = ChannelControlPanel(chan_tab, app=self)
        self._chan_ctrl.pack(fill="both", expand=True)
        notebook.add(chan_tab, text="Manual Channel Control")

        outer.add(notebook, weight=4)

        # ── Bottom pane: status log ─────────────────
        lf = ttk.LabelFrame(outer, text="Status Log", padding=2)
        hdr = ttk.Frame(lf)
        hdr.pack(fill="x")
        ttk.Button(hdr, text="Clear", width=6,
                   command=self._clear_log).pack(side="right", pady=1)
        self._log_txt = scrolledtext.ScrolledText(
            lf, state="disabled",
            font=_F_MONO, bg="#0A0A12", fg="#9999BB",
            relief="flat", wrap="word")
        self._log_txt.pack(fill="both", expand=True)
        outer.add(lf, weight=1)

    def _build_log(self) -> None:
        pass   # folded into _build_main_area; kept so __init__ call is harmless

    def log(self, msg: str) -> None:
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_txt.configure(state="normal")
        self._log_txt.insert("end", f"[{ts}] {msg}\n")
        self._log_txt.see("end")
        self._log_txt.configure(state="disabled")

    def _clear_log(self) -> None:
        self._log_txt.configure(state="normal")
        self._log_txt.delete("1.0", "end")
        self._log_txt.configure(state="disabled")

    def _changelog(self, msg: str) -> None:
        """Append one line to the persistent changelog file (see CHANGELOG_PATH)."""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{getpass.getuser()}] {msg}\n"
        try:
            with open(CHANGELOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            self.log(f"  (could not write changelog: {exc})")

    # ── Callbacks ───────────────────────────────────────────────────────────

    def _on_elem_selected(self, key: str) -> None:
        self.log(f"Selected: {key}  ({LSTAR_ELEMENTS[key]['description']})")
        self._ctrl.load_element(key)
        # Auto-readback so Sw buttons show the real On/Off state immediately
        # instead of sitting on the grey "?" until the user manually hits
        # Readback.
        if not self._busy:
            self._on_action("readback")

    def _on_action(self, action: str, payload=None) -> None:
        if self._busy:
            messagebox.showwarning("Busy",
                                   "An operation is already in progress.")
            return

        host       = self._host_var.get()
        write_comm = self._wcomm_var.get()
        read_comm  = self._rcomm_var.get()
        dry_run    = self._dry_var.get()
        element    = self._ctrl._element

        if element is None:
            self.log("No element selected.")
            return

        ch_map = load_channel_map(None).get(element, {})

        if action == "readback":
            self._start(self._do_readback, element, ch_map, host, read_comm)

        elif action == "zero":
            if not messagebox.askyesno(
                    "Zero element",
                    f"Set ALL mapped channels of {element} to 0.000 V?\n\n"
                    + ("[DRY RUN, nothing will change]"
                       if dry_run else "")):
                return
            self._changelog(
                f"ZERO requested: element={element}, "
                f"channels={len(ch_map)}, host={host}, dry_run={dry_run}")
            self._start(self._do_zero, element, ch_map,
                        host, write_comm, dry_run)

        elif action == "switch_group":
            elem, sw_ch_map, state = payload
            label = "ON" if state == SWITCH_ON else "OFF"
            if not messagebox.askyesno(
                    f"Switch {label}",
                    f"Turn {label} all mapped channels of {elem}?\n"
                    f"({sum(1 for c in sw_ch_map.values() if c)} channels)\n\n"
                    + ("[DRY RUN]" if dry_run else "")):
                return
            self._changelog(
                f"SWITCH {label} requested: element={elem}, "
                f"channels={sum(1 for c in sw_ch_map.values() if c)}, "
                f"host={host}, dry_run={dry_run}")
            self._start(self._do_switch, elem, sw_ch_map, state,
                        host, write_comm, dry_run)

        elif action == "switch_channel":
            elem, electrode, ch, state = payload
            label = "ON" if state == SWITCH_ON else "OFF"
            self._changelog(
                f"SWITCH {label} requested: element={elem}, "
                f"electrode={electrode}, channel={ch}, "
                f"host={host}, dry_run={dry_run}")
            self._start(self._do_switch_channel, elem, electrode, ch, state,
                        host, write_comm, dry_run)

        elif action == "push":
            elem, final, push_ch_map = payload
            dlg = PushDialog(self, elem, final, push_ch_map, host, dry_run)
            self.wait_window(dlg)
            if not dlg.confirmed:
                self.log("Push cancelled.")
                return
            amps = self._ctrl.get_amplitudes()
            corr = self._ctrl.get_corrections()
            self._changelog(
                f"PUSH requested: element={elem}, amplitudes={amps}, "
                f"corrections={corr}, host={host}, dry_run={dry_run}")
            self._start(self._do_push, elem, final, push_ch_map,
                        host, write_comm, dry_run)

    # ── Thread helpers ───────────────────────────────────────────────────────

    def _start(self, fn, *args) -> None:
        self._busy = True
        self._set_status("\u25cf  Working\u2026", _C_AMBER)
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _finish(self) -> None:
        self._busy = False
        self._set_status("\u25cf  Ready",
                         _C_GREEN if _SNMP_OK else _C_AMBER)

    def _set_status(self, text: str, color: str) -> None:
        self._status_lbl.configure(text=text, foreground=color)

    # ── SNMP workers (daemon threads) ────────────────────────────────────────

    def _do_push(self, element: str, final: list[float],
                 ch_map: dict, host: str,
                 write_comm: str, dry_run: bool) -> None:
        n_ch = sum(1 for k in range(len(final)) if ch_map.get(k))
        self.after(0, self.log,
                   f"Push: {element}  | {n_ch} channels  |"
                   f"  host={host}  dry_run={dry_run}")

        async def _run():
            if dry_run or not _SNMP_OK:
                tag = "[DRY RUN]" if dry_run else "[NO-SNMP]"
                for k, v in enumerate(final):
                    raw = ch_map.get(k)
                    if raw is None:
                        continue
                    ch, pol = resolve_ch(raw)
                    v_set   = v * pol
                    pol_sym = "+" if pol == +1 else "\u2212"
                    self.after(0, self.log,
                               f"  {tag} el {k:2d} ({ch} [{pol_sym}])  "
                               f"electrode {v:+.4f} V \u2192 supply {v_set:+.4f} V")
                    await asyncio.sleep(0.004)
                self.after(0, self.log, "  Dry run complete.")
                self.after(0, self._changelog,
                           f"PUSH result: element={element}, dry_run=True, "
                           f"would_send={n_ch} channels")
                return

            from puresnmp import V2C, Client
            wtr = Client(host, V2C(write_comm), port=161)
            sent = fails = 0
            for k, v in enumerate(final):
                raw = ch_map.get(k)
                if raw is None:
                    continue
                ch, pol = resolve_ch(raw)
                v_set   = v * pol
                pol_sym = "+" if pol == +1 else "\u2212"
                try:
                    await wtr.set(oid_for(OID_VOLT_SET, ch), encode_float(v_set))
                    self.after(0, self.log,
                               f"  \u2713 el {k:2d} ({ch} [{pol_sym}])  "
                               f"electrode {v:+.4f} V \u2192 supply {v_set:+.4f} V")
                    sent += 1
                except Exception as exc:
                    self.after(0, self.log,
                               f"  \u2717 el {k:2d} ({ch} [{pol_sym}])  "
                               f"supply {v_set:+.4f} V: {exc}")
                    fails += 1
                await asyncio.sleep(_WRITE_PAUSE)
            self.after(0, self.log,
                       f"  Push done: {sent} sent, {fails} failed.")
            self.after(0, self._changelog,
                       f"PUSH result: element={element}, sent={sent}, "
                       f"failed={fails}, host={host}")

        asyncio.run(_run())
        self.after(0, self._finish)

    def _do_readback(self, element: str, ch_map: dict,
                     host: str, read_comm: str) -> None:
        self.after(0, self.log,
                   f"Readback: {element}  ({len(ch_map)} mapped channels)")

        async def _run():
            if not _SNMP_OK:
                self.after(0, self.log,
                           "  (SNMP unavailable, cannot readback)")
                return
            from puresnmp import V2C, Client
            rdr = Client(host, V2C(read_comm), port=161)
            for k, raw in sorted(ch_map.items()):
                ch, pol = resolve_ch(raw)
                pol_sym = "+" if pol == +1 else "\u2212"
                try:
                    r  = await rdr.multiget([
                        oid_for(OID_VOLT_SET,  ch),
                        oid_for(OID_VOLT_MEAS, ch),
                        oid_for(OID_SWITCH,    ch),
                    ])
                    vs   = decode_float(r[0].value)
                    vm   = decode_float(r[1].value)
                    sw_i = decode_int(r[2].value)
                    sw   = SWITCH_LABELS.get(sw_i, "?")
                    vs_s = f"{vs:+.3f}" if vs is not None else "?"
                    vm_s = f"{vm:+.3f}" if vm is not None else "?"
                    self.after(0, self.log,
                               f"  el {k:2d} ({ch} [{pol_sym}])  "
                               f"V_set={vs_s} V  V_meas={vm_s} V  sw={sw}")
                    if sw_i is not None:
                        self.after(0, self._ctrl._volt_table.set_switch_state,
                                   k, sw_i)
                except Exception as exc:
                    self.after(0, self.log,
                               f"  el {k:2d} ({ch} [{pol_sym}])  ERROR: {exc}")

        asyncio.run(_run())
        self.after(0, self._finish)

    def _do_zero(self, element: str, ch_map: dict,
                 host: str, write_comm: str, dry_run: bool) -> None:
        self.after(0, self.log,
                   f"Zero: {element}  ({len(ch_map)} channels)")

        async def _run():
            if dry_run or not _SNMP_OK:
                tag = "[DRY RUN]" if dry_run else "[NO-SNMP]"
                for k, raw in sorted(ch_map.items()):
                    ch, pol = resolve_ch(raw)
                    pol_sym = "+" if pol == +1 else "\u2212"
                    self.after(0, self.log,
                               f"  {tag} el {k:2d} ({ch} [{pol_sym}]) \u2192 0.000 V")
                self.after(0, self.log, "  Zero dry run complete.")
                self.after(0, self._changelog,
                           f"ZERO result: element={element}, dry_run=True, "
                           f"would_zero={len(ch_map)} channels")
                return

            from puresnmp import V2C, Client
            wtr = Client(host, V2C(write_comm), port=161)
            sent = fails = 0
            for k, raw in sorted(ch_map.items()):
                ch, pol = resolve_ch(raw)
                pol_sym = "+" if pol == +1 else "\u2212"
                try:
                    await wtr.set(oid_for(OID_VOLT_SET, ch), encode_float(0.0))
                    self.after(0, self.log,
                               f"  \u2713 el {k:2d} ({ch} [{pol_sym}]) \u2192 0.000 V")
                    sent += 1
                except Exception as exc:
                    self.after(0, self.log,
                               f"  \u2717 el {k:2d} ({ch} [{pol_sym}]): {exc}")
                    fails += 1
                await asyncio.sleep(_WRITE_PAUSE)
            self.after(0, self.log,
                       f"  Zero done: {sent} sent, {fails} failed.")
            self.after(0, self._changelog,
                       f"ZERO result: element={element}, sent={sent}, "
                       f"failed={fails}, host={host}")

        asyncio.run(_run())
        self.after(0, self._finish)

    def _do_switch(self, element: str, ch_map: dict, state: int,
                   host: str, write_comm: str, dry_run: bool) -> None:
        """Switch all mapped channels of element On or Off."""
        label = "ON" if state == SWITCH_ON else "OFF"
        self.after(0, self.log,
                   f"Switch {element} \u2192 {label}  ({len(ch_map)} channels)")

        async def _run():
            if dry_run or not _SNMP_OK:
                tag = "[DRY RUN]" if dry_run else "[NO-SNMP]"
                for k, raw in sorted(ch_map.items()):
                    ch, pol = resolve_ch(raw)
                    pol_sym = "+" if pol == +1 else "\u2212"
                    self.after(0, self.log,
                               f"  {tag} el {k:2d} ({ch} [{pol_sym}]) \u2192 {label}")
                self.after(0, self._changelog,
                           f"SWITCH {label} result: element={element}, "
                           f"dry_run=True, would_switch={len(ch_map)} channels")
                return
            from puresnmp import V2C, Client
            from x690.types import Integer
            wtr = Client(host, V2C(write_comm), port=161)
            ok = fails = 0
            for k, raw in sorted(ch_map.items()):
                ch, pol = resolve_ch(raw)
                pol_sym = "+" if pol == +1 else "\u2212"
                try:
                    await wtr.set(oid_for(OID_SWITCH, ch), Integer(state))
                    self.after(0, self.log,
                               f"  \u2713 el {k:2d} ({ch} [{pol_sym}]) \u2192 {label}")
                    self.after(0, self._ctrl._volt_table.set_switch_state,
                               k, state)
                    ok += 1
                except Exception as exc:
                    self.after(0, self.log,
                               f"  \u2717 el {k:2d} ({ch} [{pol_sym}]): {exc}")
                    fails += 1
                await asyncio.sleep(_WRITE_PAUSE)
            self.after(0, self.log,
                       f"  Switch done: {ok} set, {fails} failed.")
            self.after(0, self._changelog,
                       f"SWITCH {label} result: element={element}, "
                       f"ok={ok}, failed={fails}, host={host}")

        asyncio.run(_run())
        self.after(0, self._finish)

    def _do_switch_channel(self, element: str, electrode: int, ch: str,
                           state: int, host: str, write_comm: str,
                           dry_run: bool) -> None:
        """Switch a single electrode channel On or Off."""
        label = "ON" if state == SWITCH_ON else "OFF"
        self.after(0, self.log,
                   f"Switch el {electrode:2d} ({ch}) \u2192 {label}")

        async def _run():
            if dry_run or not _SNMP_OK:
                tag = "[DRY RUN]" if dry_run else "[NO-SNMP]"
                self.after(0, self.log,
                           f"  {tag} el {electrode:2d} ({ch}) \u2192 {label}")
                self.after(0, self._changelog,
                           f"SWITCH {label} result: element={element}, "
                           f"electrode={electrode}, channel={ch}, dry_run=True")
                return
            from puresnmp import V2C, Client
            from x690.types import Integer
            wtr = Client(host, V2C(write_comm), port=161)
            try:
                await wtr.set(oid_for(OID_SWITCH, ch), Integer(state))
                self.after(0, self.log,
                           f"  \u2713 el {electrode:2d} ({ch}) \u2192 {label}")
                self.after(0, self._ctrl._volt_table.set_switch_state,
                           electrode, state)
                self.after(0, self._changelog,
                           f"SWITCH {label} result: element={element}, "
                           f"electrode={electrode}, channel={ch}, ok=True")
            except Exception as exc:
                self.after(0, self.log,
                           f"  \u2717 el {electrode:2d} ({ch}): {exc}")
                self.after(0, self._changelog,
                           f"SWITCH {label} result: element={element}, "
                           f"electrode={electrode}, channel={ch}, "
                           f"ok=False, error={exc}")

        asyncio.run(_run())
        self.after(0, self._finish)


# ══════════════════════════════════════════════════════════════════════════════
#  §9  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LSTAR Multipole Control GUI - TAMUTRAP, TAMU Cyclotron Institute"
    )
    p.add_argument("--host", default="192.168.55.8",
                   help="MPOD IP address  (default: 192.168.55.8)")
    p.add_argument("--write-community", default="guru", dest="write_community",
                   help="SNMP write community  (default: guru)")
    p.add_argument("--read-community",  default="public", dest="read_community",
                   help="SNMP read community   (default: public)")
    p.add_argument("--dry-run", action="store_true",
                   help="Doesn't write to hardware (safe for testing)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    app  = LSTARApp(
        host       = args.host,
        write_comm = args.write_community,
        read_comm  = args.read_community,
        dry_run    = args.dry_run,
    )
    app.mainloop()


if __name__ == "__main__":
    main()
