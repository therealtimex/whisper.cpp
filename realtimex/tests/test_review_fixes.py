"""Tests for the fixes made after the AI review of PRs #2 and #3.

Run: python3 -m unittest discover -s realtimex/tests -v
"""

from __future__ import annotations

import io
import os
import struct
import sys
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import addon_release as ar  # noqa: E402
import book_runner as br  # noqa: E402
import release_state  # noqa: E402
import validate_addon  # noqa: E402
import watch_upstream as wu  # noqa: E402
from test_release import (  # noqa: E402
    ACTIVE, E, HEAD, NOW, SHA, TAG, FULL, PackageAndValidateTests, elf, manifest, pe, release, ts,
)


def macho_platform(cputype, platform, minos=(13, 0, 0)):
    encoded = (minos[0] << 16) | (minos[1] << 8) | minos[2]
    build_version = struct.pack("<IIIIII", 0x32, 24, platform, encoded, encoded, 0)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, cputype, 0, 6, 1, len(build_version), 0, 0)
    return header + build_version + b"\0" * 64


class SixAssetContractTests(unittest.TestCase):
    def test_matrix_is_exactly_the_six_assets_the_app_downloads(self):
        self.assertEqual(ar.expected_asset_names("41.0.3"), [
            "whisper-addon-mac-x64-electron-41.0.3.zip",
            "whisper-addon-mac-arm64-electron-41.0.3.zip",
            "whisper-addon-linux-x64-electron-41.0.3.zip",
            "whisper-addon-linux-arm64-electron-41.0.3.zip",
            "whisper-addon-win-x64-electron-41.0.3.zip",
            "whisper-addon-win-arm64-electron-41.0.3.zip",
        ])


class ValidatorReviewTests(unittest.TestCase):
    # Reuse the helpers only; inheriting would run that class's tests twice.
    setUp = PackageAndValidateTests.setUp
    tearDown = PackageAndValidateTests.tearDown
    pack = PackageAndValidateTests.pack
    validate = PackageAndValidateTests.validate

    def test_good_mac_x64(self):
        report = self.validate(self.pack("mac", "x64", macho_platform(0x01000007, 1)), "mac", "x64")
        self.assertEqual((report["errors"], report["archs"]), ([], ["x64"]))

    def test_good_win_x64(self):
        report = self.validate(self.pack("win", "x64", pe(0x8664)), "win", "x64")
        self.assertEqual((report["errors"], report["archs"]), ([], ["x64"]))

    def test_weak_const_member_function_fails_on_mac(self):
        nm = "0000000000001000 (__TEXT,__text) weak external __ZNK4Napi5Value2IsEv\n"
        report = self.validate(self.pack("mac", "arm64", macho_platform(0x0100000C, 1)), "mac", "arm64", nm=nm)
        self.assertEqual(report["weakNapiExports"], 1)
        self.assertTrue(any("weak node-addon-api" in e for e in report["errors"]))

    def test_weak_const_member_function_warns_on_linux(self):
        nm = "0000000000001000 W _ZNK4Napi5Value2IsEv\n"
        report = self.validate(self.pack("linux", "x64", elf(62)), "linux", "x64", nm=nm)
        self.assertEqual((report["errors"], len(report["warnings"])), ([], 1))

    def test_non_macos_macho_fails(self):
        ios = 2
        report = self.validate(self.pack("mac", "arm64", macho_platform(0x0100000C, ios)), "mac", "arm64")
        self.assertTrue(any("not macOS" in e for e in report["errors"]))

    def test_truncated_elf_is_an_error_not_a_crash(self):
        report = self.validate(self.pack("linux", "x64", b"\x7fELF\x02"), "linux", "x64")
        self.assertTrue(any("truncated" in e for e in report["errors"]))

    def test_metadata_must_be_an_object(self):
        path = self.pack("linux", "x64", elf(62))
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(ar.ADDON_NAME, elf(62))
            z.writestr(ar.METADATA_NAME, "null")
        report = self.validate(path, "linux", "x64")
        self.assertTrue(any("not a JSON object" in e for e in report["errors"]))


class ManifestTrustTests(unittest.TestCase):
    def state(self, man):
        return ar.release_state(release(FULL), man, release_tag=TAG, electron_version=E, upstream_sha=SHA)

    def test_manifest_cannot_shrink_the_matrix(self):
        man = manifest()
        man["expectedAssetNames"] = man["expectedAssetNames"][:3]
        self.assertEqual(self.state(man)["state"], ar.STATE_INVALID)

    def test_manifest_reporting_incomplete_is_not_complete(self):
        man = manifest()
        man["complete"] = False
        self.assertEqual(self.state(man)["state"], ar.STATE_INVALID)

    def test_manifest_with_problems_is_not_complete(self):
        man = manifest()
        man["problems"] = ["unexpected asset x.zip"]
        self.assertEqual(self.state(man)["state"], ar.STATE_INVALID)

    def test_clean_manifest_is_complete(self):
        man = manifest()
        man.update(complete=True, problems=[])
        self.assertEqual(self.state(man)["state"], ar.STATE_COMPLETE)


class ReleaseStateExitCodeTests(unittest.TestCase):
    def run_main(self, state, *flags):
        result = {"releaseTag": TAG, "state": state, "complete": state == ar.STATE_COMPLETE,
                  "draft": state == ar.STATE_DRAFT, "missingAssets": [], "reasons": []}
        with mock.patch.object(ar.GitHub, "inspect_release", return_value=result), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return release_state.main(["--tag", TAG, "--electron-version", E, *flags])

    def test_require_assemblable(self):
        self.assertEqual(self.run_main(ar.STATE_ABSENT, "--require-assemblable"), 0)
        self.assertEqual(self.run_main(ar.STATE_DRAFT, "--require-assemblable"), 0)
        self.assertEqual(self.run_main(ar.STATE_COMPLETE, "--require-assemblable"), 1)
        self.assertEqual(self.run_main(ar.STATE_INCOMPLETE, "--require-assemblable"), 1)

    def test_require_complete(self):
        self.assertEqual(self.run_main(ar.STATE_COMPLETE, "--require-complete"), 0)
        self.assertEqual(self.run_main(ar.STATE_DRAFT, "--require-complete"), 1)
        self.assertEqual(self.run_main(ar.STATE_INVALID, "--require-complete"), 1)


class UntaggedRunTitleTests(unittest.TestCase):
    def decide(self, runs, release_tag=TAG):
        return wu.decide(upstream_ref="v1.9.5", release_tag=release_tag, state={"state": ar.STATE_ABSENT},
                         workflow_states=ACTIVE, runs=runs, packaging_sha=HEAD, now=NOW)

    def run_(self, title, status="in_progress", conclusion=None):
        return {"id": 9, "display_title": title, "status": status, "conclusion": conclusion, "run_attempt": 1,
                "head_sha": HEAD, "created_at": ts(1), "updated_at": ts(1), "html_url": "x"}

    def test_publish_run_without_release_tag_is_seen_in_flight(self):
        out = self.decide([self.run_("RealTimeX Whisper Addon v1.9.5")])
        self.assertEqual((out["action"], out["reason"]), ("none", "in-flight:in_progress"))

    def test_untitled_run_counts_only_for_the_default_tag(self):
        out = self.decide([self.run_("RealTimeX Whisper Addon v1.9.5")], release_tag="realtimex-v1.9.5-electron-42.0.0")
        self.assertEqual(out["action"], "dispatch")

    def test_untagged_dry_run_is_ignored(self):
        out = self.decide([self.run_("RealTimeX Whisper Addon v1.9.5 (dry run)")])
        self.assertEqual(out["action"], "dispatch")


class VariableErrorTests(unittest.TestCase):
    def gh_raising(self, status):
        gh = ar.GitHub(token="x")

        def boom(path, params=None):
            raise ar.GitHubError(status, path, "")
        gh.get = boom
        return gh

    def test_forbidden_means_unreadable(self):
        self.assertIsNone(self.gh_raising(403).variable("o/r", "V"))

    def test_server_error_is_raised_not_defaulted(self):
        with self.assertRaises(ar.GitHubError):
            self.gh_raising(502).variable("o/r", "V")


class PaginationTests(unittest.TestCase):
    def test_unfinished_runs_reads_every_page(self):
        pages = {("queued", 1): [{"id": i} for i in range(100)], ("queued", 2): [{"id": 100}],
                 ("in_progress", 1): [{"id": 200}]}

        class FakeGitHub:
            def get(self, path, params):
                return {"workflow_runs": pages.get((params["status"], params["page"]), [])}

        runs = br.unfinished_runs(FakeGitHub(), "o/r", "w.yml")
        self.assertEqual(len(runs), 102)

    def test_workflow_runs_reads_every_page(self):
        gh = ar.GitHub(token="x")
        calls = []

        def get(path, params=None):
            calls.append(params["page"])
            n = 100 if params["page"] < 3 else 7
            return {"workflow_runs": [{"id": params["page"] * 1000 + i} for i in range(n)]}
        gh.get = get
        self.assertEqual(len(gh.workflow_runs("o/r", "w.yml")), 207)
        self.assertEqual(calls, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
