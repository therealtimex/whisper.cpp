#!/usr/bin/env python3
"""Book an on-demand self-hosted runner for queued release jobs.

The mac-arm64 fallback job of realtimex-whisper-addon.yml waits for a runner
with the label in REALTIMEX_MAC_ARM64_FALLBACK_RUNNER. Nothing listens on that
label by default (the repository is public, so no standing self-hosted runner
waits for jobs). The iMac heartbeat runs this script; when a job is queued for
the label and no runner with it is online, it mints a single-use (JIT) runner
configuration, which the heartbeat hands to the Mac mini's runner (see
realtimex/self-hosted/start-jit-runner.sh). The runner takes one job and is gone.

Modes:
  default   print the plan (one line, or JSON with --json)
  --mint    when a runner is needed, print only the encoded JIT configuration
            on stdout (nothing otherwise). Never log or store it.
  --cleanup first delete offline runners named with --name-prefix (single-use
            registrations that never connected or already finished)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import ADDON_WORKFLOW, PACKAGING_REPO, GitHub  # noqa: E402

DEFAULT_LABEL = "rtx-whisper-mac-arm64"
DEFAULT_PREFIX = "rtx-mini-"
# Repository-level runners belong to the "Default" group.
DEFAULT_RUNNER_GROUP = 1


def runner_labels(runner: Dict[str, Any]) -> List[str]:
    return [lbl.get("name", "") for lbl in runner.get("labels") or []]


def decide(queued_jobs: List[Dict[str, Any]], runners: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """`queued_jobs`: jobs waiting for a runner; `runners`: the repository's runners."""
    waiting = [j for j in queued_jobs if label in (j.get("labels") or [])]
    mine = [r for r in runners if label in runner_labels(r)]
    online = [r for r in mine if r.get("status") == "online"]
    plan: Dict[str, Any] = {
        "label": label,
        "queuedJobs": [{"id": j.get("id"), "name": j.get("name"), "runId": j.get("run_id")} for j in waiting],
        "onlineRunners": [{"name": r.get("name"), "busy": bool(r.get("busy"))} for r in online],
        "needed": False,
        "reason": "",
    }
    if not waiting:
        plan["reason"] = "no-queued-job"
    elif online:
        # One build at a time on the mini: an idle runner will take the job,
        # a busy one frees up when its job ends.
        plan["reason"] = "runner-online"
    else:
        plan.update(needed=True, reason="queued-job-without-runner")
    return plan


def unfinished_runs(gh: GitHub, repo: str, workflow: str, max_pages: int = 50) -> List[Dict[str, Any]]:
    """Every queued or in-progress run (until a short page; max_pages is only
    a runaway guard, and hitting it is reported)."""
    runs: List[Dict[str, Any]] = []
    for status in ("queued", "in_progress"):
        page = 1
        while True:
            data = gh.get(f"repos/{repo}/actions/workflows/{workflow}/runs",
                          {"status": status, "per_page": 100, "page": page})
            batch = list(data.get("workflow_runs") or [])
            runs.extend(batch)
            if len(batch) < 100:
                break
            if page >= max_pages:
                print(f"warning: more than {max_pages * 100} {status} runs; later ones not checked", file=sys.stderr)
                break
            page += 1
    return runs


def queued_jobs(gh: GitHub, repo: str, workflow: str) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    for run in unfinished_runs(gh, repo, workflow):
        data = gh.get(f"repos/{repo}/actions/runs/{run['id']}/jobs", {"filter": "latest", "per_page": 100})
        jobs.extend(j for j in data.get("jobs") or [] if j.get("status") == "queued")
    return jobs


def list_runners(gh: GitHub, repo: str) -> List[Dict[str, Any]]:
    return list(gh.get(f"repos/{repo}/actions/runners", {"per_page": 100}).get("runners") or [])


def cleanup(gh: GitHub, repo: str, runners: List[Dict[str, Any]], prefix: str) -> List[str]:
    removed = []
    for r in runners:
        if r.get("status") == "offline" and str(r.get("name", "")).startswith(prefix):
            gh.delete(f"repos/{repo}/actions/runners/{r['id']}")
            removed.append(r["name"])
    return removed


def mint(gh: GitHub, repo: str, label: str, prefix: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    body = {
        "name": prefix + now.strftime("%Y%m%dT%H%M%SZ"),
        "runner_group_id": DEFAULT_RUNNER_GROUP,
        # Only the fallback label: the runner must not match generic
        # `self-hosted` jobs from anywhere else.
        "labels": [label],
        "work_folder": "_work",
    }
    return gh.post(f"repos/{repo}/actions/runners/generate-jitconfig", body)["encoded_jit_config"]


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--packaging-repo", default=PACKAGING_REPO)
    p.add_argument("--workflow", default=ADDON_WORKFLOW)
    p.add_argument("--label", default=DEFAULT_LABEL)
    p.add_argument("--name-prefix", default=DEFAULT_PREFIX)
    p.add_argument("--cleanup", action="store_true")
    p.add_argument("--mint", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    gh = GitHub()
    runners = list_runners(gh, args.packaging_repo)
    removed = cleanup(gh, args.packaging_repo, runners, args.name_prefix) if args.cleanup else []
    if removed:
        runners = list_runners(gh, args.packaging_repo)
    plan = decide(queued_jobs(gh, args.packaging_repo, args.workflow), runners, args.label)
    plan["removedOfflineRunners"] = removed

    if args.mint:
        if plan["needed"]:
            sys.stdout.write(mint(gh, args.packaging_repo, args.label, args.name_prefix))
        return 0
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        jobs = ", ".join(f"{j['name']} (run {j['runId']})" for j in plan["queuedJobs"]) or "none"
        print(f"needed={str(plan['needed']).lower()}: {plan['reason']}; queued: {jobs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
