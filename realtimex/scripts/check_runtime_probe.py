#!/usr/bin/env python3
"""Judge a runtime_probe.cjs run: every step reached, both transcripts right,
and a clean exit (realtimex-ai-app#3238 aborted at exit with 134).

  check_runtime_probe.py --log probe.jsonl --exit-code N [--expect "phrase"] [--json]

Exit 0 only when the probe passed. Prints a one-line verdict (or JSON).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional

# jfk.wav: "And so my fellow Americans, ask not what your country can do for you,
# ask what you can do for your country."
DEFAULT_EXPECT = "ask not what your country can do for you"
# Every step the app's native.cjs goes through, session path and dispose included.
REQUIRED_STEPS = [
    "env", "loaded", "devices", "wav", "initialized", "transcribed-legacy",
    "session-initialized", "transcribed-session", "session-destroyed", "disposed", "done",
]


def normalize(text: str) -> str:
    return re.sub(r"[^a-z ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def parse(lines: List[str]) -> List[Dict[str, Any]]:
    steps = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            steps.append(json.loads(line))
        except ValueError:
            continue
    return steps


def judge(lines: List[str], exit_code: int, expect: str = DEFAULT_EXPECT) -> Dict[str, Any]:
    want = normalize(expect)
    if not want:
        raise ValueError("the expected phrase is empty; it would match any transcript")
    steps = parse(lines)
    by_step: Dict[str, Dict[str, Any]] = {}
    for s in steps:
        by_step.setdefault(str(s.get("step")), s)
    problems: List[str] = []
    for name in REQUIRED_STEPS:
        if name not in by_step:
            problems.append(f"missing step '{name}'")
    if "devices-error" in by_step:
        problems.append(f"getBackendDevices failed: {by_step['devices-error'].get('message')}")
    if "error" in by_step:
        problems.append(f"error: {str(by_step['error'].get('message'))[:200]}")
    texts: Dict[str, Optional[str]] = {}
    for name in ("transcribed-legacy", "transcribed-session"):
        if name in by_step:
            texts[name] = str(by_step[name].get("text", ""))
            if want not in normalize(texts[name]):
                problems.append(f"{name} transcript lacks '{expect}': {texts[name][:120]!r}")
    if exit_code != 0:
        problems.append(f"exit code {exit_code} (expected 0; 134 = abort at exit)")
    env = by_step.get("env", {})
    devices = by_step.get("devices", {}).get("devices")
    return {
        "passed": not problems,
        "problems": problems,
        "exitCode": exit_code,
        "platform": env.get("platform"),
        "arch": env.get("arch"),
        "electron": env.get("electron"),
        "napi": env.get("napi"),
        "devices": [d.get("name") for d in devices] if isinstance(devices, list) else devices,
        "texts": texts,
        "ms": by_step.get("done", {}).get("ms"),
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", required=True)
    p.add_argument("--exit-code", type=int, required=True)
    p.add_argument("--expect", default=DEFAULT_EXPECT)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        try:
            verdict = judge(fh.readlines(), args.exit_code, args.expect)
        except ValueError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        state = "PASS" if verdict["passed"] else "FAIL"
        print(f"{state} {verdict['platform']}-{verdict['arch']} electron {verdict['electron']} "
              f"devices {verdict['devices']} exit {verdict['exitCode']}"
              + ("" if verdict["passed"] else " | " + "; ".join(verdict["problems"])))
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
