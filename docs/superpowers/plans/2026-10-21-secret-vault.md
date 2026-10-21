# Platform secret vault implementation plan

> For agentic workers: use superpowers:subagent-driven-development. The approved implementation scope is summarized below.

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
- The initial implementation and UI checks used synthetic secrets. Customer repositories and business code were not modified. Local Docker was not started.

## Additional verification summary (2026-10-21)

- Candidate `de43f6805a95259bb1bec9f89175cf78afe48d1f` passed **1,895 tests** on Linux, with one existing Starlette/httpx deprecation warning. Artifact hash verification, offline installation and `pip check` passed. These results establish tested code behavior; they do not establish production rollout acceptance.
- Real Linux tmpfs permission, cleanup and crash-recovery checks passed in isolation. Synthetic migration resumed across interruption points, and an independent fixture KEK/state restore preserved the synthetic business bytes.
- Isolated rootless Docker checks preserved container, image, volume and data identity across recovery. Forged volume identity was rejected, gateway failure stopped the fixture, and revoked secrets prevented restart. Temporary fixture resources were removed.
- Real root-owned Nginx in isolated namespaces verified TLS tombstones, probe logs, quarantine replay and worker draining. A held old-worker connection correctly prevented completion until it was released.
- A real `sudo` entrypoint was exercised in an isolated installation layout with Nginx. Checks covered argument restrictions, UID/permissions, hostile environments, malformed input, publishing, quarantine/replay and renewal restrictions. This did not validate a production-installed helper.
- A restored platform-state copy initialized with the candidate while preserving application and active-deployment mappings. An isolated business-database restore verified original database bytes, encrypted-record readability and application health. This evidence does not establish complete backup/restore coverage for every application or an independent production vault KEK restore.
- Callback log suppression was checked using a synthetic marker. Network connectivity checks did not exercise OAuth authorization or API authentication. Real authorization callbacks and external log collectors require separate acceptance.
- A migration edge case involving an explicitly empty `secret_versions` map was corrected so migration and `vault-check` use the same conservative field-presence predicate. Focused regression tests, Ruff and diff checks passed.
- The current login implementation requires an OAuth redirect URL. It has no Feishu event-subscription, card-callback or long-connection consumer. See `ops/FEISHU-LOGIN.md` for the generic developer-console checklist.

## Remaining deployment acceptance gates

- Complete real Feishu OAuth, tenant/member eligibility, authorization denial and revocation checks, administrator recovery and cross-application permission verification with the deployment's own app and designated test users. CLI credentials and placeholder identities are not substitutes.
- Verify existing application owners explicitly against identities from the configured Feishu app. Review legacy-secret ownership, kinds and generations before migration; never infer ownership from a matching repository.
- Install and validate the root-owned gateway helper through the deployment's formal entrypoint, then complete privileged integration checks. Isolated entrypoint tests do not close this gate.
- Validate actual byte and inode hard quotas on a supported filesystem before claiming storage enforcement. Filesystem migration and quota activation require a separate reviewed deployment procedure.
- Register KEK responsibility and retention, verify an independent KEK backup, complete the full platform/business backup-and-restore plan, and accept the complete historical-secret migration before enabling encrypted mode. Synthetic recovery and a single business-database restore do not close these gates.
- Screen-reader speech and operating-system reduced-motion integration were not exercised.
- C device pairing and D approvals/service audit gates remain separate future implementation stages. This branch does not enable or claim those features.
