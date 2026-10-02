"""Load statuses from the standalone parts index's data.json.

    python -m tools.import_parts ~/steamoji_vex/part_index/data.json

Writes to whichever store the environment selects (see
storage/parts_inventory.py): the local file by default, or the live Redis when
KV_REST_API_URL and KV_REST_API_TOKEN are set - which is how to seed the
hosted site. Logged under the name "import".

Custom parts the old page let people add are not imported: the list is
webapp/parts.py now. They are printed, so any worth keeping can be added there.
"""
import argparse
import json

from storage import parts_inventory
from webapp import parts


def main():
    parser = argparse.ArgumentParser(description="Import parts statuses from the standalone index")
    parser.add_argument("path", help="the old index's data.json")
    args = parser.parse_args()

    with open(args.path) as handle:
        data = json.load(handle)

    changes, skipped = {}, []
    for pid, value in (data.get("state") or {}).items():
        value = "have" if value is True else value     # the old page's v1 format
        if pid in parts.IDS and value in parts.STATES:
            changes[pid] = value
        else:
            skipped.append(pid)

    kind = parts_inventory.backend()
    if kind is None:
        raise SystemExit("no parts storage configured - set KV_REST_API_URL and KV_REST_API_TOKEN")
    parts_inventory.apply(changes, "import")
    print(f"imported {len(changes)} statuses into {kind}")
    if skipped:
        print(f"skipped {len(skipped)} not on the parts list: {', '.join(skipped)}")
    for custom in data.get("customParts") or []:
        print(f"custom part not imported: {custom.get('cat')} / {custom.get('name')}")


if __name__ == "__main__":
    main()
