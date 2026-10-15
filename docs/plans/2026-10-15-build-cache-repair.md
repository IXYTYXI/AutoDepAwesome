# Restore correct incremental builds

The temporary 0.6.3 `--no-cache` workaround prevented stale directory permissions
from reaching application images, but re-executed every Dockerfile step.

The root cause matches upstream [BuildKit #7176](https://github.com/moby/buildkit/issues/7176):
fsutil updates an existing directory's metadata without notifying BuildKit's
content checksum cache. The fix in [fsutil #283](https://github.com/tonistiigi/fsutil/pull/283)
is still open and is absent from the installed BuildKit v0.34.0. Buildx v0.38.0
uses the local context basename in its shared source key; all previous
`{job_id}/web` contexts therefore reused the same mutable source state.

## Implementation and migration

- Freeze the pinned build inputs under a basename derived from a versioned,
  deterministic fingerprint of relative paths, types, permissions, ownership,
  extended attributes and file contents. Exclude timestamps and job IDs.
- Preserve customer file bytes and Dockerfile semantics. Base-image pinning
  remains the existing, permitted transformation. `.dockerignore`, target and
  build arguments remain BuildKit's responsibility.
- Reuse identical inputs and BuildKit's normal content-addressed COPY/RUN cache.
  Different inputs cannot inherit stale mutable source checksums. Stream file
  hashing; source size remains bounded by the platform's repository limit.
- Handle legacy frozen jobs in the build path without repinning inputs, reading
  current secret versions, or modifying their frozen original directory. Resume
  completed immutable image IDs without rebuilding.
- Before enabling cached builds on this existing installation, switch once to
  the independent `awesome-builder-cache-v2` builder with an empty cache. Keep
  the same pinned rootless BuildKit image, sandbox, networking and resource
  limits. Do not import historical cache records: their keys may disagree with
  their outputs. Preserve the old builder for rollback.
- Remove `--no-cache`. Normal builds do not prune cache or restart Docker.
  Existing builder garbage collection and platform disk limits still apply.

## Acceptance

Use the real `DeploymentBackend.freeze/build` path on the production rootless
engine with isolated jobs and diagnostic containers. Assert cold then cached
builds, directory-only permission changes, file executable-bit changes, changed
bytes with unchanged size/mtime, ignored-file changes, multi-stage target
interleaving, legacy frozen context recovery and immutable image recovery.
Capture actual `CACHED` output, not timing alone. Build the unchanged 90-file
model-studio commit twice and check its HTTP health using disposable data and
generated diagnostic credentials. Compare all source hashes.

Test the installed Linux release before activation. Switch code and builder
configuration while the queue is idle. Verify existing application IDs,
containers, data volumes, secret metadata, routes, reservations and public HTTPS
remain unchanged. This migration does not redeploy active customer applications.

## Real-builder evidence (2026-10-15)

The 13-case matrix passed on rootless BuildKit v0.34.0 / buildx v0.38.0.
The first multi-stage fixture build took 12.040 seconds including base-image
preparation; its identical repeat took 0.635 seconds, with every COPY/RUN step
reported as CACHED. Directory 0700→0755, executable bits and same-size/same-mtime
content changes produced correct new outputs. Unchanged dependency steps,
ignored-file changes, target interleaving and legacy frozen-input recovery
retained their expected cache hits.

The exact model-studio commit `caab2378c543b29b6a8df60c4ba53b0798d04713`
also passed two isolated builds and HTTP health checks as UID 65532, using
disposable tmpfs data and diagnostic credentials. Its 90 source file hashes and
the frozen file modes matched the input. The repeat build reported all COPY/RUN
steps cached (0.657 seconds). These are measured fixture build times, not a
general promise about application deployment duration.
