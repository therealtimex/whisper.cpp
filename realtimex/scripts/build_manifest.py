#!/usr/bin/env python3
"""Write whisper-addon-manifest.json for a set of packaged addon zips.

The manifest lists every asset with its size, sha256 and packaging metadata,
plus `expectedAssetNames` and `complete`, so the watcher can tell a finished
release from a partial one. With --require-complete it exits 1 unless every
platform in the release matrix is present and no asset has a problem (built
from another upstream SHA or Electron runtime, or not in the matrix).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import zipfile
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import (  # noqa: E402
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    METADATA_NAME,
    UPSTREAM_REPO,
    expected_asset_names,
    write_github_output,
)


def build_manifest(
    dist_dir: pathlib.Path,
    *,
    release_tag: str,
    electron_version: str,
    upstream_ref: str,
    upstream_sha: str,
    packaging_repo: str,
    packaging_sha: str,
) -> Dict[str, Any]:
    expected = expected_asset_names(electron_version)
    assets: List[Dict[str, Any]] = []
    problems: List[str] = []
    for path in sorted(dist_dir.glob("whisper-addon-*.zip")):
        if path.name not in expected:
            problems.append(f"unexpected asset {path.name}")
            continue
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read(METADATA_NAME)) if METADATA_NAME in archive.namelist() else {}
        if metadata.get("upstreamSha") != upstream_sha:
            problems.append(f"{path.name} was built from {metadata.get('upstreamSha')}, not {upstream_sha}")
        if metadata.get("electronRuntimeVersion") != electron_version:
            problems.append(f"{path.name} targets Electron {metadata.get('electronRuntimeVersion')}")
        data = path.read_bytes()
        assets.append({
            "name": path.name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "metadata": metadata,
        })
    present = {a["name"] for a in assets}
    missing = [n for n in expected if n not in present]
    assets.sort(key=lambda a: expected.index(a["name"]))
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "releaseTag": release_tag,
        "electronRuntimeVersion": electron_version,
        "upstreamRepository": UPSTREAM_REPO,
        "upstreamRef": upstream_ref,
        "upstreamSha": upstream_sha,
        "packagingRepository": packaging_repo,
        "packagingSha": packaging_sha,
        "expectedAssetNames": expected,
        "missingAssetNames": missing,
        "complete": not missing and not problems,
        "problems": problems,
        "assets": assets,
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dist-dir", required=True)
    p.add_argument("--release-tag", required=True)
    p.add_argument("--electron-version", required=True)
    p.add_argument("--upstream-ref", required=True)
    p.add_argument("--upstream-sha", required=True)
    p.add_argument("--packaging-repo", required=True)
    p.add_argument("--packaging-sha", required=True)
    p.add_argument("--out", default="")
    p.add_argument("--require-complete", action="store_true")
    p.add_argument("--github-output", action="store_true")
    args = p.parse_args(argv)

    dist = pathlib.Path(args.dist_dir)
    manifest = build_manifest(
        dist,
        release_tag=args.release_tag,
        electron_version=args.electron_version,
        upstream_ref=args.upstream_ref,
        upstream_sha=args.upstream_sha,
        packaging_repo=args.packaging_repo,
        packaging_sha=args.packaging_sha,
    )
    out = pathlib.Path(args.out) if args.out else dist / MANIFEST_NAME
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}: {len(manifest['assets'])} assets, complete={manifest['complete']}")
    for problem in manifest["problems"]:
        print(f"::error::{problem}", file=sys.stderr)
    if manifest["missingAssetNames"]:
        print(f"Missing: {', '.join(manifest['missingAssetNames'])}", file=sys.stderr)
    if args.github_output:
        write_github_output({"complete": manifest["complete"], "missing_count": len(manifest["missingAssetNames"])})
    if args.require_complete and not manifest["complete"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
