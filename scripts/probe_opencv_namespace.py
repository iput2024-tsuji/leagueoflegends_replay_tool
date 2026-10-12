"""Compile a fixed synthetic TU and inspect COFF; never link or execute it.

This diagnostic does not change or validate the OpenCV release recipe. Command
output is bounded in memory; only diagnostic codes and its hash are retained.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

from scripts.prepare_opencv_wheel import (
    COMPILER_FLAG_ENVIRONMENT,
    REQUIRED_SOURCE_DATE_EPOCH,
    REQUIRED_TOOLSET_VERSION,
    _capture_msvc_tools,
    _computer_name_crc32,
    _policy,
    _select_msvc_instance,
)

OUTPUT_NAME = "opencv-namespace-probe"
FILE_LIMIT = 32 * 1024 * 1024
TOTAL_LIMIT = 256 * 1024 * 1024
FILE_COUNT_LIMIT = 100
COFF_LIMIT = 1024 * 1024
LOG_LIMIT = 64 * 1024
JSON_LIMIT = 1024 * 1024
SOURCE = b'''#include "probe.h"
namespace {
struct Local {
    virtual ~Local() {}
    virtual int read() const { return 7; }
};
}
extern "C" int namespace_probe(int input) {
    Local value;
    auto apply = [input](int value) { return value + input; };
    return apply(value.read());
}
'''
CREATOR = b'#include "probe.h"\n'
CASE_PATHS = ("case-a", "case-b-longer", "case-c/different-depth")


class ProbeError(ValueError):
    """Fixed internal codes; exception messages never enter the report."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _no_redirect(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ProbeError("redirected_path")


def _read(path: Path, limit: int) -> bytes:
    _no_redirect(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ProbeError("input_file_limit")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    if len(data) > limit or identity(before) != identity(after) or identity(after) != identity(path.stat()):
        raise ProbeError("input_changed_or_oversized")
    return data


def _fingerprint(path: Path, limit: int = FILE_LIMIT) -> dict:
    data = _read(path, limit)
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _unique_json(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError("duplicate_json_key")
        result[key] = value
    return result


def inspect_coff(data: bytes) -> dict:
    """Read namespace IDs from primary COFF symbols, excluding auxiliary rows."""
    if not 20 <= len(data) <= COFF_LIMIT:
        raise ProbeError("coff_size")
    machine, sections, _, offset, count, optional, _ = struct.unpack_from("<HHIIIHH", data)
    table_end = offset + count * 18
    if (machine != 0x8664 or not 1 <= sections <= 64 or optional != 0
            or offset < 20 + sections * 40 or not 1 <= count <= 4096
            or table_end + 4 > len(data)):
        raise ProbeError("coff_header")
    for index in range(sections):
        size, raw = struct.unpack_from("<II", data, 20 + index * 40 + 16)
        if size and (raw < 20 + sections * 40 or raw + size > offset):
            raise ProbeError("coff_section_bounds")
    string_size = struct.unpack_from("<I", data, table_end)[0]
    if string_size < 4 or table_end + string_size != len(data):
        raise ProbeError("coff_string_bounds")
    strings = data[table_end:]
    namespaces, lambdas = set(), set()
    index = 0
    while index < count:
        current = offset + index * 18
        short = data[current:current + 8]
        auxiliary = data[current + 17]
        if index + auxiliary >= count:
            raise ProbeError("coff_auxiliary_bounds")
        if short[:4] == b"\0" * 4:
            relative = struct.unpack_from("<I", short, 4)[0]
            if not 4 <= relative < string_size:
                raise ProbeError("coff_name_bounds")
            end = strings.find(b"\0", relative)
            if end < 0:
                raise ProbeError("coff_name_termination")
            name = strings[relative:end]
        else:
            name = short.split(b"\0", 1)[0]
        namespaces.update(match.decode("ascii").lower() for match in re.findall(rb"\?A0x[0-9a-f]{8}(?![0-9a-f])", name, re.IGNORECASE))
        lambdas.update(match.decode("ascii").lower() for match in re.findall(rb"<lambda_[0-9a-fA-F]{32}>", name))
        index += 1 + auxiliary
    if len(namespaces) != 1:
        raise ProbeError("coff_namespace_not_unique")
    return {"namespace_id": next(iter(namespaces)), "lambda_ids": sorted(lambdas)}


def predict_namespace(source: Path, *, pch: bool, seed: int) -> str:
    payload = str(source.absolute()).lower().encode("utf-8") + bytes([int(pch)])
    return f"?a0x{zlib.crc32(payload, seed) ^ 0xFFFFFFFF:08x}"


def _inventory(root: Path) -> list[dict]:
    files, total = [], 0
    _no_redirect(root)
    for path in sorted(root.rglob("*")):
        _no_redirect(path)
        if path.is_dir():
            continue
        record = _fingerprint(path)
        total += record["size"]
        captured = path.suffix in {".cpp", ".h", ".obj"}
        files.append({"path": path.relative_to(root).as_posix(), **record,
                      "artifact_scope": "synthetic_source_or_coff" if captured else "private_hash_only"})
        if len(files) > FILE_COUNT_LIMIT or total > TOTAL_LIMIT:
            raise ProbeError("output_total_limit")
    return files


def _check_sizes(root: Path) -> None:
    total, count = 0, 0
    for path in root.rglob("*"):
        _no_redirect(path)
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            continue
        total += info.st_size
        count += 1
        if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_LIMIT:
            raise ProbeError("output_file_limit")
        if count > FILE_COUNT_LIMIT or total > TOTAL_LIMIT:
            raise ProbeError("output_total_limit")


def _command(args: list[str], cwd: Path, *, timeout: int = 45, output_root: Path | None = None) -> tuple[dict, bytes]:
    # No caller-supplied flags and no environment content enter a saved log.
    environment = os.environ.copy()
    for key in (*COMPILER_FLAG_ENVIRONMENT, "INCLUDE", "LIB", "LIBPATH"):
        environment.pop(key, None)
    process = subprocess.Popen(
        args, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = bytearray()
    overflow, read_error = threading.Event(), threading.Event()

    def receive():
        try:
            while chunk := process.stdout.read(4096):
                if len(output) + len(chunk) > LOG_LIMIT:
                    overflow.set()
                    return
                output.extend(chunk)
        except OSError:
            read_error.set()
        finally:
            process.stdout.close()

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if overflow.is_set():
                raise ProbeError("command_output_limit")
            if time.monotonic() >= deadline:
                raise ProbeError("command_timeout")
            if output_root is not None:
                _check_sizes(output_root)
            time.sleep(0.05)
        reader.join(timeout=5)
        if reader.is_alive() or read_error.is_set():
            raise ProbeError("command_output_read")
        if overflow.is_set():
            raise ProbeError("command_output_limit")
        data = bytes(output)
        codes = sorted({item.decode("ascii") for item in re.findall(rb"\b[CD][0-9]{4}\b", data)})
        return {"returncode": process.returncode, "diagnostic_codes": codes,
                "unknown_option_D9002": "D9002" in codes,
                "output_size": len(data), "output_sha256": hashlib.sha256(data).hexdigest()}, data
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        # A pending Windows pipe read can also lock FileIO.close. Its daemon
        # owns the stream until EOF or this short-lived probe process exits.
        reader.join(timeout=5)


def _write_json(path: Path, report: dict) -> None:
    data = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > JSON_LIMIT:
        raise ProbeError("report_size")
    with path.open("xb") as stream:
        stream.write(data)


def _compile(compiler: Path, case: Path, mode: str, pch: str, report: dict) -> None:
    source = case / ("creator.cpp" if pch == "create" else "probe.cpp")
    tag = f"{mode}-{pch}"
    obj, cache = case / f"{tag}.obj", case / f"{mode}.pch"
    flags = ["/nologo", "/c", "/Od", "/Z7", "/TP", "/X"]
    if mode == "deterministic":
        flags.append("/experimental:deterministic")
    flags += ["/Y-"] if pch == "none" else [f"/Y{'c' if pch == 'create' else 'u'}probe.h", f"/Fp{cache}"]
    entry = {"case": case.relative_to(report["_root"]).as_posix(), "mode": mode, "pch": pch,
             "fixed_flags": [flag for flag in flags if not flag.startswith("/Fp")], "status": "failed"}
    report["commands"].append(entry)
    try:
        result, _ = _command([str(compiler), *flags, f"/Fo{obj}", str(source)], case, output_root=report["_root"])
        entry.update(result, status="success" if result["returncode"] == 0 else "failed")
    except Exception as error:
        entry["error"] = {"code": "command_failed", "exception_type": type(error).__name__}
        _write_json(case / f"{tag}.log.json", entry)
        raise
    _write_json(case / f"{tag}.log.json", entry)
    if entry["returncode"] != 0:
        raise ProbeError("compile_nonzero")
    _check_sizes(report["_root"])
    if pch == "create":
        _fingerprint(cache)
        return
    observed = inspect_coff(_read(obj, COFF_LIMIT))
    host = predict_namespace(source, pch=pch == "use", seed=int(report["computer_name_crc32"]["crc32"], 16))
    zero = predict_namespace(source, pch=pch == "use", seed=0)
    path_bytes = str(source.absolute()).lower().encode("utf-8")
    report["observations"].append({
        "case": entry["case"], "mode": mode, "pch": pch == "use", **observed,
        "source_absolute_path": str(source.absolute()),
        "source": _fingerprint(source), "source_path_lower_utf8_sha256": hashlib.sha256(path_bytes).hexdigest(),
        "source_path_lower_utf8_size": len(path_bytes), "host_seed_prediction": host,
        "zero_seed_prediction": zero, "matches_host_seed": observed["namespace_id"] == host,
        "matches_zero_seed": observed["namespace_id"] == zero,
        "option_accepted_without_D9002": not entry["unknown_option_D9002"],
    })


def run_probe(temp: Path) -> dict:
    report = {"schema_version": 1, "purpose": "synthetic_namespace_diagnostics_only",
              "status": "failed", "product_build_evidence": False, "linked": False,
              "generated_native_artifacts_executed": False, "commands": [], "observations": [], "errors": [],
              "unverified": ["product_recipe_and_wheel", "loaded_tool_dll_identity", "supported_flag_contract"],
              "model": {"path": "lower_absolute_utf8", "suffix": "one_byte_0_no_pch_1_pch",
                        "checksum": "zlib_crc32_then_xor_ffffffff", "seeds": ["GetComputerNameW_crc32", "zero"]},
              "computer_name_crc32": {"source": "GetComputerNameW", "encoding": "utf-8_no_nul_original_case",
                                      "status": "not_observed", "crc32": None},
              "limits": {"per_file_bytes": FILE_LIMIT, "total_bytes": TOTAL_LIMIT, "file_count": FILE_COUNT_LIMIT,
                         "coff_bytes": COFF_LIMIT, "command_output_bytes": LOG_LIMIT, "json_bytes": JSON_LIMIT}}
    root = temp / OUTPUT_NAME
    _no_redirect(temp)
    if not temp.is_absolute() or not temp.is_dir():
        raise ProbeError("runner_temp_invalid")
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", temp.drive) is None:
        raise ProbeError("runner_temp_not_local_drive")
    root.mkdir(exist_ok=False)
    report["_root"] = root
    repository = Path(__file__).resolve().parents[1]
    inputs = {"probe_script": Path(__file__), "prepare_script": repository / "scripts/prepare_opencv_wheel.py",
              "components": repository / "compliance/components.json"}
    compiler = None
    sources = {}
    try:
        if any(os.environ.get(key) for key in COMPILER_FLAG_ENVIRONMENT):
            raise ProbeError("inherited_compiler_flags")
        report["inputs_before"] = {key: _fingerprint(path) for key, path in inputs.items()}
        git, output = _command(["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"], root, timeout=10)
        checkout = output.strip().decode("ascii")
        github = os.environ.get("GITHUB_SHA", "")
        if git["returncode"] != 0 or re.fullmatch(r"[0-9a-f]{40}", checkout) is None:
            raise ProbeError("checkout_sha_unavailable")
        report["actual_commit_sha"] = checkout
        if github != checkout:
            raise ProbeError("checkout_sha_mismatch")
        report["github_sha"] = github
        lock = json.loads(_read(inputs["components"], JSON_LIMIT), object_pairs_hook=_unique_json)
        policy = _policy(lock)
        _, compiler, tools = _select_msvc_instance(policy)
        report["toolset_version"] = REQUIRED_TOOLSET_VERSION
        report["tools_before"] = tools
        try:
            crc = _computer_name_crc32()
        except Exception:
            report["computer_name_crc32"]["status"] = "error"
            raise
        if crc is None or re.fullmatch(r"[0-9a-f]{8}", crc) is None:
            raise ProbeError("computer_name_crc32_unavailable")
        report["computer_name_crc32"].update(status="observed", crc32=crc)
        for relative in CASE_PATHS:
            case = root / relative
            case.mkdir(parents=True)
            for name, data in (("probe.cpp", SOURCE), ("creator.cpp", CREATOR), ("probe.h", b"")):
                path = case / name
                with path.open("xb") as stream:
                    stream.write(data)
                os.utime(path, (int(REQUIRED_SOURCE_DATE_EPOCH),) * 2)
                sources[path.relative_to(root).as_posix()] = _fingerprint(path)
            for mode in ("control", "deterministic"):
                for pch in ("none", "create", "use"):
                    _compile(compiler, case, mode, pch, report)
        rows = report["observations"]
        report["summary"] = {
            "consumer_count": len(rows),
            "control_all_host_seed": all(row["matches_host_seed"] for row in rows if row["mode"] == "control"),
            "deterministic_all_zero_seed": all(row["matches_zero_seed"] for row in rows if row["mode"] == "deterministic"),
            "deterministic_accepted_without_D9002": all(not row["unknown_option_D9002"] for row in report["commands"] if row["mode"] == "deterministic"),
            "lambda_ids_equal_by_path_and_pch": all(
                len(pair) == 2 and bool(pair[0]["lambda_ids"]) and pair[0]["lambda_ids"] == pair[1]["lambda_ids"]
                for case in CASE_PATHS for pch in (False, True)
                for pair in [[row for row in rows if row["case"] == case and row["pch"] == pch]]
            ),
        }
        report["status"] = "completed"
    except Exception as error:
        report["errors"].append({"code": error.code if isinstance(error, ProbeError) else "probe_failed",
                                 "exception_type": type(error).__name__})
    finally:
        try:
            report["inputs_after"] = {key: _fingerprint(path) for key, path in inputs.items()}
            report["sources_before"] = sources
            report["sources_after"] = {key: _fingerprint(root / key) for key in sources}
            report["inputs_unchanged"] = report.get("inputs_before") == report["inputs_after"]
            report["sources_unchanged"] = report["sources_before"] == report["sources_after"]
            if compiler is not None:
                report["tools_after"] = _capture_msvc_tools(compiler)
                report["tools_unchanged"] = report.get("tools_before") == report["tools_after"]
            if not report["inputs_unchanged"] or not report["sources_unchanged"] or report.get("tools_unchanged") is False:
                raise ProbeError("inputs_changed")
            report["files"] = _inventory(root)
        except Exception as error:
            report["status"] = "failed"
            report["errors"].append({"code": "final_input_or_output_check_failed", "exception_type": type(error).__name__})
        del report["_root"]
        _write_json(root / "report.json", report)
    return report


def main() -> int:
    if (sys.argv[1:] or os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") not in {"pull_request", "workflow_dispatch"}
            or os.environ.get("OPENCV_NAMESPACE_PROBE_ONLY") != "1"):
        print("Namespace probe requires its Windows CI diagnostic job.")
        return 1
    try:
        report = run_probe(Path(os.environ["RUNNER_TEMP"]))
    except Exception as error:
        print(f"Namespace probe report unavailable: {type(error).__name__}.")
        return 1
    print(f"Namespace probe: {report['status']}; synthetic compile-only evidence.")
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
