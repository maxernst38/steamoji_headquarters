# Archive

Approaches that were built, measured, and did not work. They are kept out of the
live tree but not deleted, because the reasons they failed are worth more than the
code — each one is a route that looks obviously correct until you measure it.

## `auto_detect.py` + `sam_detect.py` — automatic field calibration

The goal was to find the field's corners without the user drawing them. Four
approaches were measured against a hand-drawn reference:

| approach | result |
|---|---|
| colour segmentation of the mat | IoU 0.44 — leaks over the far wall into the venue floor |
| line-based quad search | never came within 60px of truth |
| SAM region proposal | IoU 0.53 |
| SAM ∩ colour mask (`sam_detect.py`) | IoU 0.75, ~30px corner error |

The blocker is that the field's surroundings are not separable from the field.
Interior and exterior are statistically identical in hue and saturation, and the
mat-coloured concrete beyond the far wall means any region-growing objective gains
more by swallowing it than by stopping at the wall.

`sam_detect.py` was the best of them and still failed generalisation: run on the
five matches in the sample video it produced one correct refusal, three quads that
under-covered, and one badly wrong — while three of those matches were shot from
near-identical angles. It was tuned to a single frame.

## `alliance.py` — reading alliance colour from the robot

The idea was sound: a robot cannot change alliance mid-match, so a track whose
colour flips has swapped onto a different robot. That reasoning still holds.

What fails is reading alliance *from colour ratios in VRC*. Measured on four
confirmed-distinct robots, three read 95–100% red in a match the scoreboard says is
two red against two blue. VEX robots are built from red-anodised aluminium and red
rubber bands regardless of alliance, and both alliances' game pieces cover the
field. Unlike FRC, where bumpers are large and definitive, VRC's alliance marker is
a small licence plate that a colour ratio drowns out.

Starting position is the sounder basis — alliances begin on opposite sides — and
that needs the calibration's orientation resolved first.

## `legacy-per-video-calibration/`

The original whole-video calibration, from before calibration became per match.
Superseded, and **not comparable to current results**: it assigns a different
corner as the origin, so positions computed against it differ from a freshly drawn
field by up to 150 inches. Kept only so old outputs can be interpreted.
