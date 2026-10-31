#!/usr/bin/env python3
"""Run the adjacent, versioned Markdown library package without a global install."""
from pathlib import Path
import sys


def main():
    project = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(project / "src"))
    from markdown_library.cli import main as library_main
    return library_main()


if __name__ == "__main__":
    raise SystemExit(main())
