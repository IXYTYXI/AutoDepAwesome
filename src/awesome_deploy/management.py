"""Public projections for the management page; never expose execution snapshots."""

from .diagnostics import localize_error


def public_operation(operation):
    if operation is None:
        return None
    retryable = operation["status"] == "recovery_required"
    return {
        "id": operation["id"],
        "appId": operation["app_id"],
        "releaseId": operation["target_release_id"],
        "status": operation["status"],
        "phase": operation["phase"],
        "error": localize_error(operation.get("error"), operation["phase"]),
        "createdAt": operation["created_at"],
        "updatedAt": operation["updated_at"],
        "retryable": retryable,
        # Authenticated request identity, not a credential. A retry after a page
        # reload must continue the same durable operation, not create another.
        "retryKey": operation["idempotency_key"] if retryable else None,
    }


def public_application(app, base_domain):
    active = app.get("active_job")
    retained = app.get("retained_job")
    operation = app.get("operation")
    pending = operation and operation["status"] in {"queued", "running", "recovery_required"}
    status = (
        "attention" if pending and operation["status"] == "recovery_required"
        else "offlining" if pending
        else "online" if active
        else "offline" if retained
        else "not_deployed"
    )
    release = active or retained
    reason = app.get("offline_block_reason")
    return {
        "id": app["id"],
        "repository": app["repository"],
        "subdomain": app["subdomain"],
        "lastSubdomain": app.get("lastSubdomain") or (release["input"]["subdomain"] if release else None),
        "domainBound": app["subdomain"] is not None,
        "status": status,
        "activeReleaseId": app["active_release_id"],
        "retainedReleaseId": retained["id"] if retained else None,
        "commit": (release.get("snapshot") or {}).get("commit") if release else None,
        "ref": release["input"]["ref"] if release else None,
        "url": f"https://{app['subdomain']}.{base_domain}" if status == "online" and app["subdomain"] else None,
        "updatedAt": app["updated_at"],
        "canOffline": bool(app.get("can_offline")),
        "offlineBlockReason": localize_error({"code": reason})["message"] if reason else None,
        "operation": public_operation(operation),
    }
