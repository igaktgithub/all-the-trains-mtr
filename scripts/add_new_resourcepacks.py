#!/usr/bin/env python3
"""
1. Finds resource packs listed on the MTR Content DB
   (https://addons.minecrafttransitrailway.com/) that are tagged MTR4 and
   not yet part of this modpack, and adds them with `packwiz mr add`.
2. Runs `packwiz refresh` / `packwiz update --all -y` / `packwiz refresh`,
   the same cycle the old update-only workflow ran, so this single
   workflow now covers both jobs.
3. Prints a commit message combining what got added and what got updated.

Data source for step 1 (reverse-engineered from the site's own frontend,
no public docs exist for this API):
  - do_addon.php?act=getall&lang=en
        Full addon list (~900 entries), each with `category` and `tags`.
        Deliberately NOT using do_addon.php?act=get_updates: that endpoint
        is a "recent activity" feed and never surfaces a pack that was
        always MTR4 but predates this automation and never got added --
        getall diffs against the *entire* current list every run, so a
        backlog item is caught on the very next run.

Names are pre-checked against api.modrinth.com before anything is
installed, so a "[Deprecated]"/"[Discontinued]"/"[Superseded]" pack (or
one of the explicitly excluded names) is skipped instead of added.

How "updated" is detected: right after the new packs are added and
`packwiz refresh` has run, everything on disk is staged with `git add -A`.
That means the working tree matches the index exactly. Then
`packwiz update --all -y` + `packwiz refresh` run, and whatever those
commands change is left as *unstaged* changes -- so `git diff --name-only`
at that point shows exactly the files packwiz update touched, with no
overlap with the newly-added files (those are already staged).

Exit behaviour:
  - Prints a single commit message between ::commit-message-start:: and
    ::commit-message-end:: markers for the workflow to extract. The block
    is empty (nothing between the markers) when there was nothing to add
    or update -- the workflow skips the commit in that case.
  - A pack that packwiz refuses to add (e.g. no version compatible with
    this pack's Minecraft version -- this is what naturally excludes
    MTR3-only packs when their builds target an older MC version) is
    logged and skipped; it does not fail the whole run.
"""

import glob
import json
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

BASE_URL = "https://addons.minecrafttransitrailway.com"
MODRINTH_API = "https://api.modrinth.com/v2"
RESOURCEPACKS_DIR = Path("resourcepacks")

# --- Changelog file -------------------------------------------------------
# Toggle to turn the CHANGELOG.txt accumulation on/off without touching any
# other logic.
CHANGELOG_ENABLED = True
# Only runs on/after this date write to CHANGELOG.txt. Earlier runs (and
# anything before this feature existed) are never logged retroactively.
CHANGELOG_START_DATE = date(2026, 9, 2)
CHANGELOG_FILE = Path("CHANGELOG.txt")
# ---------------------------------------------------------------------------
REQUIRED_TAG = "MTR4"
TARGET_CATEGORY = "Resource Pack"

# Exact Modrinth titles to never add, regardless of tags -- maintained by
# hand, add more as needed (one string per line, comma after each).
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


def has_mtr4_tag(tags: list[str]) -> bool:
    """True for 'MTR4' itself or any compound variant like 'MTR4+[JCM]'."""
    return any(tag == REQUIRED_TAG or tag.startswith(f"{REQUIRED_TAG}+") for tag in tags)


def is_excluded(title: str) -> bool:
    if title in EXCLUDED_NAMES:
        return True
    lowered = title.lower()
    return any(keyword in lowered for keyword in DEPRECATED_KEYWORDS)


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "all-the-trains-mtr-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def get_mtr4_resourcepack_ids() -> set[str]:
    """mr_ids of every current MTR4 Resource Pack entry on the site."""
    url = f"{BASE_URL}/do_addon.php?act=getall&lang=en"
    entries = fetch_json(url)
    return {
        e["mr_id"]
        for e in entries
        if e.get("category") == TARGET_CATEGORY and has_mtr4_tag(e.get("tags", []))
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
    result = run(
        ["packwiz", "mr", "add", mr_id, "--meta-folder", "resourcepacks", "-y"]
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
        print(f"::warning::{mr_id} reported success but no new file was found", file=sys.stderr)
        return mr_id

    new_file = next(iter(new_files))
    with open(new_file, "rb") as f:
        data = tomllib.load(f)
    return data.get("name", mr_id)


def add_new_resourcepacks() -> list[str]:
    site_ids = get_mtr4_resourcepack_ids()
    existing = get_existing_modrinth_ids()
    to_add = sorted(site_ids - existing)

    if not to_add:
        print("No new MTR4 resource packs found on the MTR Content DB.")
        return []

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

    return added_names


def changed_pw_toml_files() -> list[str]:
    """.pw.toml files with unstaged changes (working tree vs index)."""
    result = run(["git", "diff", "--name-only"])
    if result.returncode != 0:
        print(f"::warning::git diff failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    return sorted(
        line for line in result.stdout.splitlines() if line.endswith(".pw.toml")
    )


def read_pack_name(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data.get("name")


def update_everything() -> tuple[list[str], list[str]]:
    """Returns (updated_mod_names, updated_resourcepack_names)."""
    # Refresh so index.toml reflects the packs just added, then stage
    # everything so far -- this is what makes the diff below show only
    # what `packwiz update` itself changes.
    run(["packwiz", "refresh"])
    run(["git", "add", "-A"])

    result = run(["packwiz", "update", "--all", "-y"])
    if result.returncode != 0:
        print(f"::warning::packwiz update --all reported an error:\n{result.stderr.strip()}", file=sys.stderr)
    run(["packwiz", "refresh"])

    updated_mods = []
    updated_resourcepacks = []
    for path in changed_pw_toml_files():
        name = read_pack_name(path)
        if not name:
            continue
        print(f"Updated: {name} ({path})")
        if path.startswith("mods/"):
            updated_mods.append(name)
        elif path.startswith("resourcepacks/"):
            updated_resourcepacks.append(name)
    return updated_mods, updated_resourcepacks


def _section(lines: list[str], count: int, verb: str, singular: str, plural: str, names: list[str]) -> None:
    if not names:
        return
    if lines:
        lines.append("")
    noun = singular if count == 1 else plural
    lines.append(f"{verb} {count} {noun}:")
    lines += [f"- {name}" for name in names]


def build_commit_message(
    added_resourcepacks: list[str],
    updated_resourcepacks: list[str],
    updated_mods: list[str],
) -> str:
    lines: list[str] = []
    _section(lines, len(added_resourcepacks), "Added", "resource pack", "resource packs", added_resourcepacks)
    _section(lines, len(updated_resourcepacks), "Updated", "resource pack", "resource packs", updated_resourcepacks)
    _section(lines, len(updated_mods), "Updated", "mod", "mods", updated_mods)
    return "\n".join(lines)


# --- Changelog file ---------------------------------------------------------
# CHANGELOG.txt keeps three running, ever-growing sections (Added resource
# packs / Updated resource packs / Updated mods). Each run appends whatever
# it added/updated to the matching section instead of writing a new dated
# block, so the file always shows one cumulative list per category.

_CHANGELOG_HEADER_RE = re.compile(r"^(Added|Updated) \d+ (resource packs?|mods?):$")

# (verb, singular noun) -> ordering in the file. Also doubles as the set of
# known section keys when parsing an existing file.
_CHANGELOG_SECTIONS = [
    ("Added", "resource pack"),
    ("Updated", "resource pack"),
    ("Updated", "mod"),
]


_NOUN_TO_SINGULAR = {
    "resource pack": "resource pack",
    "resource packs": "resource pack",
    "mod": "mod",
    "mods": "mod",
}


def _changelog_section_key(verb: str, noun: str) -> tuple[str, str]:
    return (verb, _NOUN_TO_SINGULAR[noun])


def parse_changelog(text: str) -> dict[tuple[str, str], list[str]]:
    sections: dict[tuple[str, str], list[str]] = {}
    current: tuple[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _CHANGELOG_HEADER_RE.match(line)
        if match:
            current = _changelog_section_key(match.group(1), match.group(2))
            sections.setdefault(current, [])
            continue
        if line.startswith("- ") and current is not None:
            sections[current].append(line[2:])
    return sections


def render_changelog(sections: dict[tuple[str, str], list[str]]) -> str:
    lines: list[str] = []
    for verb, noun in _CHANGELOG_SECTIONS:
        names = sections.get((verb, noun), [])
        if not names:
            continue
        plural = "resource packs" if noun == "resource pack" else "mods"
        lines.append(f"{verb} {len(names)} {plural}:")
        lines += [f"- {name}" for name in names]
    return "\n".join(lines) + "\n" if lines else ""


def update_changelog(
    added_resourcepacks: list[str],
    updated_resourcepacks: list[str],
    updated_mods: list[str],
) -> None:
    if not CHANGELOG_ENABLED:
        return
    if date.today() < CHANGELOG_START_DATE:
        return
    if not (added_resourcepacks or updated_resourcepacks or updated_mods):
        return  # nothing happened this run -- leave the file untouched

    existing = CHANGELOG_FILE.read_text() if CHANGELOG_FILE.exists() else ""
    sections = parse_changelog(existing)

    sections.setdefault(("Added", "resource pack"), []).extend(added_resourcepacks)
    sections.setdefault(("Updated", "resource pack"), []).extend(updated_resourcepacks)
    sections.setdefault(("Updated", "mod"), []).extend(updated_mods)

    CHANGELOG_FILE.write_text(render_changelog(sections))
# -----------------------------------------------------------------------------


def main() -> int:
    added_resourcepacks = add_new_resourcepacks()
    updated_mods, updated_resourcepacks = update_everything()

    update_changelog(added_resourcepacks, updated_resourcepacks, updated_mods)

    message = build_commit_message(added_resourcepacks, updated_resourcepacks, updated_mods)

    print("::commit-message-start::")
    if message:
        print(message)
    print("::commit-message-end::")

    return 0


if __name__ == "__main__":
    sys.exit(main())
