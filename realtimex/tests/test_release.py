"""Unit tests for the RealTimeX whisper addon release scripts.

Run: python3 -m unittest discover -s realtimex/tests -v
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import addon_release as ar  # noqa: E402
import build_manifest  # noqa: E402
import package_addon  # noqa: E402
import validate_addon  # noqa: E402
import watch_upstream as wu  # noqa: E402

E = "41.0.3"
SHA = "a" * 40
TAG = "realtimex-v1.9.5"
NOW = datetime(2026, 11, 9, 20, 0, 0, tzinfo=timezone.utc)
HEAD = "f" * 40
ACTIVE = {ar.WATCH_WORKFLOW: "active", ar.ADDON_WORKFLOW: "active"}


def ts(hours_ago: float) -> str:
    return wu.format_time(NOW - timedelta(hours=hours_ago))


def release(names, draft=False):
    return {"draft": draft, "assets": [{"name": n} for n in names]}


def manifest(electron=E, upstream_sha=SHA, tag=TAG, names=None, expected=True):
    names = names if names is not None else ar.expected_asset_names(electron)
    m = {
        "releaseTag": tag,
        "electronRuntimeVersion": electron,
        "upstreamSha": upstream_sha,
        "assets": [{"name": n} for n in names],
    }
    if expected:
        m["expectedAssetNames"] = ar.expected_asset_names(electron)
    return m


FULL = ar.expected_asset_names(E) + [ar.MANIFEST_NAME]


class ReleaseStateTests(unittest.TestCase):
    def state(self, rel, man, **kw):
        return ar.release_state(rel, man, release_tag=TAG, electron_version=E, upstream_sha=SHA, **kw)["state"]

    def test_absent(self):
        self.assertEqual(self.state(None, None), ar.STATE_ABSENT)

    def test_draft_is_assemblable(self):
        self.assertEqual(self.state(release(FULL[:2], draft=True), None), ar.STATE_DRAFT)

    def test_complete(self):
        self.assertEqual(self.state(release(FULL), manifest()), ar.STATE_COMPLETE)

    def test_legacy_manifest_without_expected_names_is_complete(self):
        self.assertEqual(self.state(release(FULL), manifest(expected=False)), ar.STATE_COMPLETE)

    def test_published_without_manifest_is_invalid(self):
        self.assertEqual(self.state(release(FULL[:-1]), None), ar.STATE_INVALID)

    def test_missing_asset_is_incomplete(self):
        names = FULL[1:]
        self.assertEqual(self.state(release(names), manifest()), ar.STATE_INCOMPLETE)

    def test_other_electron(self):
        other = ar.expected_asset_names("40.0.0") + [ar.MANIFEST_NAME]
        self.assertEqual(self.state(release(other), manifest(electron="40.0.0")), ar.STATE_ELECTRON_MISMATCH)

    def test_other_upstream_sha(self):
        self.assertEqual(self.state(release(FULL), manifest(upstream_sha="b" * 40)), ar.STATE_UPSTREAM_MISMATCH)

    def test_run_title_matches_workflow_run_name(self):
        workflow = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/realtimex-whisper-addon.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("RealTimeX Whisper Addon ${{ inputs.upstream_ref || 'latest-upstream' }}", text)
        self.assertIn("format(' -> {0}', inputs.release_tag)", text)
        self.assertIn("' (dry run)'", text)
        self.assertEqual(ar.run_title("v1.9.5", TAG, True), "RealTimeX Whisper Addon v1.9.5 -> realtimex-v1.9.5")
        self.assertTrue(ar.run_title("v1.9.5", TAG, False).endswith(" (dry run)"))


class DecideTests(unittest.TestCase):
    title = ar.run_title("v1.9.5", TAG, True)

    def run_(self, hours_ago, conclusion="failure", status="completed", attempt=1, head=HEAD, title=None,
             finished_hours_ago=None):
        return {
            "id": int(hours_ago * 100) + attempt,
            "display_title": title or self.title,
            "status": status,
            "conclusion": conclusion if status == "completed" else None,
            "run_attempt": attempt,
            "head_sha": head,
            "created_at": ts(hours_ago),
            "updated_at": ts(hours_ago if finished_hours_ago is None else finished_hours_ago),
            "html_url": "https://example.invalid/run",
        }

    def decide(self, runs, state=ar.STATE_ABSENT, states=None, **kw):
        return wu.decide(
            upstream_ref="v1.9.5", release_tag=TAG, state={"state": state},
            workflow_states=states or ACTIVE, runs=runs, packaging_sha=HEAD, now=NOW, **kw,
        )

    def test_complete_does_nothing(self):
        self.assertEqual(self.decide([], state=ar.STATE_COMPLETE)["reason"], "already-current")

    def test_published_but_broken_needs_a_person(self):
        out = self.decide([], state=ar.STATE_INCOMPLETE)
        self.assertEqual((out["action"], out["reason"]), ("none", "needs-human:incomplete"))

    def test_paused_by_disabled_watch_workflow(self):
        out = self.decide([], states={ar.WATCH_WORKFLOW: "disabled_manually", ar.ADDON_WORKFLOW: "active"})
        self.assertEqual(out["action"], "none")
        self.assertTrue(out["reason"].startswith("paused:realtimex-watch-upstream.yml"))

    def test_paused_by_disabled_addon_workflow(self):
        out = self.decide([], states={ar.WATCH_WORKFLOW: "active", ar.ADDON_WORKFLOW: "disabled_manually"})
        self.assertTrue(out["reason"].startswith("paused:realtimex-whisper-addon.yml"))

    def test_first_attempt_dispatches(self):
        self.assertEqual(self.decide([])["action"], "dispatch")

    def test_draft_dispatches(self):
        self.assertEqual(self.decide([], state=ar.STATE_DRAFT)["action"], "dispatch")

    def test_in_flight_is_not_duplicated_even_from_another_head(self):
        out = self.decide([self.run_(0.2, status="queued", head="0" * 40)])
        self.assertEqual((out["action"], out["reason"]), ("none", "in-flight:queued"))

    def test_dry_runs_and_other_targets_are_ignored(self):
        runs = [
            self.run_(1, title=ar.run_title("v1.9.5", TAG, False)),
            self.run_(1, title=ar.run_title("v1.9.4", "realtimex-v1.9.4", True)),
        ]
        self.assertEqual(self.decide(runs)["action"], "dispatch")

    def test_backoff_after_failure(self):
        out = self.decide([self.run_(2)])
        self.assertEqual((out["action"], out["reason"]), ("none", "backoff-after-failure"))
        self.assertEqual(out["nextEligibleAt"], ts(-4))

    def test_ignore_backoff(self):
        self.assertEqual(self.decide([self.run_(2)], ignore_backoff=True)["action"], "rerun-failed")

    def test_rerun_failed_jobs_after_backoff(self):
        out = self.decide([self.run_(7, conclusion="cancelled")])
        self.assertEqual((out["action"], out["reason"]), ("rerun-failed", "retry-after-cancelled"))
        self.assertEqual(out["runId"], 701)

    def test_stops_after_max_attempts_on_same_head(self):
        out = self.decide([self.run_(30, attempt=4)])
        self.assertEqual((out["action"], out["reason"]), ("none", "attempts-exhausted:4"))

    def test_failures_add_up_across_runs_on_same_head(self):
        runs = [self.run_(30, attempt=2), self.run_(60, attempt=2)]
        self.assertEqual(self.decide(runs)["reason"], "attempts-exhausted:4")

    def test_new_packaging_commit_resets_attempts(self):
        runs = [self.run_(30 + i, head="0" * 40) for i in range(90)]
        self.assertEqual(self.decide(runs)["action"], "dispatch")

    def test_old_failure_gets_a_fresh_dispatch(self):
        out = self.decide([self.run_(200)])
        self.assertEqual((out["action"], out["reason"]), ("dispatch", "last-run-too-old-to-rerun"))

    def test_success_waits_for_release_check(self):
        out = self.decide([self.run_(1, conclusion="success")])
        self.assertEqual((out["action"], out["reason"]), ("none", "last-run-succeeded"))

    def test_electron_mismatch_moves_to_its_own_tag(self):
        seen = []

        def inspect(tag):
            seen.append(tag)
            return {"state": ar.STATE_ELECTRON_MISMATCH if tag == TAG else ar.STATE_ABSENT}

        tag, state = wu.choose_target("v1.9.5", "42.0.0", inspect)
        self.assertEqual(tag, "realtimex-v1.9.5-electron-42.0.0")
        self.assertEqual(state["state"], ar.STATE_ABSENT)
        self.assertEqual(seen, [TAG, tag])


def macho(cputype, minos=(13, 0, 0)):
    encoded = (minos[0] << 16) | (minos[1] << 8) | minos[2]
    build_version = struct.pack("<IIIIII", 0x32, 24, 1, encoded, encoded, 0)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, cputype, 0, 6, 1, len(build_version), 0, 0)
    return header + build_version + b"\0" * 64


def elf(machine):
    head = bytearray(64)
    head[:4] = b"\x7fELF"
    head[4], head[5] = 2, 1
    struct.pack_into("<H", head, 18, machine)
    return bytes(head)


def pe(machine):
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    return bytes(data)


NM_WEAK = (
    "0000000000001000 (__TEXT,__text) weak external "
    "__ZN4Napi7details12CallbackDataIPFNS_5ValueERKNS_12CallbackInfoEES2_E7WrapperEP10napi_env__P20napi_callback_info__\n"
    "0000000000002000 (__TEXT,__text) external _napi_register_module_v1\n"
)
NM_HIDDEN = (
    "0000000000001000 (__TEXT,__text) non-external (was a private external) __ZN4Napi5Error3NewEP10napi_env__\n"
    "0000000000002000 (__TEXT,__text) weak private external __ZN4Napi6ObjectC2Ev\n"
    "                 (undefined) weak external __ZN4Napi4OtherEv (from libfoo)\n"
    "0000000000002000 (__TEXT,__text) external _napi_register_module_v1\n"
)


class PackageAndValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pack(self, platform, arch, data, electron=E, sha=SHA):
        build = self.dir / f"build-{platform}-{arch}" / "Release"
        build.mkdir(parents=True)
        (build / ar.ADDON_NAME).write_bytes(data)
        return package_addon.package(
            addon=package_addon.find_addon(build.parent), out_dir=self.dir / "dist",
            platform=platform, arch=arch, node_arch=arch, electron_version=electron,
            upstream_ref="v1.9.5", upstream_sha=sha, packaging_repo=ar.PACKAGING_REPO, packaging_sha=HEAD,
            macos_deployment_target="13.0" if platform == "mac" else None,
        )

    def validate(self, path, platform, arch, nm=NM_HIDDEN, **kw):
        return validate_addon.validate(
            str(path), platform=platform, arch=arch, electron_version=E, upstream_sha=SHA, nm_output=nm, **kw
        )

    def test_good_mac_arm64(self):
        report = self.validate(self.pack("mac", "arm64", macho(0x0100000C)), "mac", "arm64")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["archs"], ["arm64"])
        self.assertEqual(report["minos"], ["13.0.0"])
        self.assertEqual(report["weakNapiExports"], 0)

    def test_mac_minos_newer_than_floor_fails(self):
        report = self.validate(self.pack("mac", "x64", macho(0x01000007, (15, 0, 0))), "mac", "x64")
        self.assertTrue(any("newer than the app's floor" in e for e in report["errors"]))

    def test_mac_weak_napi_export_fails(self):
        report = self.validate(self.pack("mac", "arm64", macho(0x0100000C)), "mac", "arm64", nm=NM_WEAK)
        self.assertTrue(any("weak node-addon-api" in e for e in report["errors"]))

    def test_mac_wrong_arch_fails(self):
        report = self.validate(self.pack("mac", "arm64", macho(0x01000007)), "mac", "arm64")
        self.assertTrue(any("architectures" in e for e in report["errors"]))

    def test_linux_weak_napi_is_a_warning(self):
        nm = "0000000000001000 W _ZN4Napi5ErrorD2Ev\n0000000000002000 T napi_register_module_v1\n"
        report = self.validate(self.pack("linux", "x64", elf(62)), "linux", "x64", nm=nm)
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["warnings"]), 1)

    def test_windows_pe_arm64(self):
        report = self.validate(self.pack("win", "arm64", pe(0xAA64)), "win", "arm64")
        self.assertEqual((report["errors"], report["archs"]), ([], ["arm64"]))

    def test_wrong_upstream_sha_fails(self):
        path = self.pack("linux", "arm64", elf(183), sha="c" * 40)
        report = self.validate(path, "linux", "arm64")
        self.assertTrue(any("upstreamSha" in e for e in report["errors"]))

    def test_tampered_binary_fails(self):
        path = self.pack("linux", "arm64", elf(183))
        with zipfile.ZipFile(path) as z:
            meta = z.read(ar.METADATA_NAME)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(ar.ADDON_NAME, elf(62))
            z.writestr(ar.METADATA_NAME, meta)
        report = self.validate(path, "linux", "arm64")
        self.assertTrue(any("sha256" in e for e in report["errors"]))

    def test_manifest_complete_only_with_all_platforms(self):
        builders = {"mac": lambda a: macho(0x0100000C if a == "arm64" else 0x01000007),
                    "linux": lambda a: elf(183 if a == "arm64" else 62),
                    "win": lambda a: pe(0xAA64 if a == "arm64" else 0x8664)}
        for platform, arch in ar.TARGETS[:-1]:
            self.pack(platform, arch, builders[platform](arch))
        args = dict(release_tag=TAG, electron_version=E, upstream_ref="v1.9.5", upstream_sha=SHA,
                    packaging_repo=ar.PACKAGING_REPO, packaging_sha=HEAD)
        partial = build_manifest.build_manifest(self.dir / "dist", **args)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["missingAssetNames"], [ar.asset_name("win", "arm64", E)])
        self.pack("win", "arm64", pe(0xAA64))
        full = build_manifest.build_manifest(self.dir / "dist", **args)
        self.assertTrue(full["complete"])
        self.assertEqual([a["name"] for a in full["assets"]], ar.expected_asset_names(E))
        rel = release(ar.expected_asset_names(E) + [ar.MANIFEST_NAME])
        state = ar.release_state(rel, json.loads(json.dumps(full)), release_tag=TAG, electron_version=E,
                                 upstream_sha=SHA)
        self.assertEqual(state["state"], ar.STATE_COMPLETE)

    def test_manifest_rejects_mixed_upstream(self):
        self.pack("linux", "x64", elf(62), sha="c" * 40)
        m = build_manifest.build_manifest(
            self.dir / "dist", release_tag=TAG, electron_version=E, upstream_ref="v1.9.5", upstream_sha=SHA,
            packaging_repo=ar.PACKAGING_REPO, packaging_sha=HEAD)
        self.assertFalse(m["complete"])
        self.assertTrue(m["problems"])


if __name__ == "__main__":
    unittest.main()
