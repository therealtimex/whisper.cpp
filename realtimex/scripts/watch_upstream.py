#!/usr/bin/env python3
"""Decide whether the RealTimeX whisper addon release needs to run.

Compares the latest ggml-org/whisper.cpp release with the packaging release
`realtimex-<ref>` and with past runs of realtimex-whisper-addon.yml, then says
what to do. It never dispatches anything itself; the callers
(.github/workflows/realtimex-watch-upstream.yml and the iMac heartbeat script)
act on `action`:

  none          nothing to do (already current, in flight, backing off, or
                something a person has to look at; see `reason`)
  dispatch      start realtimex-whisper-addon.yml with `dispatchInputs`
  rerun-failed  re-run only the failed jobs of `runId` (the successful
                platforms' artifacts are reused, so a runner outage on one
                platform does not rebuild the other five)

Failed attempts are counted per packaging commit: runs of the same target
started from the current default-branch head. A failure is retried at most
every --backoff-hours and at most --max-attempts times in total; then the
watcher reports `attempts-exhausted` until the harness changes (a new commit
resets the count) or a person acts.

Off switch: disabling realtimex-watch-upstream.yml pauses every automatic
release, including the heartbeat that runs this script outside GitHub.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import (  # noqa: E402
    ADDON_WORKFLOW,
    ASSEMBLABLE_STATES,
    DEFAULT_ELECTRON_VERSION,
    ELECTRON_VARIABLE,
    PACKAGING_REPO,
    STATE_COMPLETE,
    STATE_ELECTRON_MISMATCH,
    UPSTREAM_REPO,
    WATCH_WORKFLOW,
    GitHub,
    default_release_tag,
    electron_release_tag,
    run_title,
    write_github_output,
)

DEFAULT_BACKOFF_HOURS = 6.0
DEFAULT_MAX_ATTEMPTS = 4
# Workflow artifacts are kept 7 days; re-running failed jobs needs the
# successful jobs' artifacts, so only re-run inside this window.
DEFAULT_RERUN_WINDOW_HOURS = 144.0


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def choose_target(
    upstream_ref: str, electron_version: str, inspect: Callable[[str], Dict[str, Any]]
) -> Tuple[str, Dict[str, Any]]:
    """Pick the release tag to check. A complete `realtimex-<ref>` built for a
    different Electron runtime is immutable, so a new runtime gets its own tag."""
    tag = default_release_tag(upstream_ref)
    state = inspect(tag)
    if state["state"] == STATE_ELECTRON_MISMATCH:
        tag = electron_release_tag(upstream_ref, electron_version)
        state = inspect(tag)
    return tag, state


def decide(
    *,
    upstream_ref: str,
    release_tag: str,
    state: Dict[str, Any],
    workflow_states: Dict[str, Optional[str]],
    runs: List[Dict[str, Any]],
    packaging_sha: str,
    now: datetime,
    backoff_hours: float = DEFAULT_BACKOFF_HOURS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    rerun_window_hours: float = DEFAULT_RERUN_WINDOW_HOURS,
    ignore_backoff: bool = False,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"action": "none", "reason": "", "runId": None, "runUrl": None, "runAttempt": None}
    title = run_title(upstream_ref, release_tag, publish=True)
    out["runTitle"] = title

    if state["state"] == STATE_COMPLETE:
        out["reason"] = "already-current"
        return out
    if state["state"] not in ASSEMBLABLE_STATES:
        # Published releases are immutable; a person decides what to do.
        out["reason"] = f"needs-human:{state['state']}"
        return out
    for name in (WATCH_WORKFLOW, ADDON_WORKFLOW):
        if workflow_states.get(name) != "active":
            # Respect a person's off switch instead of re-enabling the workflow.
            out["reason"] = f"paused:{name}={workflow_states.get(name) or 'missing'}"
            return out

    matching = [r for r in runs if r.get("display_title") == title]
    matching.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    active = next((r for r in matching if r.get("status") != "completed"), None)
    if active is not None:
        out.update(reason=f"in-flight:{active.get('status')}", runId=active.get("id"), runUrl=active.get("html_url"))
        return out

    # Only runs of the current harness count: a new packaging commit gets a
    # fresh set of attempts, an unchanged one does not retry forever.
    current = [r for r in matching if r.get("head_sha") == packaging_sha]
    failed_attempts = sum(int(r.get("run_attempt") or 1) for r in current if r.get("conclusion") != "success")
    out["failedAttempts"] = failed_attempts
    last = current[0] if current else None
    if last is None:
        out.update(action="dispatch", reason=f"release-{state['state']}")
        return out

    out.update(runId=last.get("id"), runUrl=last.get("html_url"), runAttempt=last.get("run_attempt"))
    if last.get("conclusion") == "success":
        # The release should now be complete; let the next check confirm it.
        out["reason"] = "last-run-succeeded"
        return out
    if failed_attempts >= max_attempts:
        out["reason"] = f"attempts-exhausted:{failed_attempts}"
        return out

    finished = parse_time(last.get("updated_at") or last["created_at"])
    created = parse_time(last["created_at"])
    eligible_at = finished + timedelta(hours=backoff_hours)
    if not ignore_backoff and now < eligible_at:
        out["reason"] = f"backoff-after-{last.get('conclusion')}"
        out["nextEligibleAt"] = format_time(eligible_at)
        return out
    if now - created < timedelta(hours=rerun_window_hours):
        out.update(action="rerun-failed", reason=f"retry-after-{last.get('conclusion')}")
        return out
    out.update(action="dispatch", reason="last-run-too-old-to-rerun")
    return out


def watch(gh: GitHub, args: argparse.Namespace, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    upstream_ref = (args.force_ref or "").strip() or gh.latest_release_tag(args.upstream_repo)
    upstream_sha = gh.commit_sha(args.upstream_repo, upstream_ref)
    electron = (
        (args.electron_version or "").strip()
        or gh.variable(args.packaging_repo, ELECTRON_VARIABLE)
        or DEFAULT_ELECTRON_VERSION
    )

    def inspect(tag: str) -> Dict[str, Any]:
        return gh.inspect_release(
            args.packaging_repo, tag, electron_version=electron, upstream_sha=upstream_sha
        )

    release_tag, state = choose_target(upstream_ref, electron, inspect)
    workflow_states = {
        name: gh.workflow_state(args.packaging_repo, name) for name in (WATCH_WORKFLOW, ADDON_WORKFLOW)
    }
    packaging_sha = gh.default_branch_sha(args.packaging_repo)
    runs = gh.workflow_runs(args.packaging_repo, ADDON_WORKFLOW)
    decision = decide(
        upstream_ref=upstream_ref,
        release_tag=release_tag,
        state=state,
        workflow_states=workflow_states,
        runs=runs,
        packaging_sha=packaging_sha,
        now=now,
        backoff_hours=args.backoff_hours,
        max_attempts=args.max_attempts,
        rerun_window_hours=args.rerun_window_hours,
        ignore_backoff=args.ignore_backoff,
    )
    return {
        "checkedAt": format_time(now),
        "upstreamRepo": args.upstream_repo,
        "upstreamRef": upstream_ref,
        "upstreamSha": upstream_sha,
        "electronVersion": electron,
        "releaseTag": release_tag,
        "releaseState": state["state"],
        "releaseUrl": state.get("releaseUrl"),
        "missingAssets": state.get("missingAssets", []),
        "stateReasons": state.get("reasons", []),
        "packagingSha": packaging_sha,
        "workflowStates": workflow_states,
        **decision,
        "dispatchInputs": {"upstream_ref": upstream_ref, "release_tag": release_tag, "publish": "true"},
    }


def summary_markdown(result: Dict[str, Any]) -> str:
    lines = [
        "## RealTimeX whisper addon watch",
        "",
        f"- Upstream: `{result['upstreamRepo']}@{result['upstreamRef']}` (`{result['upstreamSha']}`)",
        f"- Electron runtime: `{result['electronVersion']}`",
        f"- Release tag: `{result['releaseTag']}` — state `{result['releaseState']}`",
    ]
    if result.get("missingAssets"):
        lines.append(f"- Missing assets: `{', '.join(result['missingAssets'])}`")
    lines.append(f"- Packaging head: `{result['packagingSha']}`; failed attempts on it: "
                 f"`{result.get('failedAttempts', 0)}`")
    lines.append(f"- Action: `{result['action']}` (`{result['reason']}`)")
    if result.get("runUrl"):
        lines.append(f"- Last run: {result['runUrl']} (attempt {result.get('runAttempt')})")
    if result.get("nextEligibleAt"):
        lines.append(f"- Next retry not before: `{result['nextEligibleAt']}`")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--upstream-repo", default=UPSTREAM_REPO)
    p.add_argument("--packaging-repo", default=PACKAGING_REPO)
    p.add_argument("--force-ref", default="", help="Check this upstream tag/branch/SHA instead of the latest release")
    p.add_argument("--electron-version", default=os.environ.get(ELECTRON_VARIABLE, ""))
    p.add_argument("--backoff-hours", type=float, default=DEFAULT_BACKOFF_HOURS)
    p.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    p.add_argument("--rerun-window-hours", type=float, default=DEFAULT_RERUN_WINDOW_HOURS)
    p.add_argument("--ignore-backoff", action="store_true")
    p.add_argument("--json", action="store_true", help="Print the result as JSON (default: one line)")
    p.add_argument("--github-output", action="store_true", help="Also write outputs and a step summary")
    args = p.parse_args(argv)

    result = watch(GitHub(), args)
    if args.github_output:
        write_github_output({
            "action": result["action"],
            "reason": result["reason"],
            "upstream_ref": result["upstreamRef"],
            "upstream_sha": result["upstreamSha"],
            "release_tag": result["releaseTag"],
            "release_state": result["releaseState"],
            "run_id": result["runId"],
        })
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(summary_markdown(result))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"{result['action']}: {result['reason']} — {result['upstreamRef']} -> {result['releaseTag']} "
            f"({result['releaseState']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
