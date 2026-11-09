#!/usr/bin/env python3
"""Package a built whisper_addon.node into the release zip the app downloads.

Zip layout (unchanged contract for the app's download-prebuilt-whisper-addon
scripts): `whisper_addon.node` plus `_realtimexWhisperAddonMetadata.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform as host_platform
import sys
import zipfile
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import ADDON_NAME, METADATA_NAME, UPSTREAM_REPO, asset_name  # noqa: E402


def find_addon(build_dir: pathlib.Path) -> pathlib.Path:
    matches = sorted(build_dir.glob(f"**/{ADDON_NAME}"))
    if not matches:
        raise SystemExit(f"missing {ADDON_NAME} under {build_dir}")
    if len(matches) > 1:
        print("Found several addons, packaging the first:", *matches, sep="\n  ")
    return matches[0]


def package(
    *,
    addon: pathlib.Path,
    out_dir: pathlib.Path,
    platform: str,
    arch: str,
    node_arch: str,
    electron_version: str,
    upstream_ref: str,
    upstream_sha: str,
    packaging_repo: str,
    packaging_sha: str,
    macos_deployment_target: Optional[str] = None,
) -> pathlib.Path:
    data = addon.read_bytes()
    name = asset_name(platform, arch, electron_version)
    metadata: Dict[str, object] = {
        "asset": name,
        "electronRuntimeVersion": electron_version,
        "platform": platform,
        "arch": arch,
        "nodeArch": node_arch,
        "upstreamRepository": UPSTREAM_REPO,
        "upstreamRef": upstream_ref,
        "upstreamSha": upstream_sha,
        "packagingRepository": packaging_repo,
        "packagingSha": packaging_sha,
        "runnerOs": host_platform.system(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if platform == "mac" and macos_deployment_target:
        metadata["macosDeploymentTarget"] = macos_deployment_target

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / name
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(ADDON_NAME, data)
        archive.writestr(METADATA_NAME, json.dumps(metadata, indent=2) + "\n")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--build-dir", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--platform", required=True, choices=["mac", "linux", "win"])
    p.add_argument("--arch", required=True, choices=["x64", "arm64"])
    p.add_argument("--node-arch", required=True)
    p.add_argument("--electron-version", required=True)
    p.add_argument("--upstream-ref", required=True)
    p.add_argument("--upstream-sha", required=True)
    p.add_argument("--packaging-repo", required=True)
    p.add_argument("--packaging-sha", required=True)
    p.add_argument("--macos-deployment-target", default="")
    args = p.parse_args(argv)

    out = package(
        addon=find_addon(pathlib.Path(args.build_dir)),
        out_dir=pathlib.Path(args.out_dir),
        platform=args.platform,
        arch=args.arch,
        node_arch=args.node_arch,
        electron_version=args.electron_version,
        upstream_ref=args.upstream_ref,
        upstream_sha=args.upstream_sha,
        packaging_repo=args.packaging_repo,
        packaging_sha=args.packaging_sha,
        macos_deployment_target=args.macos_deployment_target or None,
    )
    print(f"Packaged {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
