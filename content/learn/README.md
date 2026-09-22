# Writing a Learn guide

A guide is one Markdown file at `content/learn/<section>/<name>.md`. It appears
on the site as soon as the file exists — no build step, no restart needed for
content changes.

Sections live in `categories.yml`. Adding a section is a folder plus an entry
there.

## The header

Every guide starts with a header block between `---` lines:

```yaml
---
title: A drivetrain that doesn't wobble
category: mechanical
summary: One or two sentences, shown on the section page and under the title.
level: new member        # or: experienced, reference
minutes: 9               # rough read time, shown on the card
order: 1                 # lower numbers first; guides without it sort by title
programs: [v5rc]         # [v5rc], [viqrc], or both — decides where it shows
cover: drivetrain/hero.jpg
videos:
  - url: https://www.youtube.com/watch?v=...
    title: Full build walkthrough
---
```

`programs` is what keeps one guide serving both sides of the site. A guide that
is true for V5 and IQ alike — notebook standards, most of programming — lists
both and is written once.

## The body

Normal Markdown: `## headings`, `**bold**`, lists, links, `code`.

Anything visual is a block. There are six.

**A photo with a caption**

```
::: figure drivetrain/bearing.jpg "Alt text, for anyone who cannot see it"
The caption. Markdown works in here.
:::
```

**Two photos side by side**, for a right way and a wrong way:

```
::: compare drivetrain/braced.jpg "Braced" drivetrain/racked.jpg "Unbraced"
What the comparison shows.
:::
```

**Numbered steps**, each with an optional photo of its own:

```
::: steps
1. Build the two side rails first. @photo drivetrain/step-rails.jpg "Two rails"
2. Bolt the cross members loosely.
:::
```

**Something to watch out for**, a tip, or an aside:

```
::: warning
Every shaft needs a bearing at both ends.
:::

::: tip
Pick the layout from the game, not from what looks impressive.
:::

::: note
Rules change between seasons — check the current manual.
:::
```

**A diagram**, drawn as an SVG in `content/learn/diagrams/`:

```
::: diagram wheel-layouts "What the drawing shows"
The caption underneath.
:::
```

Diagrams are inlined into the page rather than linked, so colours written as
`var(--accent)`, `var(--text)` or `var(--line)` inside the SVG follow whichever
program is being shown — one file is red on black for V5 and blue on white for
IQ. Use those tokens rather than fixed colours, or the drawing will be wrong in
one of the two themes.

A diagram is usually better than a photo for a concept — wheel layouts, force
paths, what square means. Photos are better for the real thing: your parts, your
robot, what a finished joint looks like.

**A video**, which loads only when a reader presses play:

```
::: video https://www.youtube.com/watch?v=... "What this shows"
:::
```

A block name that is not one of these is rendered as ordinary prose rather than
dropped, so a typo costs you a layout, never a paragraph.

## Photos

Put them under `webapp/static/learn/`, in a folder per guide, and reference them
without that prefix: `drivetrain/hero.jpg` means
`webapp/static/learn/drivetrain/hero.jpg`.

**A photo that does not exist yet renders as a labelled placeholder naming the
file it wants.** Write the guide first, take the photos afterwards, and the page
tells you exactly what is still missing — the guide page counts them at the
bottom.

Phone photos are far too large to serve directly. Resize them before committing;
a width of about 1600px is plenty.
