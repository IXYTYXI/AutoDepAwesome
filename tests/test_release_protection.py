"""The release copy must run without shipping readable application sources."""

import hashlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.release.protect_install import _replacement_record, build_protected_copy


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    package = tmp_path / "source" / "awesome_deploy"
    static = package / "static"
    static.mkdir(parents=True)
    (package / "__init__.py").write_text("from .module import value\n")
    (package / "module.py").write_text("value = 42\n")
    (package / "schema").mkdir()
    (package / "schema" / "app.json").write_text('{"version": 1}\n')
    source_assets = {"app.js": b"globalThis.value = 42;\n", "index.html": b"<html></html>\n"}
    protected_assets = {"app.js": b"globalThis['value']=42;\n", "index.html": source_assets["index.html"]}
    for name, data in source_assets.items():
        (static / name).write_bytes(data)
    protected = tmp_path / "protected-static"
    protected.mkdir()
    for name, data in protected_assets.items():
        (protected / name).write_bytes(data)
    manifest = {"format": 1, "tool": {"name": "javascript-obfuscator", "version": "5.9.0"},
                "files": {name: {"sourceSha256": _digest(source_assets[name]),
                                  "sourceBytes": len(source_assets[name]), "sha256": _digest(data),
                                  "bytes": len(data), "transformed": name.endswith(".js")}
                          for name, data in protected_assets.items()}}
    (protected / "manifest.json").write_text(json.dumps(manifest))
    return package, protected, tmp_path / "release" / "awesome_deploy"


def test_release_copy_imports_from_bytecode_and_serves_protected_assets(tmp_path):
    source, protected, output = _fixture(tmp_path)

    report = build_protected_copy(source, protected, output)

    assert (source / "module.py").exists()
    assert not list(output.rglob("*.py"))
    assert (output / "__init__.pyc").exists()
    assert (output / "module.pyc").exists()
    assert (output / "static" / "app.js").read_bytes() == (protected / "app.js").read_bytes()
    assert (output / "schema" / "app.json").read_text() == '{"version": 1}\n'
    assert report["pythonModules"] == 2
    env = {**os.environ, "PYTHONPATH": str(output.parent)}
    result = subprocess.run([sys.executable, "-c", "import awesome_deploy; print(awesome_deploy.value)"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "42"


def test_release_copy_rejects_changed_static_source_without_output(tmp_path):
    source, protected, output = _fixture(tmp_path)
    (source / "static" / "app.js").write_text("changed")

    with pytest.raises(ValueError, match="source digest"):
        build_protected_copy(source, protected, output)

    assert not output.exists()


def test_release_copy_rejects_extra_protected_file_without_output(tmp_path):
    source, protected, output = _fixture(tmp_path)
    (protected / "extra.js").write_text("unexpected")

    with pytest.raises(ValueError, match="file list"):
        build_protected_copy(source, protected, output)

    assert not output.exists()


def test_install_record_tracks_protected_files_for_future_uninstall(tmp_path):
    source, protected, output = _fixture(tmp_path)
    build_protected_copy(source, protected, output)
    dist_info = tmp_path / "release" / "awesome_deploy-0.1.dist-info"
    dist_info.mkdir()
    record = dist_info / "RECORD"
    record.write_text("awesome_deploy/module.py,sha256=old,11\n"
                      "awesome_deploy-0.1.dist-info/RECORD,,\n")
    protection = dist_info / "PROTECTION.json"
    protection.write_text('{"format": 1}\n')

    rows = list(csv.reader(io.StringIO(_replacement_record(record, output, protection).decode())))
    names = [row[0] for row in rows]

    assert "awesome_deploy/module.py" not in names
    assert "awesome_deploy/module.pyc" in names
    assert "awesome_deploy/static/app.js" in names
    assert "awesome_deploy-0.1.dist-info/PROTECTION.json" in names
    assert names.count("awesome_deploy-0.1.dist-info/RECORD") == 1
