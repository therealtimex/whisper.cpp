"""Tests for check_runtime_probe.py and prepare_runtime_probe.py.

Run: python3 -m unittest discover -s realtimex/tests -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import check_runtime_probe as crp  # noqa: E402
import prepare_runtime_probe as prp  # noqa: E402

JFK = "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country."


def lines(*steps):
    return [json.dumps(s) + "\n" for s in steps]


def good_run(**overrides):
    steps = [
        {"step": "env", "platform": "win32", "arch": "arm64", "electron": "44.10.2", "napi": "10"},
        {"step": "loaded", "exports": ["initialize"]},
        {"step": "devices", "devices": [{"name": "CPU"}]},
        {"step": "wav", "samples": 176000},
        {"step": "initialized"},
        {"step": "transcribed-legacy", "text": JFK},
        {"step": "session-initialized"},
        {"step": "transcribed-session", "text": JFK},
        {"step": "session-destroyed"},
        {"step": "disposed"},
        {"step": "done", "ms": 1234},
    ]
    for s in steps:
        s.update(overrides.get(s["step"], {}))
    return [s for s in steps if s["step"] not in overrides.get("drop", [])]


class CheckRuntimeProbeTests(unittest.TestCase):
    def test_full_run_passes(self):
        v = crp.judge(lines(*good_run()), 0)
        self.assertTrue(v["passed"], v["problems"])
        self.assertEqual((v["platform"], v["arch"], v["devices"]), ("win32", "arm64", ["CPU"]))

    def test_abort_at_exit_fails(self):
        v = crp.judge(lines(*good_run()), 134)
        self.assertFalse(v["passed"])
        self.assertIn("134", v["problems"][0])

    def test_empty_transcript_fails(self):
        v = crp.judge(lines(*good_run(**{"transcribed-legacy": {"text": ""}})), 0)
        self.assertFalse(v["passed"])
        self.assertTrue(any("transcribed-legacy" in p for p in v["problems"]))

    def test_missing_session_step_fails(self):
        v = crp.judge(lines(*good_run(drop=["transcribed-session"])), 0)
        self.assertFalse(v["passed"])

    def test_crash_before_done_fails(self):
        v = crp.judge(lines(*good_run(drop=["initialized", "transcribed-legacy", "session-initialized",
                                            "transcribed-session", "session-destroyed", "disposed", "done"])), 132)
        self.assertFalse(v["passed"])
        self.assertTrue(any("initialized" in p for p in v["problems"]))

    def test_devices_error_fails(self):
        steps = good_run()
        steps.insert(3, {"step": "devices-error", "message": "boom"})
        self.assertFalse(crp.judge(lines(*steps), 0)["passed"])

    def test_non_json_noise_is_ignored(self):
        noise = ["whisper_init_from_file_with_params: loading model\n"]
        self.assertTrue(crp.judge(noise + lines(*good_run()), 0)["passed"])


class PrepareRuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(prp.ADDON_NAME, b"\x7fELF fake")
            z.writestr("_realtimexWhisperAddonMetadata.json", "{}")
        self.zip_bytes = buf.getvalue()
        self.name = prp.asset_name("linux", "x64", "41.0.3")
        self.manifest = {
            "electronRuntimeVersion": "41.0.3",
            "upstreamSha": "d1be6fde",
            "assets": [{"name": self.name, "sha256": hashlib.sha256(self.zip_bytes).hexdigest()}],
        }
        self.models = {n: b"model " + n.encode() for n in prp.MODELS}

    def tearDown(self):
        self.tmp.cleanup()

    def fake_fetch(self, url, dest):
        if url.endswith(self.name):
            data = self.zip_bytes
        elif url.endswith("jfk.wav"):
            data = b"RIFF"
        else:
            data = self.models[url.rsplit("/", 1)[1]]
        with open(dest, "wb") as fh:
            fh.write(data)

    def gh(self, release=None):
        gh = mock.Mock()
        gh.find_release.return_value = release if release is not None else {
            "draft": False, "assets": [{"name": self.name, "browser_download_url": "https://x/" + self.name}]}
        gh.read_manifest.return_value = self.manifest
        return gh

    def run_prepare(self, gh):
        pinned = {n: (u, hashlib.sha256(self.models[n]).hexdigest()) for n, (u, _) in prp.MODELS.items()}
        with mock.patch.object(prp, "fetch", self.fake_fetch), mock.patch.object(prp, "MODELS", pinned):
            return prp.prepare(gh, "o/r", "realtimex-v1.9.5", "linux", "x64",
                               os.path.join(self.dir, "out"), os.path.join(self.dir, "cache"))

    def test_extracts_the_checked_addon(self):
        out = self.run_prepare(self.gh())
        with open(out["addon"], "rb") as fh:
            self.assertEqual(fh.read(), b"\x7fELF fake")
        self.assertTrue(out["model"].endswith(prp.SPEECH_MODEL))
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(out["model"]), "ggml-silero-v6.2.0.bin")))

    def test_zip_not_matching_the_manifest_is_refused(self):
        self.manifest["assets"][0]["sha256"] = "0" * 64
        with self.assertRaises(SystemExit):
            self.run_prepare(self.gh())

    def test_draft_release_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_prepare(self.gh(release={"draft": True, "assets": []}))

    def test_model_with_wrong_hash_is_refused(self):
        with mock.patch.object(prp, "fetch", self.fake_fetch):  # real pins vs fake bytes
            with self.assertRaises(SystemExit):
                prp.prepare(self.gh(), "o/r", "realtimex-v1.9.5", "linux", "x64",
                            os.path.join(self.dir, "out"), os.path.join(self.dir, "cache"))


if __name__ == "__main__":
    unittest.main()
