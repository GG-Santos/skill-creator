#!/usr/bin/env python3
"""Verify a .skill archive without extracting or executing it."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import zipfile

from package_skill import PackageError, verify_archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify .skill paths, limits, manifest hashes, and root metadata."
    )
    parser.add_argument("archive")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)
    try:
        result = verify_archive(args.archive)
    except (OSError, PackageError, zipfile.BadZipFile) as exc:
        if args.json:
            print(json.dumps({"passed": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"Error: {exc}")
        return 1
    payload = {"passed": True, **asdict(result)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
