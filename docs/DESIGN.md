# LSTAR MPOD Control — Design Notes

This document is the "why," not the "how." For operating instructions, see
[`MANUAL.md`](MANUAL.md). For the high-level project overview, see the main
[`README.md`](../README.md).

---

## 1. The problem

What physically exists: LSTAR elements are rings of 4, 6, or 24 independently
wired electrode rods. What's physically meaningful: a handful of multipole
amplitudes (quadrupole strength, hexapole strength, ...). Translating between the
two — for every element shape, in both directions, without the physics code
needing to know about SNMP and without the SNMP code needing to know about
physics — is the actual design problem this project solves.

*(Expand: restate this more concretely with one worked example — e.g. take a
single {Q: 1500, O: -200} request for `(Q+oct)1` and trace it end-to-end through
every layer below, ending in the actual SNMP SETs that would be issued.)*

---

## 2. The multipole math

### 2.1 Why one formula covers every element shape

*(Expand: show that a pure quadrupole's [+1,-1,+1,-1] pattern and a 24-rod
squirrel-cage's pattern are literally the same function evaluated at different N,
not two related-but-separate cases. This is the core "first-principles
abstraction" argument — worth spending real space on.)*

### 2.2 `triangular_basis()` derivation

*(Expand: walk through `t = (n*k/N) % 1.0` and `2*abs(2*t - 1) - 1` step by step —
what each stage of the expression is doing geometrically, and why it produces a
normalized triangular wave on [-1, +1] rather than e.g. a sinusoid. State what was
verified against the DANFYSIK/LSTAR spreadsheet and to what precision.)*

### 2.3 Superposition

*(Expand: why amplitudes for different multipole orders can simply be summed
per-electrode — what physical/mathematical property of the basis functions makes
that valid, and what would break if it weren't true.)*

---

## 3. The polarity-factor architecture

### 3.1 The actual hardware problem

*(Expand: bipolar iseg HV modules vs. unipolar 0MPV LV modules — what "unipolar"
actually constrains, and why this is a real constraint rather than a software
inconvenience to abstract away.)*

### 3.2 Why a multiplicative polarity factor, and not something else

*(Expand: state at least one alternative that was implicitly rejected — e.g.
hardcoding sign flips inside `compute_voltages()` per element, or handling it
ad-hoc at write-time — and why `v_set = v_electrode × polarity_factor` living in
the channel map is the better seam. What does this design keep separate that the
alternatives would tangle together?)*

### 3.3 Consequence: the physics layer never knows about hardware polarity

*(Expand: this is the payoff of 3.2 — trace how `compute_voltages()` stays
hardware-agnostic and all module-type-specific knowledge lives in exactly one
place, the channel map.)*

---

## 4. The three-layer safety design

A table of *what* the layers are belongs in the manual. This section is about
*why three, and why these three.*

| Layer | What it would look like if this layer didn't exist |
|---|---|
| Spec limits (`max_amplitude`) | *(Expand: concrete scenario — a physics-invalid amplitude gets pushed because nothing checked it against Table 5)* |
| Hard limit (`hard_limit` / `HARD_LIMIT_V`) | *(Expand: concrete scenario — something passes the spec check anyway and still damages hardware; why is this layer independent rather than derived from the spec table?)* |
| Polarity preflight (`_check_voltage_signs`) | *(Expand: concrete scenario — a negative set-point reaches a unipolar module; what does the crate actually do, and why is catching it *before* the write loop starts better than letting the crate reject it mid-push?)* |

*(Expand: close with the general principle — these three layers catch failures at
different points in the pipeline (config-time / hardware-ceiling / per-electrode
write-time) and none of them is redundant with the others.)*

---

## 5. Known discrepancies and open questions

This section exists so spec-vs-implementation drift is tracked deliberately
instead of silently. Pulled forward from the `lstar_gui.py` module docstring —
expand each into a real entry with current status and what resolving it would
require:

- **M rod count (24 vs. 48).** Code uses `n_rods=24`. Spec v03.10.2023 Fig. 2
  shows a 48-rod example, but that figure is cited as design inspiration from the
  CARIBU instrument at Argonne, not as the LSTAR spec itself. DANFYSIK report
  504663 (Apr 2024) specifies 24 rods. *(Expand: status — confirmed with
  Melconian? still open?)*
- **Q1-Q4 / S1-S2 classification.** These are simple bipolar HV elements, not
  squirrel-cage multipoles — *(Expand: resolve against the fact that they ARE
  present in `LSTAR_ELEMENTS` with defined spec limits; the current GUI docstring
  saying they're "absent" is stale and should be corrected as part of writing this
  section, not left as a documented discrepancy.)*
- **(Q+oct)1 / (Q+oct)2 wiring.** Channel maps empty — hardware not yet installed.
  *(Expand: any known timeline / dependency for when this gets wired.)*
- **M hexapole sign convention.** Table 5 lists H = −0.3 kV (unipolar negative),
  but the code stores `limit=300` and checks with `abs()`, so +300 V would also
  pass the spec check. *(Expand: is this intentionally permissive pending an
  operational sign-convention rule, or should the limit check itself enforce the
  sign? Decide and state the decision here, not just the ambiguity.)*

---

## 6. Deliberate non-features

Design decisions about what the system *doesn't* do, stated explicitly so they
don't read as oversights to a future maintainer (or a reviewer).

- **Multipole pushes never switch channels on/off.** *(Expand: why this is a
  safety choice — what's the risk of coupling "set a voltage" and "energize a
  channel" into one action, given a human should presumably be confirming the
  channel state separately?)*
- *(Expand: add any other "we could have automated this but chose not to"
  decisions — e.g. should pushing to a full element auto-zero unmapped electrodes?
  Does it currently? Should `--force` require any additional confirmation step?)*

---

## 7. What would need to change to extend this

*(Expand: a short forward-looking section — e.g. what's involved in wiring a new
element's channel map, what would need to change if a future module type were
neither bipolar nor unipolar-in-the-current-sense, whether the triangular basis
generalizes to multipole orders beyond Do/order-6 without modification.)*
