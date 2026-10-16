#!/usr/bin/env python3
"""Regenerate the template repository from `oxbrook new`.

    python tools/template.py ../oxbrook-template

The template repository on GitHub is what `oxbrook new` writes, and nothing
else, so the two can never differ: this replaces everything in a checkout of
it except `.git` with a fresh `oxbrook new`, made by the Oxbrook installed in
the running interpreter. Run it with the release just published installed,
since the project pins that minor version, then review the diff and commit.

The project is named `oxbrook-app`: a name for someone to change, and a
valid one until they do.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

NAME = "oxbrook-app"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("checkout", type=Path, help="a clone of the template repository")
    args = parser.parse_args()
    checkout: Path = args.checkout

    if not (checkout / ".git").exists():
        print(f"{checkout} is not a git checkout; clone the template repository first",
              file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        made = Path(tmp) / NAME
        result = subprocess.run([sys.executable, "-m", "oxbrook", "new", str(made)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout + result.stderr, file=sys.stderr)
            return result.returncode

        for entry in checkout.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        shutil.copytree(made, checkout, dirs_exist_ok=True)

    print(f"{checkout} now holds `oxbrook new` from oxbrook {version('oxbrook')}.")
    print(f"Review it with `git -C {checkout} status`, then commit and push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
