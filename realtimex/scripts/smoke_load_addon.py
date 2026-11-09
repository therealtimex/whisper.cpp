#!/usr/bin/env python3
"""Load the packaged addon in a JavaScript runtime and list its exports.

This catches binaries that build but cannot load (missing symbols or system
libraries, wrong architecture) before they are published. The addon uses
N-API, which is ABI-stable across Node and Electron, so CI loads it with the
job's Node (Electron on headless Linux runners needs GTK libraries the arm64
image may lack). Pass an Electron executable as --runtime to load it there;
ELECTRON_RUN_AS_NODE is set for that case.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import ADDON_NAME  # noqa: E402

LOADER = (
    "const m = require(require('path').resolve(process.argv[1]));"
    "const keys = Object.keys(m).sort();"
    "if (!keys.length) { console.error('addon exports nothing'); process.exit(2); }"
    "console.log('exports: ' + keys.join(', '));"
)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zip", required=True)
    p.add_argument("--runtime", default="node", help="node, or an Electron executable")
    args = p.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(args.zip) as archive:
            archive.extract(ADDON_NAME, tmp)
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        proc = subprocess.run(
            [args.runtime, "-e", LOADER, os.path.join(tmp, ADDON_NAME)],
            env=env, capture_output=True, text=True, timeout=120, check=False,
        )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print(f"::error::{os.path.basename(args.zip)} failed to load in {args.runtime} (exit {proc.returncode})",
              file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
