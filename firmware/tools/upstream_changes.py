#!/usr/bin/env python3
"""What upstream changed in the stock Voice PE config, to port into ours.

Our firmware config (home-assistant-voice.realtime.yaml) is a full copy of
upstream's stock config (home-assistant-voice.yaml) with our changes; an
overlay on upstream's config can't express them (ESPHome joins a component's
action lists rather than replacing them). So upstream's later changes are
ported by hand, and this lists them: upstream's stock config at two of its
release tags, compared item by item (a top-level section, or one of its list
items, by id), against ours.

    git fetch esphome --tags
    python3 firmware/tools/upstream_changes.py 26.9.0 26.10.0

For each item upstream changed it prints upstream's diff, and says what to do:
"copy" if ours is still upstream's old version, "already in ours" if ours is its
new version, or "merge by hand" if ours differs from both.
"""
import difflib
import re
import subprocess
import sys
from pathlib import Path

STOCK = "home-assistant-voice.yaml"
OURS = Path(__file__).resolve().parent.parent / "home-assistant-voice.realtime.yaml"


def items(text: str) -> dict[str, dict[str, list[str]]]:
    """{section: {item: lines}}: list items named by their id, the rest by position."""
    sections: dict[str, dict[str, list[str]]] = {}
    section = key = None
    for line in text.split("\n"):
        if m := re.match(r"^([a-z_0-9]+):", line):
            section, key = m.group(1), "(section)"
            sections[section] = {key: [line]}
            continue
        if section is None:
            continue
        if line.startswith("  - "):
            key = f"(item {len(sections[section])})"
            sections[section][key] = []
        sections[section][key].append(line)
    named: dict[str, dict[str, list[str]]] = {}
    for section, its in sections.items():
        named[section] = {}
        for key, lines in its.items():
            for line in lines[:8]:
                if m := re.match(r"^\s*-?\s*id:\s*(\S+)", line):
                    key = m.group(1)
                    break
            named[section][key] = [
                re.sub(r"\s+#.*$", "", l) for l in lines if l.strip() and not l.strip().startswith("#")
            ]
    return named


def stock_at(tag: str) -> str:
    return subprocess.run(["git", "show", f"{tag}:{STOCK}"], capture_output=True, text=True, check=True).stdout


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    old, new, ours = items(stock_at(sys.argv[1])), items(stock_at(sys.argv[2])), items(OURS.read_text())
    changes = 0
    for section in sorted(set(old) | set(new)):
        before, after, mine = old.get(section, {}), new.get(section, {}), ours.get(section, {})
        for item in sorted(set(before) | set(after)):
            if before.get(item) == after.get(item):
                continue
            changes += 1
            if mine.get(item) == after.get(item):
                action = "already in ours"
            elif mine.get(item) == before.get(item):
                action = "copy"
            else:
                action = "** merge by hand: ours changed it too **"
            print(f"\n=== {section} / {item}: {action}")
            diff = difflib.unified_diff(before.get(item, []), after.get(item, []), lineterm="", n=2)
            print("\n".join(l for l in diff if not l.startswith(("---", "+++"))))
    print(f"\n{changes} upstream change(s) between {sys.argv[1]} and {sys.argv[2]}")


if __name__ == "__main__":
    main()
