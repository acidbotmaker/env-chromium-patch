#!/usr/bin/env python3
"""Apply env-driven fingerprint overrides to a Chromium checkout.

The patch is authored without access to the target tree, so it locates each
edit by a verbatim anchor quoted from real Chromium source rather than by line
number. Every anchor must match exactly once; a missing or ambiguous anchor is
reported with the file and the search string so it can be relocated by hand,
and nothing is written until all of them resolve.

Usage:
    python3 apply.py --chromium-src ~/chromium/src --dry-run
    python3 apply.py --chromium-src ~/chromium/src --emit-patch env-fp.patch
    python3 apply.py --chromium-src ~/chromium/src --revert
"""

import argparse
import difflib
import subprocess
import sys
from pathlib import Path

from edits import collect_edits, detect_api_flavors

BACKUP_SUFFIX = ".env-fp.orig"


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def validate_src(src: Path) -> None:
    if not src.is_dir():
        fail(f"{src} is not a directory")
    missing = [p for p in ("chrome/VERSION", "base/environment.h") if not (src / p).exists()]
    if missing:
        fail(f"{src} does not look like a chromium/src checkout (missing {', '.join(missing)})")


def describe_version(src: Path) -> str:
    fields = {}
    for line in (src / "chrome" / "VERSION").read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return ".".join(fields.get(k, "?") for k in ("MAJOR", "MINOR", "BUILD", "PATCH"))


def revert(src: Path) -> int:
    restored = 0
    for backup in src.rglob("*" + BACKUP_SUFFIX):
        target = backup.with_name(backup.name[: -len(BACKUP_SUFFIX)])
        target.write_text(backup.read_text())
        backup.unlink()
        print(f"  restored {target.relative_to(src)}")
        restored += 1
    if not restored:
        print("No backups found; nothing to revert.")
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chromium-src", required=True, type=Path,
                        help="path to the chromium/src checkout")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the diff without writing anything")
    parser.add_argument("--revert", action="store_true",
                        help="restore the .env-fp.orig backups written by a previous run")
    parser.add_argument("--emit-patch", type=Path, metavar="FILE",
                        help="after applying, write `git diff` of the tree to FILE")
    args = parser.parse_args()

    src = args.chromium_src.expanduser().resolve()
    validate_src(src)

    if args.revert:
        print(f"Reverting in {src}")
        revert(src)
        return 0

    print(f"Target: {src}  (Chrome {describe_version(src)})")

    ctx = detect_api_flavors(src)
    from_utf8 = f"String::{ctx['from_utf8_name']}"
    if ctx["from_utf8_needs_span"]:
        from_utf8 += "(span)"
    print("Detected APIs: "
          f"GetVar={'optional' if ctx['optional_getvar'] else 'out-param'}, "
          f"values={ctx['dict_type']}, "
          f"JSONReader::ReadDict={'yes' if ctx['has_read_dict'] else 'no'}, "
          f"utf8={from_utf8}")

    if ctx["from_utf8_needs_span"] and not ctx["has_as_byte_span"]:
        fail("this tree's String::" + ctx["from_utf8_name"] + " only accepts a "
             "byte span, but base::as_byte_span() is not in "
             "base/containers/span.h; teach spell_from_utf8() in "
             "edits/__init__.py how to convert on this milestone")

    edits = collect_edits(ctx)

    # Phase 1: resolve every anchor before touching anything on disk.
    contents: dict = {}
    planned: list = []
    already: list = []
    errors: list = []

    for edit in edits:
        path = src / edit.path
        if not path.exists():
            errors.append(f"{edit.path}: file not found")
            continue
        if edit.path not in contents:
            contents[edit.path] = path.read_text()
        text = contents[edit.path]

        if edit.marker in text:
            already.append(edit)
            continue

        count = text.count(edit.anchor)
        if count != 1:
            first_line = edit.anchor.strip().splitlines()[0][:80]
            errors.append(
                f"{edit.path}: anchor matched {count} times, expected 1\n"
                f"           purpose: {edit.why}\n"
                f"           anchor starts: {first_line!r}"
            )
            continue

        contents[edit.path] = text.replace(edit.anchor, edit.replacement, 1)
        planned.append(edit)

    if errors:
        print(f"\n{len(errors)} anchor(s) did not resolve. Nothing was written.\n",
              file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        print("\nThe surrounding source has probably drifted on this milestone. "
              "Open each file, find the equivalent code, and update the anchor in "
              "the matching edits/*.py module.", file=sys.stderr)
        return 1

    if already:
        print(f"\n{len(already)} edit(s) already applied, skipping.")
    if not planned:
        print("Nothing to do.")
        return 0

    print(f"\n{len(planned)} edit(s) to apply across {len(set(e.path for e in planned))} file(s):")
    for edit in planned:
        print(f"  {edit.path}: {edit.why}")

    if args.dry_run:
        print("\n--- dry run ---")
        for rel, new_text in sorted(contents.items()):
            original = (src / rel).read_text()
            if original == new_text:
                continue
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}",
            )
            sys.stdout.writelines(diff)
        return 0

    # Phase 2: write, backing up each original exactly once.
    for rel, new_text in sorted(contents.items()):
        path = src / rel
        original = path.read_text()
        if original == new_text:
            continue
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if not backup.exists():
            backup.write_text(original)
        path.write_text(new_text)
        print(f"  wrote {rel}")

    print("\nApplied. Build with: autoninja -C out/Release chrome")

    if args.emit_patch:
        result = subprocess.run(
            ["git", "-C", str(src), "diff"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"warning: git diff failed: {result.stderr.strip()}", file=sys.stderr)
        else:
            args.emit_patch.write_text(result.stdout)
            print(f"Wrote {args.emit_patch} ({len(result.stdout.splitlines())} lines)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
