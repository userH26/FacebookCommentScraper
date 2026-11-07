#!/usr/bin/env python3
"""
Version Bumper Utility for Facebook Comment Scraper.
Usage:
  python bump_version.py         # Automatically bumps patch (e.g. 2.4.1 -> 2.4.2)
  python bump_version.py minor   # Bumps minor (e.g. 2.4.1 -> 2.5.0)
  python bump_version.py major   # Bumps major (e.g. 2.4.1 -> 3.0.0)
  python bump_version.py 2.5.0   # Sets explicit version
"""

import os
import re
import sys

TARGET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "facebook_scraper.py")


def get_current_version() -> str:
    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
    if not m:
        raise ValueError(f"Could not find __version__ in {TARGET_FILE}")
    return m.group(1)


def bump(current: str, part: str) -> str:
    pieces = current.split(".")
    while len(pieces) < 3:
        pieces.append("0")

    major, minor, patch = int(pieces[0]), int(pieces[1]), int(pieces[2])

    if part == "major":
        return f"{major + 1}.0.0"
    elif part == "minor":
        return f"{major}.{minor + 1}.0"
    elif part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        # Check if argument is a direct version number like 2.5.0
        if re.match(r"^\d+\.\d+\.\d+$", part):
            return part
        raise ValueError(f"Unknown bump target: '{part}'. Choose 'patch', 'minor', 'major', or e.g. '2.5.0'.")


def set_version(new_version: str):
    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = re.sub(
        r'__version__\s*=\s*["\']([^"\']+)["\']',
        f'__version__ = "{new_version}"',
        content,
    )

    with open(TARGET_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    part = sys.argv[1].lower() if len(sys.argv) > 1 else "patch"
    current = get_current_version()
    new_ver = bump(current, part)

    set_version(new_ver)

    print("=" * 60)
    print(f" Version successfully updated: {current} -> {new_ver}")
    print(f" File updated: {TARGET_FILE}")
    print("=" * 60)
    print(" Next steps:")
    print(f'   git commit -am "Bump version to v{new_ver}"')
    print("   git push")
    print(f" GitHub Actions will automatically tag and publish release v{new_ver}!")
    print("=" * 60)


if __name__ == "__main__":
    main()
