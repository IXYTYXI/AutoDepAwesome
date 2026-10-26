"""Replace an installed Awesome Deploy package with a verified release copy.

Run this with the target virtual environment's Python, after the frontend
release build and its UI verification have passed. The source checkout is
never modified.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from uuid import uuid4


PACKAGE = "awesome_deploy"
DIST_NAME = "awesome-deploy"
OBFUSCATOR_VERSION = "5.9.0"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Expected an ordinary directory: {root}")
    found = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError(f"Symbolic link in release input: {name}")
        for name in filenames:
            file = Path(current) / name
            if file.is_symlink() or not file.is_file() or file.stat().st_nlink != 1:
                raise ValueError(f"Non-regular release input: {file}")
            found[file.relative_to(root).as_posix()] = file
    return found


def _checked_static(source: Path, protected: Path) -> dict[str, Path]:
    source_files = _files(source)
    protected_files = _files(protected)
    manifest_path = protected_files.pop("manifest.json", None)
    if manifest_path is None:
        raise ValueError("Protected static manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("format") != 1
            or manifest.get("tool") != {"name": "javascript-obfuscator", "version": OBFUSCATOR_VERSION}):
        raise ValueError("Unexpected protected static manifest format")
    records = manifest.get("files")
    if not isinstance(records, dict) or set(records) != set(source_files) or set(records) != set(protected_files):
        raise ValueError("Protected static file list differs from the installed package")
    for name, record in records.items():
        original = source_files[name].read_bytes()
        result = protected_files[name].read_bytes()
        if (not isinstance(record, dict)
                or record.get("sourceSha256") != _digest(original)
                or record.get("sourceBytes") != len(original)):
            raise ValueError(f"Protected static source digest mismatch: {name}")
        if record.get("sha256") != _digest(result) or record.get("bytes") != len(result):
            raise ValueError(f"Protected static output digest mismatch: {name}")
        if record.get("transformed") is not name.endswith(".js"):
            raise ValueError(f"Protected static transform flag mismatch: {name}")
    return protected_files


def _ignore_cache(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith(".pyc")}


def build_protected_copy(source_package: Path, protected_static: Path, output_package: Path) -> dict:
    """Build a source-free package copy, leaving the installed package intact."""
    source_package = Path(source_package)
    protected_static = Path(protected_static)
    output_package = Path(output_package)
    if output_package.exists() or output_package.is_symlink():
        raise FileExistsError(f"Release output already exists: {output_package}")
    source_files = _files(source_package)
    if "__init__.py" not in source_files:
        raise ValueError("The installed package is missing __init__.py")
    protected_files = _checked_static(source_package / "static", protected_static)
    output_package.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source_package, output_package, ignore=_ignore_cache)
        for name, protected_file in protected_files.items():
            (output_package / "static" / name).write_bytes(protected_file.read_bytes())
        python = {}
        for relative, original in sorted(source_files.items()):
            if not relative.endswith(".py"):
                continue
            target = output_package / relative
            compiled = target.with_suffix(".pyc")
            py_compile.compile(str(target), cfile=str(compiled),
                               dfile=f"{PACKAGE}/{relative}", doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            python[relative] = {"sourceSha256": _digest(original.read_bytes()),
                                "bytecodeSha256": _digest(compiled.read_bytes())}
            target.unlink()
        if not python or any(name.endswith(".py") for name in _files(output_package)):
            raise ValueError("Release copy still contains Python source")
        return {"format": 1, "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}",
                "pythonModules": len(python), "python": python,
                "staticFiles": len(protected_files)}
    except BaseException:
        shutil.rmtree(output_package, ignore_errors=True)
        raise


def _record_line(relative: str, file: Path) -> list[str]:
    data = file.read_bytes()
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return [relative, f"sha256={encoded}", str(len(data))]


def _replacement_record(record: Path, package: Path, protection: Path) -> bytes:
    old = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    entries = [row for row in old if row and not row[0].startswith(f"{PACKAGE}/")
               and row[0] != f"{protection.parent.name}/PROTECTION.json"]
    for relative, file in sorted(_files(package).items()):
        entries.append(_record_line(f"{PACKAGE}/{relative}", file))
    entries.append(_record_line(f"{protection.parent.name}/PROTECTION.json", protection))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(entries)
    return output.getvalue().encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        pending = Path(stream.name)
        stream.write(content)
    try:
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _smoke(path: Path | None) -> None:
    prefix = f"import sys; sys.path.insert(0, {str(path)!r}); " if path else ""
    code = prefix + ("import awesome_deploy.api, awesome_deploy.cli, awesome_deploy.worker, "
                     "awesome_deploy.team_server, awesome_deploy.intake_server; "
                     "from awesome_deploy.api import STATIC; assert (STATIC / 'index.html').is_file()")
    subprocess.run([sys.executable, "-I", "-c", code], cwd=sys.prefix,
                   capture_output=True, text=True, check=True)


def protect_current_venv(protected_static: Path) -> dict:
    """Install the protected copy into this non-editable virtual environment."""
    if sys.prefix == sys.base_prefix:
        raise ValueError("Run with the target virtual environment's Python")
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    distribution = metadata.distribution(DIST_NAME)
    package = purelib / PACKAGE
    metadata_file = next(
        (file for file in distribution.files or ()
         if file.name == "METADATA" and file.parts[0].endswith(".dist-info")),
        None,
    )
    if metadata_file is None:
        raise ValueError("Installed package metadata is missing")
    dist_info = Path(distribution.locate_file(metadata_file)).resolve().parent
    if package.resolve().parent != purelib or dist_info.parent != purelib:
        raise ValueError("Install awesome-deploy non-editably into this virtual environment first")
    if (dist_info / "direct_url.json").exists():
        direct_url = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
        if direct_url.get("dir_info", {}).get("editable"):
            raise ValueError("Editable installs cannot be protected")
    record = dist_info / "RECORD"
    if not record.is_file():
        raise ValueError("Installed package RECORD is missing")
    if (dist_info / "PROTECTION.json").exists():
        raise ValueError("This installation is already protected; reinstall before rebuilding")
    if any(purelib.glob(".awesome_deploy_protect_*")):
        raise ValueError("An interrupted protection run is present; inspect it before retrying")
    staging = purelib / f".awesome_deploy_protect_{uuid4().hex}"
    backup = staging / "backup"
    prepared = staging / PACKAGE
    original_record = record.read_bytes()
    swapped = False
    try:
        report = build_protected_copy(package, protected_static, prepared)
        _smoke(staging)
        os.replace(package, backup)
        try:
            os.replace(prepared, package)
            swapped = True
            _smoke(None)
            protection = dist_info / "PROTECTION.json"
            _atomic_write(protection, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
            _atomic_write(record, _replacement_record(record, package, protection))
        except BaseException:
            if swapped:
                os.replace(package, prepared)
            os.replace(backup, package)
            _atomic_write(record, original_record)
            (dist_info / "PROTECTION.json").unlink(missing_ok=True)
            raise
        shutil.rmtree(backup)
        return report
    finally:
        if not backup.exists():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            # Preserve the original installation if restoration needs operator attention.
            print(f"Original package retained at {backup}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protected-static", type=Path, required=True)
    args = parser.parse_args()
    report = protect_current_venv(args.protected_static)
    print(json.dumps({"pythonModules": report["pythonModules"],
                      "staticFiles": report["staticFiles"],
                      "pythonVersion": report["pythonVersion"]}, sort_keys=True))


if __name__ == "__main__":
    main()
