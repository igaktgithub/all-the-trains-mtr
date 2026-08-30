#!/usr/bin/env python3
"""
Finds resource packs listed on the MTR Content DB
(https://addons.minecrafttransitrailway.com/) that are tagged for MTR4 and
are not yet part of this modpack, then adds them with `packwiz mr add`.

Data sources (reverse-engineered from the site's own frontend, no public
docs exist for this API):
  - do_addon.php?act=get_updates&limit=N
        Recently published Modrinth versions, newest first. Cheap to poll
        often, but has no `tags` field, so it can't tell MTR3 from MTR4
        addons on its own.
  - do_addon.php?act=getall&lang=en
        Full addon list (~900 entries), each with `category` and `tags`.
        No usable filter server-side; used here only to look up tags for
        the mr_id candidates found via get_updates.

Exit behaviour:
  - Prints one line per pack it added, and a final summary block matching
    the requested commit-message format on stdout, so the workflow step
    can capture it directly with `... >> "$GITHUB_OUTPUT"`-style redirection
    (see the accompanying workflow for how this is consumed).
  - A pack that packwiz refuses to add (e.g. no version compatible with
    this pack's Minecraft version -- this is what naturally excludes
    MTR3-only packs when their builds target an older MC version) is
    logged and skipped; it does not fail the whole run.
"""

import glob
import json
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

BASE_URL = "https://addons.minecrafttransitrailway.com"
# How far back to look at each run. Bump this (or run the workflow more
# often) if you expect >GET_UPDATES_LIMIT resource-pack + addon-mod
# versions to be published between two runs.
GET_UPDATES_LIMIT = 150
RESOURCEPACKS_DIR = Path("resourcepacks")
REQUIRED_TAG = "MTR4"
TARGET_CATEGORY = "Resource Pack"


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "all-the-trains-mtr-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def get_recent_candidates() -> set[str]:
    """mr_ids for Resource Pack versions published recently."""
    url = f"{BASE_URL}/do_addon.php?act=get_updates&limit={GET_UPDATES_LIMIT}"
    entries = fetch_json(url)
    return {e["mr_id"] for e in entries if e.get("category") == TARGET_CATEGORY}


def get_tag_map() -> dict[str, list[str]]:
    """mr_id -> tags, from the full addon list."""
    url = f"{BASE_URL}/do_addon.php?act=getall&lang=en"
    entries = fetch_json(url)
    return {
        e["mr_id"]: e.get("tags", [])
        for e in entries
        if e.get("category") == TARGET_CATEGORY
    }


def get_existing_modrinth_ids() -> set[str]:
    """mr_ids already tracked as .pw.toml files in resourcepacks/."""
    ids = set()
    for path in glob.glob(str(RESOURCEPACKS_DIR / "*.pw.toml")):
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            continue
        mod_id = data.get("update", {}).get("modrinth", {}).get("mod-id")
        if mod_id:
            ids.add(mod_id)
    return ids


def existing_pw_files() -> set[str]:
    return set(glob.glob(str(RESOURCEPACKS_DIR / "*.pw.toml")))


def add_resourcepack(mr_id: str) -> str | None:
    """Runs `packwiz mr add` for mr_id. Returns the pack's display name on
    success, or None if packwiz rejected it (logged, not fatal)."""
    before = existing_pw_files()
    result = subprocess.run(
        [
            "packwiz",
            "mr",
            "add",
            mr_id,
            "--meta-folder",
            "resourcepacks",
            "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"::warning::Skipped {mr_id}: packwiz declined it "
            f"(likely no version for this pack's MC version -- "
            f"probably MTR3-only)\n{result.stderr.strip()}",
            file=sys.stderr,
        )
        return None

    after = existing_pw_files()
    new_files = after - before
    if not new_files:
        # Shouldn't happen if returncode was 0, but don't crash on it.
        print(f"::warning::{mr_id} reported success but no new file was found", file=sys.stderr)
        return mr_id

    new_file = next(iter(new_files))
    with open(new_file, "rb") as f:
        data = tomllib.load(f)
    return data.get("name", mr_id)


def main() -> int:
    candidates = get_recent_candidates()
    if not candidates:
        print("No recent updates from the MTR Content DB.")
        return 0

    tag_map = get_tag_map()
    existing = get_existing_modrinth_ids()

    to_add = []
    for mr_id in sorted(candidates):
        if mr_id in existing:
            continue
        tags = tag_map.get(mr_id)
        if tags is None:
            print(f"Skipping {mr_id}: not found in the full addon list (removed/hidden?)")
            continue
        if REQUIRED_TAG not in tags:
            print(f"Skipping {mr_id}: not tagged {REQUIRED_TAG} (tags: {tags})")
            continue
        to_add.append(mr_id)

    added_names = []
    for mr_id in to_add:
        name = add_resourcepack(mr_id)
        if name:
            added_names.append(name)
            print(f"Added: {name} ({mr_id})")

    # Machine-readable summary consumed by the workflow to build the commit
    # message. Keep this the last thing printed.
    print("::added-summary-start::")
    print(json.dumps(added_names))
    print("::added-summary-end::")

    return 0


if __name__ == "__main__":
    sys.exit(main())
