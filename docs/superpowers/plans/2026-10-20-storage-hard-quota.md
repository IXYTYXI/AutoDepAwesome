# Storage hard quota implementation plan

> For agentic workers: use test-driven development and independent review. Work in the shared isolated checkout; edit only assigned files.

**Goal:** Implement the reviewed per-service XFS byte/inode hard-quota design without silently migrating existing named volumes.

**Architecture:** The root-owned `awesome-storage` helper owns one SQLite allocation/operation/writer ledger and delegates only fixed filesystem operations to an XFS adapter. The unprivileged deployment runtime uses a bounded JSON protocol; a helper guard session holds the same admission lock as fencing while Docker starts a registered writer. Existing v1/local volumes remain explicitly legacy, while v2 new quota storage requires an enabled, verified backend. Quota records contain stable identity, never an independently authoritative copy of effective limits.

**Tech stack:** Python standard library, SQLite, Linux XFS quota/ioctl APIs, rootless Docker, pytest.

**Baseline:** `e93af9d03a3ac2f789e24be132f352537e68de49` (0.7.0). Approved design: https://guanghe.feishu.cn/docx/Cy1Tdi4qwo4lHoxqXqic6Wndnrl.

## Shared contracts

- Helper package: `ops/storage_quota/`, installed root-owned beside `/usr/local/libexec/awesome-storage`; script uses Python isolated mode. Never sudo a checkout or import worker-writable Python.
- `QuotaError(code, message)` in `ops/storage_quota/errors.py`; errors carry fixed categories, not command output.
- `XFSBackend(config)` in `ops/storage_quota/xfs.py` supplies `check_pool()`, `create(record)`, `inspect(record)`, `set_limits(record, size_mib, max_inodes)`, `delete(record)`. The ledger record supplies `storage_id`, `generation`, `project_id`, `size_mib`, `max_inodes`, `directory` (after creation), `app_id`, `service`, `name`.
- `create(record)` is idempotent for the ledger's root-owned, unique generation; returns a directory identity dictionary. `inspect(record)` validates it and returns actual `allocated_bytes`, `used_inodes`, `size_mib`, `max_inodes`, `checked_at`. `delete(record)` returns true only once directory data and project quota usage are gone; it never follows links or releases a replaced directory.
- Stable runtime reference: `{"backend":"xfs-project","storage_id":<64 hex digest>,"generation":<32 hex>}`. Path and live limits come only from helper readback. Identity is `{app_id, service, name}` with `/data` implicit.
- JSON request operations: `check`, `ensure`, `inspect`, `expand`, `inventory`, `delete`, `unblock`, `guard`, `patrol`, `retire`, `retry`. Responses: `{"ok":true,"result":...}` or `{"ok":false,"error":{"code":...,"message":...}}`.
- `ensure`: identity, optional `expected` stable reference, optional `limits:{size_mib,max_inodes}`, required `operation_id`. Larger declarations require explicit expansion; lower/omitted declarations retain effective values. Existing reference missing from ledger must fail closed.
- `expand`: `expected`, `limits`, `operation_id`, `expected_revision`. Persist pending before kernel change, retain reservation until confirmed. No shrinking.
- `inspect`/`delete`/record `unblock`: `expected`; delete includes operation ID and requires no remaining writer registrations. Pool unblock is the separate explicit `{op: unblock, pool: true}` request. `inventory`: optional `app_id` or exact `expected`. `retry`: exact `expected` plus original `operation_id`; replays only the validated durable original request.
- `guard`: list `items`, each `{expected,writer:{container_id,container_name,app_id,release_id,service}}`; validates every ready record and registers writers before returning. It holds admission flock until a second newline-delimited `{"op":"release"}` or EOF. Worker passes stdin write FD to mutating Docker subprocesses so worker death cannot release the guard before the child exits. An uncertain start remains registered.
- `patrol`: same session handshake, checks all records, persists blocked on confirmed mismatch, returns `{blocked:[{reference,writers,reason}],unknown:[...]}` while holding admission lock. Worker stops/inspects exact registered containers before releasing; stops must not require quota readiness. Unknown query failures do not trigger stopping.
- `retire`: `container_id` after confirmed stopped/removal. Root helper treats worker/daemon as trusted control plane; repository/app code never gets the helper or Docker socket.
- Backend error categories distinguish confirmed unsafe identity/enforcement (`QUOTA_*_MISMATCH`, `QUOTA_NOT_ENFORCED`) from unavailable query (`QUOTA_UNAVAILABLE`). Never infer quota vs pool exhaustion from ENOSPC alone.

## Tasks

- [x] **1. XFS adapter** — `ops/storage_quota/xfs.py`, `tests/test_quota_xfs.py`. Tests first for mount/UUID/enforcement, actual UID map, safe directory creation/identity, dual limits, zero/unlimited rejection, symlink/replacement refusal, retained open-file usage. Implement fixed operations only; no shell interpolation, user paths or arbitrary project IDs. First release initializes empty `/data` explicitly; no privileged archive extraction.
- [x] **2. Helper and authoritative ledger** — `ops/awesome-storage`, `ops/storage_quota/{errors,service,protocol}.py`, helper tests. Test pending reservations, concurrent budget admission, partial expansion/crash retries, never shrink, monotonic project IDs, retained allocation, delete reconciliation, guard/fence ordering, unknown-vs-confirmed failures. Implement root-owned config/state checks, bounded request/response, fixed allowed caller, durable operations and writer registrations.
- [x] **3. Runtime integration** — `quota_client.py`, `storage.py`, `runtime.py`, `worker.py`, `offline_data.py`, targeted lifecycle tests. Preserve legacy semantics; new exact bind source is helper-derived. Prepare before stopping previous release; gate both start sites and recovery; separate stop identity from write permission. Independent patrol avoids worker build lock. Deletion retires exact writers before helper removal.
- [x] **4. Protocol/operator integration** — `manifest.py`, new `schema/awesome-v2.json`, `config.py`, `cli.py`, `api.py`, `store.py`, corresponding tests. Separate immutable storage identity from limits. Version schema/capability, validate config, expose status/expand/unblock/patrol commands, normalize deletion history by stable identity. Do not add quota UI or approval flows.
- [x] **5. Operational delivery** — root-owned install/config example, patrol systemd unit/timer, operator/compatibility/migration instructions, isolated Linux acceptance runbook (no automatic disk formatting or fault injection). Preserve boot recovery. Document defaults disabled, no production formatting/migration, no platform downgrade after quota activation.
- [x] **6. Verification and review** — run `python -m pytest`, `ruff check`, compile checks; independent spec/security review and fixes. No local Docker or server mutation. Actual XFS/kernel/rootless integration remains explicitly unverified until an authorized isolated Linux test target exists.

## Test execution

Use `.venv/bin/python -m pytest -q <target tests>` first to demonstrate missing behavior, then implement and rerun. Full suite command: `.venv/bin/python -m pytest -q`. Lint: `.venv/bin/ruff check src tests ops/storage_quota ops/awesome-storage`.

The current interpreter lacks FastAPI/jsonschema; install the repository's declared development dependencies in this worktree's `.venv` before judging baseline failures. Do not start Docker Desktop.

## Review corrections and delivery limits

Independent reviews covered the helper trust boundary/ledger, XFS ABI/directory handling, runtime admission/recovery/deletion and protocol/capability presentation. Corrections include established ledger corruption refusal, durable whole-pool fencing, source-only privileged imports, separate control filesystem, exact generation tombstones, nonblocking large deletions, bounded-FD deep-tree cleanup, explicit original-operation recovery, legacy explicit deletion, strict integer preflight, v2-aware diagnostics and ordered capability observations.

The implementation starts new quota directories empty. Existing named volumes have no in-place migration command or source/target association record. The operational manual explicitly forbids editing only the current storage reference; mixed historical backend references remain a conflict. Manual migration planning and full deployment/data-transfer validation are still required. No arbitrary host copy, archive restore, automatic shrink, quota UI or approval product was added.

Default backend remains local. Production XFS setup, live-rootless access, kernel boundary/pressure/failure tests, reboot acceptance and actual migration have not run. No Docker Desktop was started and no server was modified. DEC-QUOTA-001 remains pending. The code review and local tests are not a substitute for the Linux acceptance matrix in `ops/STORAGE-QUOTA.md`.

## Verification result — 2026-10-20

- Final complete suite: `python -m pytest -q --tb=short` — **1529 passed, 1 skipped**, 69.27 s. The skipped existing cache test requires user xattrs unavailable on this local filesystem. One upstream Starlette/httpx deprecation warning remains.
- `ruff check src tests ops/storage_quota ops/awesome-storage`, `compileall`, and `git diff --check` passed.
- Built `awesome_deploy-0.8.0-py3-none-any.whl`; confirmed v2 schema and quota client/status modules are packaged. Privileged helper and systemd files are installed separately from reviewed `ops/` sources.
- The full suite required local loopback/Unix socket permission for existing simulated tests. No real Docker or XFS was used. Initial full-suite CLI umask leakage was isolated in the test fixture and the complete suite rerun successfully.
- Runtime and helper reviewers rechecked fixes. Large deletion/patrol concurrency, old-generation retry protection, deep directories with bounded file descriptors, and cross-process capability cache ordering were exercised with temporary local fixtures.
