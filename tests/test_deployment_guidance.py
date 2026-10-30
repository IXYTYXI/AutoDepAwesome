"""Current copyable manifests must follow the new-project contract and real parser."""
from pathlib import Path
from html import unescape
import re

import pytest
import yaml

from awesome_deploy.config import Settings
from awesome_deploy.manifest import load_manifest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["simple-web", "frontend-api"])
def test_current_repository_examples_use_v2_and_pass_runtime_validation(name):
    folder = ROOT / "examples" / name
    raw = (folder / "awesome.yaml").read_text()
    assert yaml.safe_load(raw)["schemaVersion"] == 2, "New-project examples must not select legacy schema 1"
    manifest = load_manifest(raw, Settings())
    for service in manifest["services"].values():
        build = service["build"]
        assert (folder / build["context"] / build["dockerfile"]).is_file()


@pytest.mark.parametrize("relative", ["README.md", "skills/awesome-deploy-integration/references/repository-deployment.md"])
def test_copyable_document_manifests_follow_current_new_project_contract(relative):
    path = ROOT / relative
    assert path.is_file(), "The distributable skill needs a self-contained repository deployment guide"
    manifests = [raw for raw in re.findall(r"```ya?ml\n(.*?)```", path.read_text(), re.S)
                 if "schemaVersion:" in raw]
    assert manifests, "The guide must contain a complete, usable manifest"
    for raw in manifests:
        assert yaml.safe_load(raw)["schemaVersion"] == 2
        load_manifest(raw, Settings())


def test_downloadable_guide_manifest_matches_the_new_project_contract():
    raw = (ROOT / "src/awesome_deploy/static/deployment-guide.txt").read_text()
    manifest = raw.split("文件：仓库根目录 awesome.yaml\n", 1)[1].split("\n文件：仓库根目录 Dockerfile", 1)[0]
    assert yaml.safe_load(manifest)["schemaVersion"] == 2
    load_manifest(manifest, Settings())


def test_bundled_protocol_examples_use_current_schema_and_pass_runtime_validation():
    raw = (ROOT / "src/awesome_deploy/static/manual-protocol.html").read_text()
    examples = re.findall(r'<pre><code class="language-yaml">(.*?)</code></pre>', raw, re.S)
    assert len(examples) >= 3, "Keep single-service, persistent, and frontend/API examples usable"
    for example in examples:
        manifest = unescape(example)
        assert yaml.safe_load(manifest)["schemaVersion"] == 2
        # The persistent example explicitly requires the quota-enabled host described by the protocol.
        load_manifest(manifest, Settings(storage_backend="xfs-project"))
