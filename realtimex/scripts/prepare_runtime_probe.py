#!/usr/bin/env python3
"""Prepare a runtime_probe.cjs run against a published realtimex-* release.

For one platform/arch it:
  - reads the release's whisper-addon-manifest.json;
  - downloads that platform's zip, checks its sha256 against the manifest and
    extracts whisper_addon.node;
  - fetches the speech model the app prefers (ggml-base-q5_1.bin) and the VAD
    model the addon loads from the same folder (ggml-silero-v6.2.0.bin), both
    checked against pinned sha256s and kept in --cache-dir;
  - fetches samples/jfk.wav from upstream at the release's upstream commit.

It prints the paths as JSON and, with --github-output, writes them as step
outputs (addon, model, wav).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import zipfile
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import (  # noqa: E402
    ADDON_NAME,
    MANIFEST_NAME,
    PACKAGING_REPO,
    UPSTREAM_REPO,
    GitHub,
    asset_name,
    write_github_output,
)

# The app's preferred model and its VAD model (realtimex-ai-app
# frontend/src/electron/modelDownloader.cjs), pinned by sha256.
MODELS = {
    "ggml-base-q5_1.bin": (
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base-q5_1.bin",
        "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898",
    ),
    "ggml-silero-v6.2.0.bin": (
        "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin",
        "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
    ),
}
SPEECH_MODEL = "ggml-base-q5_1.bin"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "realtimex-whisper-verify"})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dest)


def ensure_model(cache_dir: str, name: str) -> str:
    url, want = MODELS[name]
    path = os.path.join(cache_dir, name)
    if not (os.path.isfile(path) and sha256_file(path) == want):
        fetch(url, path)
    got = sha256_file(path)
    if got != want:
        raise SystemExit(f"{name}: sha256 {got} != pinned {want}")
    return path


def manifest_asset(manifest: Dict, name: str) -> Dict:
    for entry in manifest.get("assets") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    raise SystemExit(f"{name} is not listed in {MANIFEST_NAME}")


def prepare(gh: GitHub, repo: str, tag: str, platform: str, arch: str, out_dir: str, cache_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    release = gh.find_release(repo, tag)
    if release is None or release.get("draft"):
        raise SystemExit(f"{tag} is not a published release of {repo}")
    manifest = gh.read_manifest(release)
    if not isinstance(manifest, dict):
        raise SystemExit(f"{tag}: {MANIFEST_NAME} is missing or unreadable")

    name = asset_name(platform, arch, str(manifest.get("electronRuntimeVersion")))
    entry = manifest_asset(manifest, name)
    url = next((a["browser_download_url"] for a in release.get("assets") or [] if a.get("name") == name), None)
    if not url:
        raise SystemExit(f"{tag} has no asset {name}")
    zip_path = os.path.join(out_dir, name)
    fetch(url, zip_path)
    got = sha256_file(zip_path)
    if got != entry.get("sha256"):
        raise SystemExit(f"{name}: sha256 {got} != manifest {entry.get('sha256')}")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extract(ADDON_NAME, out_dir)

    model = ensure_model(cache_dir, SPEECH_MODEL)
    for extra in MODELS:
        if extra != SPEECH_MODEL:
            ensure_model(cache_dir, extra)

    upstream_sha = str(manifest.get("upstreamSha") or "")
    wav = os.path.join(out_dir, "jfk.wav")
    fetch(f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{upstream_sha}/samples/jfk.wav", wav)

    return {
        "addon": os.path.join(out_dir, ADDON_NAME),
        "model": model,
        "wav": wav,
        "asset": name,
        "asset_sha256": got,
        "upstream_sha": upstream_sha,
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=PACKAGING_REPO)
    p.add_argument("--tag", required=True)
    p.add_argument("--platform", required=True, choices=["mac", "linux", "win"])
    p.add_argument("--arch", required=True, choices=["x64", "arm64"])
    p.add_argument("--out-dir", required=True)
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--github-output", action="store_true")
    args = p.parse_args(argv)

    paths = prepare(GitHub(), args.repo, args.tag, args.platform, args.arch, args.out_dir, args.cache_dir)
    print(json.dumps(paths, indent=2))
    if args.github_output:
        write_github_output(paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
