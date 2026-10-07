#!/usr/bin/env python3
"""
Rebuilds the cumulative changelog from past commit messages.

Reads the commits made by the automation (default author:
github-actions[bot]) since a given date, picks out the
"Added N resource packs:" / "Updated N resource packs:" / "Updated N mods:"
sections of each commit message, and merges them -- oldest commit first --
into the same three growing lists that CHANGELOG.txt uses
(see scripts/add_new_resourcepacks.py).

Usage (run from the repo root, after a `git pull`):

    # just print the merged result
    python3 scripts/backfill_changelog.py --since 2026-09-18

    # print AND append it to CHANGELOG.txt (existing entries stay on top)
    python3 scripts/backfill_changelog.py --since 2026-09-18 --write

Pick --since as the day of the first commit you want included. If
CHANGELOG.txt already logged some of those commits, start the day AFTER
the last one it covers -- the merge doesn't dedupe (same as the live
changelog, where a pack added twice is listed twice).

Manual commits (like "update pack.toml") have no such sections and are
ignored. Old lowercase headers ("added 23 resource packs:") are accepted.
"""

import argparse
import re
import subprocess
import sys

from add_new_resourcepacks import (
    CHANGELOG_FILE,
    parse_changelog,
    render_changelog,
)

RECORD_SEP = "\x1e"
_HEADER_FIX_RE = re.compile(r"^(added|updated)(?= \d+ )", re.IGNORECASE)


def commit_messages(since: str, author: str) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "log",
            f"--since={since}",
            f"--author={author}",
            # --author is a regex by default, and "[bot]" would be read as a
            # character class, so match it as plain text instead.
            "--fixed-strings",
            "--reverse",  # oldest first, so lists read chronologically
            f"--format={RECORD_SEP}%B",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"git log failed: {result.stderr.strip()}")
    return [m for m in result.stdout.split(RECORD_SEP) if m.strip()]


def normalize(message: str) -> str:
    """'added 3 resource packs:' -> 'Added 3 resource packs:'"""
    return "\n".join(
        _HEADER_FIX_RE.sub(lambda m: m.group(1).capitalize(), line.strip())
        for line in message.splitlines()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", required=True, help="first day to include, YYYY-MM-DD")
    parser.add_argument("--author", default="github-actions[bot]")
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"append the result to {CHANGELOG_FILE} instead of only printing it",
    )
    args = parser.parse_args()

    # Start from what the file already has (only used with --write).
    existing = ""
    if args.write and CHANGELOG_FILE.exists():
        existing = CHANGELOG_FILE.read_text()
    sections = parse_changelog(existing)

    messages = commit_messages(args.since, args.author)
    for message in messages:
        for key, names in parse_changelog(normalize(message)).items():
            sections.setdefault(key, []).extend(names)

    merged = render_changelog(sections)
    print(f"# {len(messages)} commit(s) read since {args.since}", file=sys.stderr)
    print(merged, end="")

    if args.write:
        CHANGELOG_FILE.write_text(merged)
        print(f"# wrote {CHANGELOG_FILE}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
