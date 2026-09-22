"""Group a team's location into something worth filtering by.

Scouting is mostly local: the teams you will actually face come from your own
corner of the country, and a flat list of 42 US states plus 30-odd countries is
too fine to be useful as a filter. So the United States is split into regions
and everywhere else is grouped whole, by country.

That asymmetry is deliberate rather than US-centric by accident - it mirrors who
a team competes against. Of 3,083 imported teams, 2,003 are in the US spread
over 42 states, while most other countries have a few dozen each; splitting
those further would make buckets of one or two.

States are listed explicitly rather than derived, because the API gives region
as a full state name and nothing guarantees a state is spelled the same way
twice. Anything unrecognised falls into "United States · Other" rather than
being silently dropped, so a new or misspelled state shows up as a visible
bucket instead of vanishing from every filter.
"""
US = "United States"

US_REGIONS = {
    "Pacific Northwest": ("Washington", "Oregon", "Idaho", "Alaska", "Montana"),
    "California & Nevada": ("California", "Nevada", "Hawaii"),
    "Southwest": ("Arizona", "New Mexico", "Texas", "Oklahoma"),
    "Mountain": ("Colorado", "Utah", "Wyoming"),
    "Midwest": ("Minnesota", "Wisconsin", "Iowa", "Missouri", "Illinois", "Indiana",
                "Michigan", "Ohio", "North Dakota", "South Dakota", "Nebraska", "Kansas"),
    "Southeast": ("Tennessee", "Kentucky", "North Carolina", "South Carolina", "Georgia",
                  "Florida", "Alabama", "Mississippi", "Arkansas", "Louisiana",
                  "Virginia", "West Virginia"),
    "Northeast": ("Maine", "New Hampshire", "Vermont", "Massachusetts", "Rhode Island",
                  "Connecticut", "New York", "New Jersey", "Pennsylvania",
                  "Delaware", "Maryland", "District of Columbia"),
}

_STATE_TO_REGION = {state: f"{US} · {region}"
                    for region, states in US_REGIONS.items() for state in states}
US_OTHER = f"{US} · Other"


def group_of(location):
    """The filter bucket for one location.

    US teams get "United States · Pacific Northwest"; everyone else gets their
    country. Returns "Unknown" rather than None so the bucket is selectable -
    a team with no location recorded should still be findable.
    """
    location = location or {}
    country = (location.get("country") or "").strip()
    if not country:
        return "Unknown"
    if country != US:
        return country
    return _STATE_TO_REGION.get((location.get("region") or "").strip(), US_OTHER)


def covers(group, chosen):
    """Does `group` fall under the chosen filter value?

    Exact match, except that the country as a whole covers all of its regions:
    picking "United States" means every US bucket, including "Other".
    """
    if not chosen:
        return True
    if chosen == US:
        return group == US or group.startswith(f"{US} · ")
    return group == chosen


def options(counts):
    """The region menu: [{value, label, count, sub}], US regions under the US.

    The country as a whole is offered alongside its parts because the two
    answer different questions - "who might we meet at nationals" against "who
    do we face most weekends" - and only the second one was selectable before.
    Its count is the sum of its regions, so a menu reading 2,003 for the US and
    120 for the Pacific Northwest is consistent rather than double-counted.
    """
    rows = []
    total = sum(count for group, count in counts.items() if covers(group, US))
    if total:
        rows.append({"value": US, "label": US, "count": total, "sub": False})
    for group, count in counts.items():
        sub = group.startswith(f"{US} · ")
        rows.append({"value": group,
                     "label": group[len(US) + 3:] if sub else group,
                     "count": count, "sub": sub})
    return rows


def sort_key(group):
    """US regions first, then countries A-Z, with Unknown last.

    The US blocks sort together because that is where the filtering actually
    happens; a strict alphabetical list would scatter them among the countries.
    """
    if group == "Unknown":
        return (3, "")
    if group == US:
        return (0, "")
    if group.startswith(f"{US} · "):
        return (0, group)
    return (1, group)


def tally(rows, key=lambda row: row.get("location_raw")):
    """{group: count} over rows, ready for a filter menu."""
    counts = {}
    for row in rows:
        group = group_of(key(row))
        counts[group] = counts.get(group, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: sort_key(item[0])))
