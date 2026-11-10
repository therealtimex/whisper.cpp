#!/usr/bin/env python3
"""Report the state of a packaging release (absent, draft, complete, ...).

With --require-complete it exits 1 unless the release is published, has
every asset of the matrix plus the manifest, its manifest reports no problem,
and it was built for this Electron runtime (and --upstream-sha, if given).

With --require-assemblable it exits 1 unless the release is absent or a draft:
published `realtimex-*` releases are immutable, because the app pins them by
tag alone. Publish a rebuilt addon under a new release tag instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import (  # noqa: E402
    ASSEMBLABLE_STATES,
    PACKAGING_REPO,
    STATE_COMPLETE,
    GitHub,
    write_github_output,
)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=PACKAGING_REPO)
    p.add_argument("--tag", required=True)
    p.add_argument("--electron-version", required=True)
    p.add_argument("--upstream-sha", default="")
    p.add_argument("--require-assemblable", action="store_true")
    p.add_argument("--require-complete", action="store_true")
    p.add_argument("--github-output", action="store_true")
    args = p.parse_args(argv)

    state = GitHub().inspect_release(
        args.repo, args.tag, electron_version=args.electron_version, upstream_sha=args.upstream_sha or None
    )
    print(json.dumps(state, indent=2))
    if args.github_output:
        write_github_output({"release_state": state["state"], "release_draft": state["draft"]})
    if args.require_assemblable and state["state"] not in ASSEMBLABLE_STATES:
        print(
            f"::error::Release {args.tag} is already published (state {state['state']}). "
            "Published releases are immutable; dispatch again with a new release_tag.",
            file=sys.stderr,
        )
        return 1
    if args.require_complete and state["state"] != STATE_COMPLETE:
        print(f"::error::Release {args.tag} is {state['state']}, not complete: {state}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
