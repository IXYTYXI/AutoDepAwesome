# Platform secret vault implementation plan

> For agentic workers: use superpowers:subagent-driven-development. User-approved design: Feishu document P1iZd1LoNoLYqnxOK0icr01Gndd revision 76.

**Goal:** Implement stage B and stage A's last-administrator safeguard without changing customer application code or migrating production secrets implicitly.

**Architecture:** Keep the existing platform identity, application ACL and worker. Add an explicitly enabled SQLite encrypted vault using per-application DEKs wrapped by an independently provisioned KEK. Every write, current pointer and audit record shares the caller's SQLite transaction. Existing deployments keep their frozen logical secret versions. Revocations are durable jobs with independently verified cleanup. Legacy mode stays explicitly separate until an operator-reviewed migration succeeds.

**Stack:** Python/FastAPI/SQLite, cryptography AESGCM, existing HTML/CSS/JavaScript control panel.

## Boundaries and decisions

- Work from main 15d0f8c in `AutoDepAwesoem-vault`, branch `codex/platform-secret-vault`.
- `secret_backend` is `legacy` or `encrypted`; encrypted mode requires Feishu mode, stable app IDs, a completed format initialization/migration and an external private KEK keyring file. No automatic KEK generation, plaintext fallback or format downgrade.
- `secret_kek_file` points outside the state directory; file mode 0600. A versioned keyring contains an active key ID and 32-byte keys. Ordinary config stores only the path.
- Vault identity is tenant + app ID; app data generation is durable. Secret `kind` and generation are authenticated metadata; ordinary rotate/set cannot replace a managed master key.
- `SecretVault` methods take an existing SQLite connection; never commit or open a second write connection. Schema setup happens before transactions. Safe metadata never contains ciphertext, secret hashes or plaintext.
- Explicit migration preserves UUID version IDs and all historical references. A maintenance marker blocks new work/writes. A reviewed manifest maps every legacy scope/name to tenant/app/kind/generation; ambiguity, orphans, corruption or missing history fail closed. Switch only after in-memory verification. Quarantine old plaintext and retain an inventory; do not silently delete or claim secure erasure.
- UI extends the existing green control-panel design. Metadata, rotation conflict, recent-auth requirement, master protection and revocation cleanup state have explicit text. No generic plaintext-read endpoint.
- Production OAuth, KEK responsibility/backup/retention registration and migration are separately gated. Code tests do not count as production acceptance.

## Tasks and verification

- [x] Vault core (`secret_vault.py`, `vault_keys.py`, `config.py`, `pyproject.toml`): schema, keyring loading, AES-256-GCM envelopes/AAD, nonce uniqueness, immutable versions/kind/generation, CAS rotation, revoked status, same-transaction audit, KEK rewrap and metadata-only destruction. Test cross-app/version/ciphertext/AAD tampering, missing KEK, byte limits, transaction rollback, frozen versions and protected masters.
- [x] A safeguard (`identity.py`): transactionally reject a web actor disabling the last eligible enabled administrator of the configured tenant/app; keep explicit trusted CLI recovery. Test concurrent disable, inactive/foreign alternatives and no failed-operation side effects.
- [x] Service/API (`vault_service.py`, `deployment_secrets.py`, `access.py`, `api.py`): identity-checked metadata/create/rotate/revoke; require can_deploy plus recent auth and role; mutation requests use `expected_current_version`. Existing missing-value completion remains create-only. Test every role, cross-app requests, stale auth/versions and no value leakage.
- [x] Execution (`runtime.py`, `worker.py`, `store.py`): freeze/resolve by stable application; check revoked versions before startup including reused containers, storage handoff, publish, successful commit, rollback and SYSTEM recovery. Persist affected jobs/instances before cleanup; replay revocation after crash, quarantine routing and verify stopped instances; expose incomplete cleanup honestly. Test build/start/publish revocation races and recovery failures.
- [x] Runtime env and destruction (`runtime.py`, `offline_data.py`): private bounded runtime env staging, require Linux `/dev/shm` tmpfs for encrypted mode and safely sweep task remnants; vault deletion plans preserve exact generation/version scope, tombstones and replay safety. Existing byte/inode/volume identity gates remain required.
- [x] CLI and migration (`vault_migration.py`, `vault_cli.py`, `cli.py`): explicit initialization, maintenance migration/check/resume, frozen-version verification, recoverability manifest and KEK rewrap; all set/rotate operations use app identity and audited paths. Legacy `set` also shares UTF-8 single-line validation. Test interrupted migration, unknown ownership/kind, all-version import, post-switch downgrade rejection and unchanged business bytes.
- [x] UI (`static/manage.*`, dedicated vault assets if needed): existing visual tokens, accessible forms/dialogs, bounded lists, safe metadata, CAS reload and retry, lifecycle progress. Add meaningful DOM tests and real browser checks across populated/empty/error/loading/conflict/master-protected states, keyboard/focus, long text and scroll limits.
- [x] Review and delivery: full Python/Node/Ruff/diff checks; independent security and requirement review; remote isolated container verification only if needed and clean all resources created by this task. Record code completion separately from actual server migration and real OAuth acceptance.

## API contract

`GET /api/v1/apps/{app_id}/secrets` returns metadata and lifecycle operations.

`POST /api/v1/apps/{app_id}/secrets/{name}/rotate` accepts `{value, expected_current_version}` and returns only the new version metadata.

`POST /api/v1/apps/{app_id}/secrets/{name}/revoke` accepts `{version, expected_current_version, reason}` and returns a durable cleanup operation. The revocation transaction makes subsequent version checks reject the version; external Docker/Nginx commands already dispatched can overlap and are then isolated by durable cleanup. Cleanup completion is reported separately.

No endpoint returns a secret value, DEK, KEK, envelope or reversible fragment. Existing deployment missing-secret endpoints preserve their semantics.


## Initial code verification record (2026-10-21, before target acceptance)

- Full Python suite: **1,873 passed, 1 skipped**. The skipped cache test requires filesystem user xattrs; this host does not support them. One existing Starlette/httpx deprecation warning remains.
- Node UI suite: **121 passed**. Ruff for `src`, `tests` and the gateway helper, plus `git diff --check`, passed.
- A fresh Python virtual environment installed `requirements-dev.lock` and the editable project with `--no-deps`; `pip check`, CLI help, legacy Store/API construction and encrypted module imports passed. The temporary environment was removed.
- Independent review corrected rollback/verify restoration of revoked previous versions, authenticated completed-migration replay, and installation dependency lock omissions. Follow-up reproductions passed.
- UI/UX Pro Max and the current Web Interface Guidelines were applied to the existing green design. Real browser checks against synthetic API fixtures covered role restrictions, master protection, version lists, rotation and CAS conflicts, recent authentication, revocation/retry progress, empty/error/loading states, hover/focus, keyboard, long content and scroll boundaries at desktop and narrow widths. The temporary preview and browser tabs were removed.
- The tests use synthetic secrets. Customer repositories and business code were not modified. No production configuration, SSH session, local Docker process or deployment was started for this work.

## Target acceptance follow-up (2026-10-21)

- Installed candidate `8bda26b` (package 0.9.0) into a separate root-owned directory on the target Linux host, using 33 verified offline wheels. `pip check` passed. Production remains release `e93af9d`, package 0.7.0, Basic/legacy; the production release link was not changed.
- Target Linux full suite: **1,874 passed**, including the xattr test skipped on macOS. One existing Starlette/httpx warning remains.
- Real Linux tmpfs permissions, normal/error cleanup and worker-lock startup cleanup after a child process crash passed. Synthetic migration resumed at six interruption points; independent fixture KEK and state restore preserved the original synthetic business bytes. Temporary state and env files were removed.
- Real rootless Docker start/recovery preserved the original test container, image, volume instance and random data ID. Forged volume identity was rejected before startup; injected gateway failure stopped the test container, and a revoked secret prevented recovery. Test container/network/volume/forwarder were removed. No test image was created; the existing base image was retained. Five pre-existing running containers had unchanged IDs and start times.
- Real root-owned Nginx in isolated mount/PID/network namespaces verified TLS tombstones, probe logs, quarantine replay and worker drain. A held old-worker connection caused a 30-second deadline failure rather than false completion; after release the same intent completed. Namespace resources were removed. This did **not** exercise the installed privileged helper entrypoint or host Docker-daemon restart.
- Production callback logging was hardened with the documented exact Nginx location; configuration test/reload passed and a synthetic callback marker was absent from three local Nginx logs. Real OAuth and external log collectors remain unverified.
- A brief API/worker maintenance window produced a protected platform-state backup. A separate restored copy initialized with the candidate Store/API; all 43 jobs and active mappings for four applications were preserved. Temporary restore state was removed and API/worker resumed. Business containers were not stopped. This was **not** a business-volume backup or production KEK restore.
- A follow-up found inconsistent treatment of failed history with an empty `secret_versions` map. Migration and `vault-check` now share the conservative field-presence predicate. The new cases failed before the fix; after the fix the focused local suite passed **166 tests**, plus Ruff and diff checks. Independent review found no blocking issue.

## Remaining production gates

- Real Feishu OAuth and tenant/member eligibility, administrator recovery and cross-application permission verification.
- The user will provide a company-wide platform Feishu app later. Do not enable Feishu/encrypted mode using placeholder identities or substitute CLI credentials.
- One offline legacy application has retained successful history and its own data volume but lacks its application row. Its legacy secret scope is also referenced by the current application. Existing ownership/migration guards correctly reject this ambiguity; an explicit historical disposition and reviewed repair are required. No history, secret or business data was deleted or reassigned.
- Root-owned gateway installation/entrypoint verification, full privileged production integration and actual byte/inode hard-quota acceptance remain pending. The production storage backend is still local, without a verified quota helper.
- Responsible-person and retention registration, real independent KEK backup, full production historical migration, and original business-volume backup/restore remain pending.
- Screen-reader speech and operating-system reduced-motion integration were not exercised.
- C device pairing and D approvals/service audit gates are separate future implementation stages. This branch does not enable or claim those features.
