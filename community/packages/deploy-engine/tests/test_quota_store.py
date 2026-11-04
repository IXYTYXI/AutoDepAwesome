import copy
import hashlib
import json

import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.store import Store
from test_offline_lifecycle import active, payload


def history(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "_available", staticmethod(lambda port: True))
    store = Store(Settings(state_dir=tmp_path))
    first = active(store)
    spec = first["snapshot"]["manifest"]["services"]["web"]["storage"]
    identity = {"app_id": first["app_id"], "service": "web", "name": spec["name"]}
    storage_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    reference = {"backend": "xfs-project", "storage_id": storage_id,
                 "generation": "a" * 32}
    store.checkpoint(first["id"], execution={"storage_volumes": {"web": reference}})
    second = store.enqueue(payload(first["app_id"]), "second")
    snapshot = copy.deepcopy(first["snapshot"])
    snapshot["manifest"]["services"]["web"]["storage"].update(sizeMiB=4096, maxInodes=100000)
    store.claim(second["id"])
    store.reserve(second["id"], snapshot)
    store.checkpoint(second["id"], execution={"storage_volumes": {"web": reference}})
    second = store.finish(second["id"])
    return store, second, reference


def metadata(store, job):
    with store._connection() as db:
        app = dict(db.execute("SELECT * FROM apps WHERE id=?", (job["app_id"],)).fetchone())
        return store._deletion_metadata(db, app)


def test_deletion_history_deduplicates_quota_by_identity_not_past_requested_limits(tmp_path, monkeypatch):
    store, job, reference = history(tmp_path, monkeypatch)
    volumes = metadata(store, job)["volumes"]
    assert len(volumes) == 1
    assert volumes[0]["expected"] == reference
    assert volumes[0]["spec"] == {"name": "images", "mountPath": "/data"}
    assert "sizeMiB" not in volumes[0]["spec"]


@pytest.mark.parametrize("change", [{"generation": "b" * 32}, {"storage_id": "f" * 64},
                                   {"size_mib": 1024}, {"backend": "local"}])
def test_changed_or_augmented_quota_identity_blocks_deletion(tmp_path, monkeypatch, change):
    store, job, reference = history(tmp_path, monkeypatch)
    store.checkpoint(job["id"], execution={"storage_volumes": {"web": {**reference, **change}}})
    with pytest.raises(DeployError) as caught:
        metadata(store, job)
    assert caught.value.code == "OFFLINE_DATA_CONFLICT"
