"""Shared definitions for the RealTimeX whisper addon release.

The release workflow, the watcher workflow and the local heartbeat watcher all
import this module, so asset names, run titles and release states have one
definition.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

UPSTREAM_REPO = "ggml-org/whisper.cpp"
PACKAGING_REPO = "therealtimex/whisper.cpp"
ADDON_WORKFLOW = "realtimex-whisper-addon.yml"
WATCH_WORKFLOW = "realtimex-watch-upstream.yml"
ELECTRON_VARIABLE = "REALTIMEX_ELECTRON_RUNTIME_VERSION"
DEFAULT_ELECTRON_VERSION = "41.0.3"
DEFAULT_MACOS_DEPLOYMENT_TARGET = "13.0"

ADDON_NAME = "whisper_addon.node"
METADATA_NAME = "_realtimexWhisperAddonMetadata.json"
MANIFEST_NAME = "whisper-addon-manifest.json"
MANIFEST_SCHEMA_VERSION = 2
RUN_TITLE_PREFIX = "RealTimeX Whisper Addon"

# (platform, arch) pairs the app downloads. Order is the manifest order.
TARGETS: Tuple[Tuple[str, str], ...] = (
    ("mac", "x64"),
    ("mac", "arm64"),
    ("linux", "x64"),
    ("linux", "arm64"),
    ("win", "x64"),
    ("win", "arm64"),
)

# Release states. Only "absent" and "draft" may be (re)assembled by automation.
STATE_ABSENT = "absent"
STATE_DRAFT = "draft"
STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_INVALID = "invalid"
STATE_ELECTRON_MISMATCH = "electron-mismatch"
STATE_UPSTREAM_MISMATCH = "upstream-mismatch"
ASSEMBLABLE_STATES = (STATE_ABSENT, STATE_DRAFT)


def asset_name(platform: str, arch: str, electron_version: str) -> str:
    return f"whisper-addon-{platform}-{arch}-electron-{electron_version}.zip"


def expected_asset_names(electron_version: str) -> List[str]:
    return [asset_name(p, a, electron_version) for p, a in TARGETS]


def safe_ref(ref: str) -> str:
    out = ref.strip()
    for ch in "/:@":
        out = out.replace(ch, "-")
    return out


def default_release_tag(upstream_ref: str) -> str:
    return f"realtimex-{safe_ref(upstream_ref)}"


def electron_release_tag(upstream_ref: str, electron_version: str) -> str:
    return f"{default_release_tag(upstream_ref)}-electron-{electron_version}"


def run_title(upstream_ref: str, release_tag: str, publish: bool) -> str:
    """Must match `run-name` in .github/workflows/realtimex-whisper-addon.yml."""
    title = f"{RUN_TITLE_PREFIX} {upstream_ref} -> {release_tag}"
    return title if publish else f"{title} (dry run)"


def parse_version(text: str) -> Tuple[int, int, int]:
    parts = [int(p) for p in str(text).strip().split(".") if p != ""]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def release_state(
    release: Optional[Dict[str, Any]],
    manifest: Optional[Dict[str, Any]],
    *,
    release_tag: str,
    electron_version: str,
    upstream_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify a packaging release.

    `release` is the GitHub release object (or None); `manifest` is the parsed
    whisper-addon-manifest.json asset (or None when absent or unreadable).
    """
    result: Dict[str, Any] = {
        "releaseTag": release_tag,
        "state": STATE_ABSENT,
        "complete": False,
        "draft": False,
        "missingAssets": [],
        "reasons": [],
    }
    if release is None:
        return result

    published = sorted(a.get("name", "") for a in release.get("assets") or [])
    if release.get("draft"):
        result.update(state=STATE_DRAFT, draft=True)
        result["missingAssets"] = [
            n for n in expected_asset_names(electron_version) + [MANIFEST_NAME] if n not in published
        ]
        return result

    if manifest is not None and not isinstance(manifest, dict):
        manifest = None
    if MANIFEST_NAME not in published or manifest is None:
        result.update(state=STATE_INVALID)
        result["reasons"].append("manifest-missing-or-unreadable")
        return result

    if manifest.get("releaseTag") not in (None, release_tag):
        result.update(state=STATE_INVALID)
        result["reasons"].append(f"manifest-release-tag={manifest.get('releaseTag')}")
        return result

    manifest_electron = str(manifest.get("electronRuntimeVersion") or "")
    # The release matrix is fixed here, not read from the manifest: a manifest
    # that lists fewer assets must not make a partial release look complete.
    expected = expected_asset_names(manifest_electron)
    listed = manifest.get("expectedAssetNames")
    assets = manifest.get("assets") or []
    if (listed is not None and not (isinstance(listed, list) and all(isinstance(n, str) for n in listed))) \
            or not isinstance(assets, list) or not all(isinstance(a, dict) for a in assets):
        result.update(state=STATE_INVALID)
        result["reasons"].append("manifest-malformed")
        return result
    if listed is not None and sorted(listed) != sorted(expected):
        result.update(state=STATE_INVALID)
        result["reasons"].append("manifest-expected-assets-differ-from-matrix")
        return result
    if manifest.get("complete") is False or manifest.get("problems"):
        result.update(state=STATE_INVALID)
        result["reasons"].append("manifest-reports-incomplete-or-problems")
        return result
    recorded = {a.get("name") for a in assets}
    missing = sorted(n for n in expected if n not in published or n not in recorded)
    result["missingAssets"] = missing

    if missing:
        result.update(state=STATE_INCOMPLETE)
        return result
    if manifest_electron != electron_version:
        result.update(state=STATE_ELECTRON_MISMATCH)
        result["reasons"].append(f"electron={manifest_electron}")
        return result
    if upstream_sha and manifest.get("upstreamSha") != upstream_sha:
        result.update(state=STATE_UPSTREAM_MISMATCH)
        result["reasons"].append(f"upstreamSha={manifest.get('upstreamSha')}")
        return result

    result.update(state=STATE_COMPLETE, complete=True)
    return result


class GitHubError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"GitHub API {status} for {url}: {body[:200]}")
        self.status = status
        self.body = body

    @property
    def rate_limited(self) -> bool:
        return self.status in (403, 429) and "rate limit" in self.body.lower()


class GitHub:
    """Minimal GitHub REST client (stdlib only, so it runs on any runner)."""

    def __init__(self, token: Optional[str] = None, api: str = "https://api.github.com"):
        self.token = token if token is not None else (
            os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        )
        self.api = api.rstrip("/")

    def _request(self, url: str, accept: str, auth: bool, method: str = "GET",
                 body: Optional[Dict[str, Any]] = None) -> bytes:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": accept,
            "User-Agent": "realtimex-whisper-addon-release",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if auth and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace") if err.fp else ""
            raise GitHubError(err.code, url, body) from None

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.api}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return json.loads(self._request(url, "application/vnd.github+json", auth=True))

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        url = f"{self.api}/{path.lstrip('/')}"
        return json.loads(self._request(url, "application/vnd.github+json", auth=True, method="POST", body=body))

    def delete(self, path: str) -> None:
        url = f"{self.api}/{path.lstrip('/')}"
        self._request(url, "application/vnd.github+json", auth=True, method="DELETE")

    def get_or_none(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self.get(path, params)
        except GitHubError as err:
            if err.status == 404:
                return None
            raise

    def download_public(self, url: str) -> bytes:
        # Release asset browser URLs redirect to object storage, which rejects
        # a forwarded Authorization header; public assets need none.
        return self._request(url, "application/octet-stream", auth=False)

    # --- higher-level helpers -------------------------------------------------

    def latest_release_tag(self, repo: str) -> str:
        return self.get(f"repos/{repo}/releases/latest")["tag_name"]

    def commit_sha(self, repo: str, ref: str) -> str:
        quoted = urllib.parse.quote(ref, safe="")
        return self.get(f"repos/{repo}/commits/{quoted}")["sha"]

    def find_release(self, repo: str, tag: str) -> Optional[Dict[str, Any]]:
        """Published release by tag, else a draft with that tag (drafts are
        listed only for tokens with push access)."""
        quoted = urllib.parse.quote(tag, safe="")
        release = self.get_or_none(f"repos/{repo}/releases/tags/{quoted}")
        if release is not None:
            return release
        for page in (1, 2, 3):
            releases = self.get(f"repos/{repo}/releases", {"per_page": 100, "page": page})
            for rel in releases:
                if rel.get("draft") and rel.get("tag_name") == tag:
                    return rel
            if len(releases) < 100:
                break
        return None

    def read_manifest(self, release: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not release or release.get("draft"):
            return None
        for asset in release.get("assets") or []:
            if asset.get("name") == MANIFEST_NAME:
                try:
                    return json.loads(self.download_public(asset["browser_download_url"]))
                except (GitHubError, ValueError):
                    return None
        return None

    def inspect_release(
        self, repo: str, tag: str, *, electron_version: str, upstream_sha: Optional[str] = None
    ) -> Dict[str, Any]:
        release = self.find_release(repo, tag)
        manifest = self.read_manifest(release)
        state = release_state(
            release, manifest, release_tag=tag, electron_version=electron_version, upstream_sha=upstream_sha
        )
        if release is not None:
            state["releaseUrl"] = release.get("html_url")
        return state

    def default_branch_sha(self, repo: str) -> str:
        branch = self.get(f"repos/{repo}")["default_branch"]
        return self.commit_sha(repo, branch)

    def workflow_state(self, repo: str, workflow_file: str) -> Optional[str]:
        wf = self.get_or_none(f"repos/{repo}/actions/workflows/{workflow_file}")
        return wf.get("state") if wf else None

    def workflow_runs(self, repo: str, workflow_file: str, limit: int = 300) -> List[Dict[str, Any]]:
        """The newest `limit` workflow_dispatch runs (paginated)."""
        runs: List[Dict[str, Any]] = []
        page = 1
        while len(runs) < limit:
            data = self.get(
                f"repos/{repo}/actions/workflows/{workflow_file}/runs",
                {"event": "workflow_dispatch", "per_page": 100, "page": page},
            )
            batch = list(data.get("workflow_runs") or [])
            runs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return runs[:limit]

    def variable(self, repo: str, name: str) -> Optional[str]:
        """A repository variable, or None if this token may not read it
        (GITHUB_TOKEN can't). Other errors raise: a transient failure must
        not silently pick a default Electron version for an immutable release."""
        try:
            data = self.get_or_none(f"repos/{repo}/actions/variables/{name}")
        except GitHubError as err:
            # 403 is also GitHub's rate-limit answer: that must not look like
            # "this token may not read variables".
            if err.status in (401, 403) and not err.rate_limited:
                return None
            raise
        return data.get("value") if data else None


def write_github_output(values: Dict[str, Any], path: Optional[str] = None) -> None:
    path = path or os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                value = json.dumps(value, separators=(",", ":"))
            elif value is None:
                value = ""
            fh.write(f"{key}={value}\n")
