"""Register the matches already on disk in the catalog.

Everything processed before the catalog existed is identified only by a video
and a frame range, spread across three stores. This walks those stores, works
out the distinct segments, and writes a match record for each so the team pages
have something to list.

What it cannot recover is who was playing: a frame range carries no team
numbers, and guessing them would put fabricated data in the store that later
looks like it came from RobotEvents. Imported matches therefore have empty
alliances and are marked `source: "imported"`, waiting for someone to fill them
in or for the API to match them up.

Safe to re-run: segments are keyed by video and frame range, so a second pass
updates the same records rather than duplicating them.
"""
import os

from storage import calibration_store, catalog, seed_store


def discover_segments():
    """Every (video, start, end) any store knows about, grouped by video."""
    segments = {}
    for record in calibration_store.list_all():
        start, end = record["frame_range"]
        segments.setdefault(record["video"], set()).add((int(start), int(end)))
    for record in seed_store.list_all():
        start, end = record.get("frame_range", (None, None))
        if start is not None:
            segments.setdefault(record["video"], set()).add((int(start), int(end)))
    return {video: sorted(ranges) for video, ranges in segments.items()}


def run(directory=catalog.CATALOG_DIR, log=print):
    found = discover_segments()
    if not found:
        log("no calibrations or seeds on disk - nothing to import")
        return []

    imported = []
    for video, ranges in sorted(found.items()):
        stem = os.path.splitext(video)[0]
        # One event per video file. That is a guess, and often a wrong one - a
        # stream can span two events, or an event three streams - but it groups
        # the matches somewhere visible and editable rather than leaving them
        # loose, and the event record is the thing an API import would replace.
        event = catalog.save_event(name=stem, source="imported", directory=directory)
        log(f"{event['key']}: {len(ranges)} segment(s) from {video}")

        for index, (start, end) in enumerate(ranges, start=1):
            match = catalog.save_match(
                event=event["key"], round_slug="unknown", instance=1, number=index,
                video={"file": video, "start_frame": start, "end_frame": end, "url": None},
                source="imported", directory=directory,
            )
            log(f"  {match['name']}  frames {start}-{end}")
            imported.append(match)
    return imported


if __name__ == "__main__":
    run()
