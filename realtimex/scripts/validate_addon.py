#!/usr/bin/env python3
"""Gate a packaged whisper addon zip before it can be published.

Checks:
  - zip holds exactly whisper_addon.node and the metadata file, and the
    metadata matches the asset (name, platform, arch, Electron, upstream SHA,
    sha256 of the binary);
  - the binary format and CPU match the asset (Mach-O / ELF / PE header);
  - macOS: the minimum macOS version is not newer than the app's floor
    (--max-macos-deployment-target; the app supports macOS 13);
  - macOS: no exported weak node-addon-api (`__ZN4Napi*`) symbols. dyld
    coalesces weak definitions across addons, so an exported copy without C++
    exception handling turns another addon's Napi::Error into an abort
    (realtimex-ai-app#3229). On Linux the same symbols are reported as a
    warning only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from addon_release import (  # noqa: E402
    ADDON_NAME,
    DEFAULT_MACOS_DEPLOYMENT_TARGET,
    METADATA_NAME,
    asset_name,
    parse_version,
)

MACHO_CPU = {0x01000007: "x64", 0x0100000C: "arm64"}
ELF_MACHINE = {62: "x64", 183: "arm64"}
PE_MACHINE = {0x8664: "x64", 0xAA64: "arm64"}
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32

# Itanium mangling puts CV and ref qualifiers of a member function after
# `N`: a const method is __ZNK4Napi..., so match the optional qualifiers too.
WEAK_NAPI_MACHO = re.compile(r"\bweak external (__ZN[rVKRO]*4Napi\S*)")
WEAK_NAPI_ELF = re.compile(r"^\S*\s+[WV]\s+(_ZN[rVKRO]*4Napi\S*)", re.MULTILINE)
PLATFORM_MACOS = 1


def _version(encoded: int) -> str:
    return f"{encoded >> 16}.{(encoded >> 8) & 0xFF}.{encoded & 0xFF}"


def macho_slices(data: bytes) -> List[Tuple[str, Optional[str], Optional[int]]]:
    """Return (arch, minos, platform) for each Mach-O slice (one for thin binaries).

    `platform` is the LC_BUILD_VERSION platform (1 = macOS), or 1 for an old
    LC_VERSION_MIN_MACOSX binary, or None when neither command is present."""
    if len(data) < 32:
        raise ValueError("file too small for Mach-O")
    (magic_be,) = struct.unpack_from(">I", data, 0)
    if magic_be == 0xCAFEBABE:
        (count,) = struct.unpack_from(">I", data, 4)
        slices = []
        for i in range(count):
            _cpu, _sub, offset, size, _align = struct.unpack_from(">iiIII", data, 8 + i * 20)
            slices.extend(macho_slices(data[offset:offset + size]))
        return slices
    (magic,) = struct.unpack_from("<I", data, 0)
    if magic != 0xFEEDFACF:
        raise ValueError(f"not a 64-bit Mach-O (magic {magic:#x})")
    _magic, cputype, _sub, _filetype, ncmds, _size, _flags, _res = struct.unpack_from("<IiiIIIII", data, 0)
    arch = MACHO_CPU.get(cputype & 0xFFFFFFFF, f"cpu-{cputype:#x}")
    minos: Optional[str] = None
    platform: Optional[int] = None
    offset = 32
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, offset)
        if cmd == LC_BUILD_VERSION:
            platform, encoded, _sdk = struct.unpack_from("<III", data, offset + 8)
            minos = _version(encoded)
        elif cmd == LC_VERSION_MIN_MACOSX and minos is None:
            (encoded,) = struct.unpack_from("<I", data, offset + 8)
            minos = _version(encoded)
            platform = PLATFORM_MACOS
        if cmdsize <= 0:
            raise ValueError("corrupt Mach-O load command")
        offset += cmdsize
    return [(arch, minos, platform)]


def binary_info(data: bytes) -> Dict[str, Any]:
    if data[:4] == b"\x7fELF":
        if len(data) < 20:
            raise ValueError("ELF header is truncated")
        if data[4] != 2:
            raise ValueError("ELF is not 64-bit")
        endian = "<" if data[5] == 1 else ">"
        (machine,) = struct.unpack_from(endian + "H", data, 18)
        return {"format": "elf", "archs": [ELF_MACHINE.get(machine, f"machine-{machine}")]}
    if data[:2] == b"MZ":
        (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
        if data[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        (machine,) = struct.unpack_from("<H", data, pe_offset + 4)
        return {"format": "pe", "archs": [PE_MACHINE.get(machine, f"machine-{machine:#x}")]}
    slices = macho_slices(data)
    return {
        "format": "macho",
        "archs": [a for a, _, _ in slices],
        "minos": [m for _, m, _ in slices],
        "platforms": [p for _, _, p in slices],
    }


def weak_napi_exports(nm_output: str, fmt: str) -> List[str]:
    if fmt == "macho":
        return sorted({m.group(1) for line in nm_output.splitlines()
                       if "(undefined)" not in line for m in [WEAK_NAPI_MACHO.search(line)] if m})
    return sorted(set(WEAK_NAPI_ELF.findall(nm_output)))


def run_nm(path: str, fmt: str) -> Optional[str]:
    nm = shutil.which("nm")
    if not nm:
        return None
    args = [nm, "-m", path] if fmt == "macho" else [nm, "-D", "--defined-only", path]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"nm failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


EXPECTED_FORMAT = {"mac": "macho", "linux": "elf", "win": "pe"}


def validate(
    zip_path: str,
    *,
    platform: str,
    arch: str,
    electron_version: str,
    upstream_sha: Optional[str] = None,
    max_macos: str = DEFAULT_MACOS_DEPLOYMENT_TARGET,
    nm_output: Optional[str] = None,
    require_symbol_check: bool = True,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    report: Dict[str, Any] = {"zip": os.path.basename(zip_path), "errors": errors, "warnings": warnings}

    expected_name = asset_name(platform, arch, electron_version)
    if os.path.basename(zip_path) != expected_name:
        errors.append(f"asset name {os.path.basename(zip_path)} != {expected_name}")

    with zipfile.ZipFile(zip_path) as archive:
        names = sorted(archive.namelist())
        if names != sorted([ADDON_NAME, METADATA_NAME]):
            errors.append(f"zip entries {names} != {[ADDON_NAME, METADATA_NAME]}")
            return report
        data = archive.read(ADDON_NAME)
        try:
            metadata = json.loads(archive.read(METADATA_NAME))
        except ValueError as err:
            errors.append(f"metadata is not JSON: {err}")
            return report
        if not isinstance(metadata, dict):
            errors.append(f"metadata is {type(metadata).__name__}, not a JSON object")
            return report

    wanted = {"asset": expected_name, "platform": platform, "arch": arch, "electronRuntimeVersion": electron_version}
    if upstream_sha:
        wanted["upstreamSha"] = upstream_sha
    for key, value in wanted.items():
        if metadata.get(key) != value:
            errors.append(f"metadata {key}={metadata.get(key)!r}, expected {value!r}")
    digest = hashlib.sha256(data).hexdigest()
    if metadata.get("sha256") != digest:
        errors.append("metadata sha256 does not match whisper_addon.node")
    report["sha256"] = digest

    try:
        info = binary_info(data)
    except (ValueError, struct.error) as err:
        errors.append(f"unreadable binary: {err}")
        return report
    report.update(info)
    if info["format"] != EXPECTED_FORMAT[platform]:
        errors.append(f"binary format {info['format']} != {EXPECTED_FORMAT[platform]}")
    if info["archs"] != [arch]:
        errors.append(f"binary architectures {info['archs']} != [{arch!r}]")

    if info["format"] == "macho":
        for platform_id in info.get("platforms") or [None]:
            if platform_id != PLATFORM_MACOS:
                errors.append(f"Mach-O platform {platform_id} is not macOS ({PLATFORM_MACOS})")
        for minos in info.get("minos") or [None]:
            if minos is None:
                errors.append("no minimum macOS version load command")
            elif parse_version(minos) > parse_version(max_macos):
                errors.append(f"minimum macOS {minos} is newer than the app's floor {max_macos}")

    if info["format"] in ("macho", "elf"):
        if nm_output is None:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, ADDON_NAME)
                with open(path, "wb") as fh:
                    fh.write(data)
                nm_output = run_nm(path, info["format"])
        if nm_output is None:
            message = "nm not available; weak node-addon-api export check skipped"
            (errors if require_symbol_check and info["format"] == "macho" else warnings).append(message)
        else:
            weak = weak_napi_exports(nm_output, info["format"])
            report["weakNapiExports"] = len(weak)
            if weak:
                message = (
                    f"{len(weak)} exported weak node-addon-api symbols (build with hidden visibility, "
                    f"realtimex-ai-app#3229), e.g. {weak[0]}"
                )
                (errors if info["format"] == "macho" else warnings).append(message)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zip", required=True)
    p.add_argument("--platform", required=True, choices=sorted(EXPECTED_FORMAT))
    p.add_argument("--arch", required=True, choices=["x64", "arm64"])
    p.add_argument("--electron-version", required=True)
    p.add_argument("--upstream-sha", default="")
    p.add_argument("--max-macos-deployment-target", default=DEFAULT_MACOS_DEPLOYMENT_TARGET)
    p.add_argument("--nm-output", help="Use this saved nm output instead of running nm")
    args = p.parse_args(argv)

    nm_output = None
    if args.nm_output:
        with open(args.nm_output, encoding="utf-8") as fh:
            nm_output = fh.read()
    report = validate(
        args.zip,
        platform=args.platform,
        arch=args.arch,
        electron_version=args.electron_version,
        upstream_sha=args.upstream_sha or None,
        max_macos=args.max_macos_deployment_target,
        nm_output=nm_output,
    )
    print(json.dumps(report, indent=2))
    for warning in report["warnings"]:
        print(f"::warning::{report['zip']}: {warning}", file=sys.stderr)
    for error in report["errors"]:
        print(f"::error::{report['zip']}: {error}", file=sys.stderr)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
