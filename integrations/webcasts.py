"""Scrape webcast links from `https://events.vex.com/webcasts`.

The VEX Events API has no webcast field (the spec never mentions "webcast",
"stream" or "video"), but this public page lists one link per event, and each
event name ends with its SKU, so joining it to the catalog is exact.

Links only. Nothing is downloaded.

Three things observed on the page shape the code:

- It sits behind Cloudflare. A bare user agent gets a 403 "Just a moment..."
  challenge page; a normal browser user agent gets the table. A challenge page is
  raised as an error rather than parsed as a page with zero webcasts, which
  would look like a successful, empty refresh.
- It is one unpaginated table, and not in date order: past events appear at the
  bottom. Nothing here depends on the order.
- The links vary a lot: of 126 rows, 31 pointed at a specific video, 89 at a
  channel, 5 at a stream page elsewhere (Vimeo, school sites) and 1 at a
  playlist. See `storage/webcasts.py` for how each is labelled.
"""
import datetime
import html
import re

import requests

from storage import webcasts

PAGE_URL = "https://events.vex.com/webcasts"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 40
EXPECTED_HEADERS = ["Event", "Start Date", "End Date", "Webcast"]

_SKU = re.compile(r"\(((?:RE|VE)-[A-Z0-9-]+)\)\s*$", re.I)


class WebcastPageError(RuntimeError):
    pass


def fetch(session=None):
    session = session or requests
    try:
        response = session.get(PAGE_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.RequestException as error:
        raise WebcastPageError(f"could not reach {PAGE_URL}: {error}") from error
    if "Just a moment" in response.text[:2000]:
        raise WebcastPageError(f"{PAGE_URL} answered with a Cloudflare challenge "
                               f"(HTTP {response.status_code}) instead of the table")
    if response.status_code != 200:
        raise WebcastPageError(f"{PAGE_URL} returned HTTP {response.status_code}")
    return response.text


def _text(cell):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell))).strip()


def _date(text):
    try:
        return datetime.datetime.strptime(text.strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def parse(page):
    """Rows of {sku, name, start, end, url}. Raises if the table is not recognisable."""
    tables = re.findall(r"(?is)<table[^>]*>(.*?)</table>", page)
    for table in tables:
        headers = [_text(h) for h in re.findall(r"(?is)<th[^>]*>(.*?)</th>", table)]
        if headers == EXPECTED_HEADERS:
            break
    else:
        raise WebcastPageError("no table with columns "
                               f"{', '.join(EXPECTED_HEADERS)} - the page layout has changed")

    rows, skipped = [], 0
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) != 4:
            continue
        name = _text(cells[0])
        sku = _SKU.search(name)
        links = re.findall(r'href="([^"]+)"', cells[3])
        url = html.unescape(links[0]).strip() if links else _text(cells[3])
        if not sku or not url.lower().startswith(("http://", "https://")):
            skipped += 1
            continue
        rows.append({"sku": sku.group(1).upper(), "name": _SKU.sub("", name).strip(),
                     "start": _date(_text(cells[1])), "end": _date(_text(cells[2])),
                     "url": url})
    if not rows:
        raise WebcastPageError("the webcast table was found but no row could be read")
    return rows, skipped


def refresh(path=webcasts.WEBCAST_FILE, log=print):
    rows, skipped = parse(fetch())
    counts = webcasts.merge(rows, path=path)
    log(f"webcasts: {len(rows)} rows read"
        + (f", {skipped} skipped (no SKU or no link)" if skipped else "")
        + f" - {counts['added']} new, {counts['changed']} changed, "
          f"{counts['unchanged']} unchanged, {counts['total']} stored")
    return counts


def main():
    import argparse

    argparse.ArgumentParser(description="Refresh webcast links from events.vex.com/webcasts").parse_args()
    refresh()


if __name__ == "__main__":
    main()
