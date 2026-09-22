---
title: A drivetrain that doesn't wobble
category: mechanical
summary: The drivetrain carries every other mechanism, so its faults become everyone's faults. Square frame, supported shafts, sensible gearing.
level: new member
minutes: 9
order: 1
programs: [v5rc]
cover: drivetrain/hero.jpg
videos: []
---

Everything else on the robot is bolted to the drivetrain. A lift mounted to a frame
that flexes will not line up twice in a row, and an intake that sits a quarter inch
low will push game pieces instead of taking them. When a robot "just doesn't score
reliably", the drivetrain is worth checking before the mechanism people are blaming.

Three things decide whether a drivetrain is any good: **is it square**, **is every
shaft supported at both ends**, and **is it geared for what the game asks**. The rest
is detail.

## Pick a wheel layout

::: diagram wheel-layouts "Tank, mecanum and X-drive, seen from above"
Three layouts you will see across a field. Most robots are tank, and most robots
should be.
:::

**Tank** — wheels on each side driven together, turning by driving the sides at
different speeds. Simple, strong, and hard to push sideways if some of the wheels
are traction wheels. This is the default and it wins a lot of matches.

**Mecanum** — rollers set at 45° let the robot strafe sideways. Handy for lining up
on a goal, but the rollers give up pushing power, which matters any time another
robot leans on you.

**X-drive** — four omnis at 45°, moving in any direction and rotating at the same
time. Fast and very agile, and the worst of the three at winning a shoving match.

::: tip
Pick the layout from the game, not from what looks impressive. If the game rewards
holding a position against another robot, traction wheels and tank beat strafing
almost every time.
:::

## Make it square, and prove it

A frame that looks square and isn't will fight itself. The wheels point in slightly
different directions, the robot pulls to one side, and the motors burn current
dragging tyres sideways.

::: steps
1. Build the two side rails first and check they are the same length, hole for hole. @photo drivetrain/step-rails.jpg "Two C-channel side rails laid together"
2. Bolt the front and rear cross members loosely, so the frame can still move. @photo drivetrain/step-cross.jpg "Cross member bolted loosely at the corner"
3. Measure both diagonals, corner to corner. Equal diagonals mean a square frame.
4. Nudge the frame until the diagonals match, then tighten every corner.
5. Re-check the diagonals after tightening — corners move as they are done up.
:::

::: diagram chassis-diagonals "Equal diagonals on a square frame, unequal on a skewed one"
This is the whole test. You do not need a square, and you cannot trust your eyes.
:::

::: diagram braced-vs-racked "A braced frame holding its shape, an unbraced one racking over"
Push the same chassis from the corner with and without a brace. A rectangle with
four bolted corners is a set of hinges; a triangle is not.
:::

## Support every shaft at both ends

This is the single most common build fault, and it is invisible until the robot is
under load: a shaft carried by one bearing flexes, the gear it drives skips teeth,
and the robot loses drive on one side halfway through a match.

::: diagram shaft-support "A shaft held by two bearings against one held at a single end"
The unsupported end drops under load, and the gear tilts with it.
:::

::: warning
Every shaft needs a bearing at **both** ends. A shaft running through a bare hole in
a C-channel wears the metal into an oval, and the play it develops never comes back
out.
:::

Screw joints and standoffs are what stop long rails bowing. If you can twist the
frame by hand, the robot will twist harder than that when another robot hits it.

## Choose a ratio you can defend

The motor cartridge sets the base speed — 100, 200 or 600 RPM — and the gears or
sprockets between motor and wheel change it from there. Free speed works out as:

`speed (in/s) = RPM × π × wheel diameter (in) ÷ 60`

So 200 RPM on a 3.25" wheel is about 34 in/s with no external gearing. That is a
number to check against the field: how long is the longest drive you make in a
match, and how many times do you make it?

Geared faster, the robot crosses the field sooner and loses pushing power. Geared
slower, it wins shoving matches and arrives late. Neither is correct on its own —
the game decides, and the honest way to settle it is to time both on a practice
field rather than argue about it.

::: note
Motor count and gearing rules change between seasons. Check the current game manual
before committing to a drive layout, rather than copying last year's robot.
:::

## Test it before you build on top of it

- Drive it straight across the field. A robot that curves has a square problem, a
  ratio problem, or a loose wheel.
- Push another robot, or a wall. Listen for gears skipping.
- Drive it for a full match length, then feel the motors. Hot motors mean the drive
  is fighting something.

Fixing a drivetrain after three mechanisms are bolted to it costs an afternoon.
Fixing it now costs twenty minutes.
