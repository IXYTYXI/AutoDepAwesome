"""Validate the portable development contract against the actual platform."""
from hashlib import sha256
import json
from pathlib import Path

import pytest
import yaml

from awesome_deploy.config import Settings
from awesome_deploy.manifest import load_manifest
from awesome_deploy.runtime import DeploymentBackend


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/awesome-deploy-protocol"


def test_distributed_schema_is_the_official_v2_schema():
    assert (SKILL / "assets/awesome-v2.schema.json").read_bytes() == (
        ROOT / "src/awesome_deploy/schema/awesome-v2.json"
    ).read_bytes()


def test_contract_sources_have_not_drifted_from_the_reviewed_implementation():
    profile = json.loads((SKILL / "assets/platform-profile.json").read_text())
    assert profile["manifestSchemaVersion"] == 2
    assert profile["sourceSha256"]
    for relative, digest in profile["sourceSha256"].items():
        path = (ROOT / relative).resolve()
        assert path.is_relative_to(ROOT)
        assert sha256(path.read_bytes()).hexdigest() == digest, f"Review the protocol after changing {relative}"
    settings = Settings()
    limits = profile["referenceLimits"]
    storage = json.loads((ROOT / "ops/storage.example.json").read_text())
    assert limits["applicationMemoryMiB"] == settings.max_app_memory_mib
    assert limits["applicationStorageMiB"] == storage["app_budget_mib"]
    assert limits["storagePoolMiB"] == storage["global_budget_mib"]
    assert limits["buildNetwork"] == settings.build_network
    assert limits["healthWaitSeconds"] == settings.health_timeout


@pytest.mark.parametrize("example", ["simple-web", "frontend-api"])
def test_development_examples_are_complete_and_accepted_by_the_real_platform(example, monkeypatch, tmp_path):
    folder = SKILL / "assets/examples" / example
    raw = (folder / "awesome.yaml").read_text()
    assert yaml.safe_load(raw)["schemaVersion"] == 2
    manifest = load_manifest(raw, Settings())
    backend = DeploymentBackend(Settings(state_dir=tmp_path))
    # Only image acquisition is replaced: do not pull/build or start local Docker.
    monkeypatch.setattr(backend, "_pin", lambda reference, pins, checkpoint: reference)
    for service in manifest["services"].values():
        build = service["build"]
        context = folder / build["context"]
        dockerfile = (context / build["dockerfile"]).read_text()
        backend._dockerfile(dockerfile, {}, lambda update: None)
        for line in dockerfile.splitlines():
            if line.startswith("COPY "):
                for source in line.split()[1:-1]:
                    assert (context / source).is_file(), f"Example COPY input missing: {source}"
        assert sum(s["resources"]["memoryMiB"] for s in manifest["services"].values()) <= 500


def test_persistent_example_uses_real_quota_fields_without_requiring_secrets():
    raw = (SKILL / "assets/persistent-service.yaml").read_text()
    manifest = load_manifest(raw, Settings(storage_backend="xfs-project"))
    assert manifest["schemaVersion"] == 2
    storages = [s["storage"] for s in manifest["services"].values() if "storage" in s]
    assert sum(s["sizeMiB"] for s in storages) <= 50
    assert all(s["mountPath"] == "/data" and s["maxInodes"] > 0 for s in storages)
    assert not any(s["secretRefs"] for s in manifest["services"].values())
