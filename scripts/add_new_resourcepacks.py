#!/usr/bin/env python3
"""
Finds resource packs listed on the MTR Content DB
(https://addons.minecrafttransitrailway.com/) that are tagged for MTR4 and
are not yet part of this modpack, then adds them with `packwiz mr add`.

Data source (reverse-engineered from the site's own frontend, no public
docs exist for this API):
  - do_addon.php?act=getall&lang=en
        Full addon list (~900 entries), each with `category` and `tags`.
        Deliberately NOT using do_addon.php?act=get_updates here: that
        endpoint is a "recent activity" feed (Modrinth versions published
        in roughly the last N publishes, across all categories) -- it
        never surfaces a resource pack that was always MTR4 but simply
        predates this automation and never got added, since nothing about
        it would be "recent". getall has no such blind spot: every run
        diffs against the *entire* current MTR4 resource-pack list, so a
        backlog item is caught on the very next run instead of waiting
        for its author to publish an update.

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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://addons.minecrafttransitrailway.com"
MODRINTH_API = "https://api.modrinth.com/v2"
RESOURCEPACKS_DIR = Path("resourcepacks")
REQUIRED_TAG = "MTR4"
TARGET_CATEGORY = "Resource Pack"

# Exact Modrinth titles to never add, regardless of tags -- maintained by
# hand, add to this as needed.
EXCLUDED_NAMES = {
    "Leah's Cheesy Resources",
    "Rekon Sound Library",
    "Ceru's Sound Library (MTR Mod)",
}

# Case-insensitive substrings that mark a pack as no longer wanted (old/
# unmaintained versions the DB keeps around for history). Matched against
# the Modrinth title, e.g. "[Deprecated] ...", "... (Discontinued-...)",
# "[SUPERSEDED] ...".
DEPRECATED_KEYWORDS = ("deprecated", "discontinued", "superseded", "abandoned")


def is_excluded(title: str) -> bool:
    if title in EXCLUDED_NAMES:
        return True
    lowered = title.lower()
    return any(keyword in lowered for keyword in DEPRECATED_KEYWORDS)


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "all-the-trains-mtr-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def get_mtr4_resourcepack_ids() -> set[str]:
    """mr_ids of every current MTR4 Resource Pack entry on the site."""
    url = f"{BASE_URL}/do_addon.php?act=getall&lang=en"
    entries = fetch_json(url)
    return {
        e["mr_id"]
        for e in entries
        if e.get("category") == TARGET_CATEGORY and REQUIRED_TAG in e.get("tags", [])
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


def get_modrinth_title(mr_id: str) -> str | None:
    url = f"{MODRINTH_API}/project/{urllib.parse.quote(mr_id)}"
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError as exc:
        print(f"::warning::Could not look up title for {mr_id}: {exc}", file=sys.stderr)
        return None
    return data.get("title")


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
    site_ids = get_mtr4_resourcepack_ids()
    existing = get_existing_modrinth_ids()
    to_add = sorted(site_ids - existing)

    if not to_add:
        print("No new MTR4 resource packs found -- pack is up to date.")
        return 0

    print(f"{len(to_add)} candidate(s) not yet in the pack: {to_add}")

    added_names = []
    for mr_id in to_add:
        title = get_modrinth_title(mr_id)
        if title is None:
            print(f"Skipping {mr_id}: could not fetch its Modrinth title, skipping to be safe")
            continue
        if is_excluded(title):
            print(f"Skipping {mr_id}: excluded by name ({title!r})")
            continue

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
