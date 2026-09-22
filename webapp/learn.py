"""The Learn tab's guides: Markdown with a few named blocks.

A guide is one file under `content/learn/<category>/<slug>.md`, in the repo
rather than in `data/`, because guides are written work: they want version
history, review before they go live, and to travel with the code.

Plain Markdown was not enough. A build guide written as paragraphs with images
dropped between them is a wall of text, which is the one thing a guide for a
new member cannot be. So prose is Markdown and anything visual is a block:

    ::: figure drivetrain/bearing.jpg "A bearing block at each end of the shaft"
    A shaft held at one end flexes under load.
    :::

Blocks are parsed here and rendered by macros in `_learn_blocks.html`, which
use the same CSS tokens as the rest of the site - so a guide picks up V5RC's
black and red or VIQRC's white and blue without a second stylesheet.

A short list of block types, deliberately: `figure`, `compare`, `steps`,
`warning`, `tip`, `note`, `video` and `diagram`. An author picks a shape
instead of inventing a layout, and every guide comes out looking like the same
site.

A referenced photo that does not exist yet renders as a labelled placeholder
naming the file it wants, so a guide can be written before the build photos
are taken - which is the order those two things actually happen in.
"""
import os
import re
import shlex

import markdown
import yaml

CONTENT_DIR = os.path.join("content", "learn")
IMAGE_DIR = os.path.join("webapp", "static", "learn")
IMAGE_URL = "/static/learn"
DIAGRAM_DIR = os.path.join(CONTENT_DIR, "diagrams")

BLOCK_START = re.compile(r"^:::\s*(\w+)\s*(.*)$")
BLOCK_END = "::: "
LEVELS = ("new member", "experienced", "reference")

# `extra` covers tables and fenced code; `sane_lists` stops a numbered list
# restarting at 1 whenever a paragraph interrupts it.
_markdown = markdown.Markdown(extensions=["extra", "sane_lists", "toc"])


def _render(text):
    _markdown.reset()
    return _markdown.convert((text or "").strip())


def _inline(text):
    """Markdown for a caption: rendered, then unwrapped from its <p>."""
    html = _render(text)
    if html.startswith("<p>") and html.endswith("</p>") and html.count("<p>") == 1:
        html = html[3:-4]
    return html


def categories(path=CONTENT_DIR):
    try:
        with open(os.path.join(path, "categories.yml")) as handle:
            rows = yaml.safe_load(handle) or []
    except OSError:
        return []
    return [row for row in rows if row.get("slug")]


def _image(reference):
    """One image reference, resolved against webapp/static/learn."""
    reference = str(reference or "").strip().lstrip("/")
    if not reference:
        return None
    stem, extension = os.path.splitext(reference)
    thumb = f"{stem}.thumb{extension}"
    on_disk = os.path.exists(os.path.join(IMAGE_DIR, reference))
    return {
        "path": reference,
        "src": f"{IMAGE_URL}/{reference}",
        "thumb": (f"{IMAGE_URL}/{thumb}"
                  if os.path.exists(os.path.join(IMAGE_DIR, thumb))
                  else f"{IMAGE_URL}/{reference}"),
        "missing": not on_disk,
    }


def video_id(url):
    """The YouTube id in a watch, short or embed URL."""
    found = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", str(url or ""))
    return found.group(1) if found else None


def _video(url, title=None, caption=None):
    identifier = video_id(url)
    return {
        "kind": "video",
        "url": url,
        "title": title or "Video walkthrough",
        "id": identifier,
        # Loaded only when someone presses play, so a guide page makes no
        # request to YouTube until it is asked to.
        "embed": f"https://www.youtube-nocookie.com/embed/{identifier}?autoplay=1" if identifier else None,
        "poster": f"https://i.ytimg.com/vi/{identifier}/hqdefault.jpg" if identifier else None,
        "caption": _inline(caption) if caption else None,
    }


def _diagram(name, title=None, caption=None):
    """An SVG from content/learn/diagrams, inlined rather than linked.

    Inlined because an <img> cannot see the page's stylesheet: a linked SVG
    would need its colours baked in, and would then be wrong in one of the two
    themes. Inlined, `var(--accent)` inside the drawing resolves against
    whichever program is being shown, so one file is red on black for V5RC and
    blue on white for VIQRC.
    """
    slug = re.sub(r"[^a-z0-9_-]", "", str(name or "").lower())
    target = os.path.join(DIAGRAM_DIR, f"{slug}.svg") if slug else None
    svg = None
    if target and os.path.exists(target):
        with open(target) as handle:
            svg = handle.read()
    return {"kind": "diagram", "name": f"{slug}.svg", "svg": svg, "title": title or "",
            "caption": _inline(caption) if caption else None, "missing": svg is None}


def _steps(body):
    """A numbered list where an item may carry its own photo.

        1. Cut two 25-hole C-channels. @photo drivetrain/step-1.jpg "Two cut rails"
    """
    items = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        text = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line)
        photo = None
        found = re.search(r"@photo\s+(\S+)(?:\s+\"([^\"]*)\")?", text)
        if found:
            photo = _image(found.group(1))
            if photo:
                photo["alt"] = found.group(2) or ""
            text = text[:found.start()].strip()
        items.append({"html": _inline(text), "photo": photo})
    return items


def _block(name, arguments, body):
    """One ::: block, as a node the template knows how to draw."""
    try:
        words = shlex.split(arguments or "")
    except ValueError:                          # an unbalanced quote
        words = (arguments or "").split()

    if name == "figure":
        image = _image(words[0] if words else "")
        if image:
            image["alt"] = words[1] if len(words) > 1 else ""
        return {"kind": "figure", "image": image, "caption": _inline(body)}

    if name == "compare":
        left, right = _image(words[0] if words else ""), _image(words[2] if len(words) > 2 else "")
        for image, label in ((left, words[1] if len(words) > 1 else "Before"),
                             (right, words[3] if len(words) > 3 else "After")):
            if image:
                image["alt"] = label
                image["label"] = label
        return {"kind": "compare", "left": left, "right": right, "caption": _inline(body)}

    if name == "steps":
        return {"kind": "steps", "steps": _steps(body)}

    if name in ("warning", "tip", "note"):
        return {"kind": "callout", "tone": name, "html": _render(body)}

    if name == "diagram":
        return _diagram(words[0] if words else "", words[1] if len(words) > 1 else None, body)

    if name == "video":
        return _video(words[0] if words else "", words[1] if len(words) > 1 else None, body)

    # An unknown block is shown as prose rather than silently dropped: losing a
    # paragraph because of a typo in its name would be invisible to the author.
    return {"kind": "prose", "html": _render(body), "unknown": name}


def parse(text):
    """(front matter, [node]) for one guide's file contents."""
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            body = parts[2]

    nodes, prose, block = [], [], None
    for line in body.splitlines():
        if block is None:
            start = BLOCK_START.match(line)
            if start:
                if "".join(prose).strip():
                    nodes.append({"kind": "prose", "html": _render("\n".join(prose))})
                prose = []
                block = {"name": start.group(1), "arguments": start.group(2), "lines": []}
                continue
            prose.append(line)
        elif line.strip() == ":::":
            nodes.append(_block(block["name"], block["arguments"], "\n".join(block["lines"])))
            block = None
        else:
            block["lines"].append(line)

    if block is not None:                       # unterminated, kept rather than lost
        nodes.append(_block(block["name"], block["arguments"], "\n".join(block["lines"])))
    if "".join(prose).strip():
        nodes.append({"kind": "prose", "html": _render("\n".join(prose))})
    return meta, nodes


def _guide_path(category, slug, path=CONTENT_DIR):
    # Both parts come from the URL, so anything with a separator in it is
    # refused rather than resolved - a guide lives exactly one level down.
    for part in (category, slug):
        if not part or "/" in part or "\\" in part or part.startswith("."):
            return None
    return os.path.join(path, category, f"{slug}.md")


def load(category, slug, path=CONTENT_DIR):
    """One guide, parsed and ready to render, or None."""
    target = _guide_path(category, slug, path)
    if not target or not os.path.exists(target):
        return None
    with open(target) as handle:
        meta, nodes = parse(handle.read())
    return _describe(meta, category, slug, nodes)


def _describe(meta, category, slug, nodes=None):
    videos = [_video(v.get("url"), v.get("title")) for v in (meta.get("videos") or [])
              if isinstance(v, dict) and v.get("url")]
    cover = _image(meta.get("cover")) if meta.get("cover") else None
    return {
        **meta,
        "category": category,
        "slug": slug,
        "url": f"/learn/{category}/{slug}",
        "title": meta.get("title") or slug.replace("-", " ").capitalize(),
        "summary": meta.get("summary") or "",
        "level": (meta.get("level") or "").lower(),
        "minutes": meta.get("minutes"),
        "programs": [str(p).lower() for p in (meta.get("programs") or ["v5rc"])],
        "cover": cover,
        "videos": videos,
        "nodes": nodes or [],
        # The cover counts too, or the page says "6 photos to add" under seven
        # placeholders.
        "photos_missing": (1 if cover and cover.get("missing") else 0)
        + sum(1 for node in (nodes or []) for image in _images_of(node)
              if image and image.get("missing"))
        + sum(1 for node in (nodes or []) if node["kind"] == "diagram" and node["missing"]),
    }


def _images_of(node):
    if node["kind"] == "figure":
        return [node.get("image")]
    if node["kind"] == "compare":
        return [node.get("left"), node.get("right")]
    if node["kind"] == "steps":
        return [step.get("photo") for step in node["steps"]]
    return []


def in_category(category, program=None, path=CONTENT_DIR):
    """Every guide in one category, in `order` then title order."""
    directory = os.path.join(path, category)
    if not os.path.isdir(directory):
        return []
    guides = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        guide = load(category, name[:-3], path)
        if guide and (program is None or program in guide["programs"]):
            guides.append(guide)
    return sorted(guides, key=lambda g: (g.get("order", 100), g["title"].lower()))


def index(program=None, path=CONTENT_DIR):
    """The categories, each with its guides attached."""
    rows = []
    for category in categories(path):
        guides = in_category(category["slug"], program, path)
        rows.append({**category, "guides": guides, "count": len(guides)})
    return rows
