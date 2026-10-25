# Plane fanout

On a board with planes (say signal / GND / power / signal), every GND and power pad needs a
via to its plane. Left to Freerouting, those vias are added by patching its routes after the
fact, which leaves 90-degree corners and detours. `plan_fanout` places them first, so the
plane nets are complete before Freerouting runs and it only routes signals.

## Workflow

1. Fill the planes, then call `plan_fanout`. By default every net with a filled zone on an
   inner layer is a plane net; `planes` names them instead (`{"GND": "In1.Cu", "+3V3": "In2.Cu"}`).
2. Read the rows, then pass the returned `plan` and `board_token` to `apply_plan`: one
   commit, with every stub and via locked (`lock=false` leaves them free).
3. `refill_zones`, then `net_islands` for each plane net: one group means every pad reaches
   its plane.
4. An exposed pad (QFN, power packages) needs a second pass with `via_in_pad=true`, usually
   limited with `references`.

## How each pad is joined

- **Sharing first.** A pad first tries to share a connection nearby: a track of at most
  `share_distance` (1 mm) to a via of its net that is on the plane, or to a pad already joined
  to it, existing or planned in the same run. One new via serves at most `pads_per_via` (4)
  pads. Only when nothing is close enough does the pad get its own via.
- **A new via** sits at most `fanout_distance` (1.5 mm) from the pad's centre, on a straight
  stub at 0, 45 or 90 degrees. It must lie entirely on its plane's fill and keep every
  clearance `check_plan` checks, plus `pad_gap` (0.1 mm) from pads so solder stays on the pad.
- **Where it goes.** Short stubs come first. On parts with many pads the via goes inwards,
  so signals keep the outside to escape; on passives it goes away from the other pad. Spots
  that leave no signal channel next to other nets' pads cost more.
- **Order.** Pads with the fewest legal spots are placed first, and pads that fit nowhere
  are tried again at the end, when there may be a via to share.
- **Tracks** to a shared via or pad run straight, or with one 45-degree bend, never at 90.
- **Sizes** come from each net's netclass unless `track_width`, `via_diameter` or `via_drill`
  are given; a stub is never wider than its pad.

Skipped:
- Through-hole pads, which already reach every layer.
- Pads whose own copper already joins the plane (`include_connected=true` plans them anyway).
  Touching a top pour that is tied to the plane somewhere else doesn't count as joined.

Same-net pads stacked on top of each other (USB-C A1/B12) count as one pad.

The three distances are arguments, so each board can use what suits its parts and density;
`share_distance=0` gives every pad its own via. Pads with no legal spot are listed as
unplaced (`spots: 0` means no via spot within `fanout_distance`). A via inside a pad is
allowed only with `via_in_pad`, and only one per pad; it needs filled or tented vias in
production.

## Tested on

A copy of a four-layer RP2350 board (GND on In1, +3V3 on In2) with all routing removed:
- **Placement.** 109 of 110 plane pads joined in about 1 s.
- **Sharing.** 105 new vias with the default `share_distance`; 109 with sharing off, 101 at
  1.5 mm. The shared ones were the stacked USB-C GND pins and pairs of neighbouring RP2350
  +3V3 pins.
- **Exposed pad.** It received its via on a second pass with `via_in_pad`.
- **DRC.** KiCad's DRC then reported no missing GND or +3V3 connections and no new
  violations; the 107 unconnected items left were all signals.

## Not yet

- One via per pad, also on exposed pads (no via arrays for heat).
- Through vias only; no blind or buried spans.
