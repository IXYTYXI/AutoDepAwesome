# Deployment configuration completion

The user needs repository-to-deployment to work without guessing how to generate secrets or SSH into the server. Add a secure inline completion form to the existing console when a fixed source snapshot cannot freeze because declared secret references are absent. No customer source, authentication, encryption, manifest or runtime behavior is modified.

## Flow

The console recognizes SECRET_NOT_FOUND, fetches the fixed task's required reference names and states, and shows all missing fields. A user may enter an existing value. For a scope with no prior application allocation, the UI also offers an explicit Web Crypto 32-byte / 64-hex random-value generator for BOOTSTRAP_PASSWORD / MASTER_KEY references whose environment uses match these known names, clearly identified as suitable for new passwords/random secrets, not provider API keys. Generated values remain in the current DOM, can be revealed/copied before saving, and are never stored in browser storage. Existing values are never returned. The form says values are stored privately on the deployment server and the application receives its declared environment variables.

The user clicks Save and continue. Values are posted only to the authenticated same-origin configuration endpoint; success clears values, then the UI explicitly submits a new deployment with the original repository/ref/subdomain/app selection using the existing idempotent submission API. The failed task remains unchanged. If submission fails after saving, the user can retry without re-entering or rotating values. Switching records clears unsaved fields and late responses cannot mutate another task. No actual customer task is submitted during implementation acceptance.

## API contract

GET /api/v1/deployments/{job_id}/secrets
POST /api/v1/deployments/{job_id}/secrets, body {"values":{"REFERENCE":"value"}}

Both return {jobId, repository, subdomain, canGenerate, items:[{name, uses:[{service,environmentVariable}], state:"missing"|"configured"|"unavailable"}]} without values or raw file paths. Only a failed SECRET_NOT_FOUND task with a validated pinned snapshot is eligible. All identities and allowed names derive from stored task data, never client overrides. POST creates only declared missing references; already configured/unavailable values cannot be overwritten. Existing cross-application scope reuse rules remain authoritative; a new writer cannot rely on successful old-version reuse permission.

POST acquires worker.lock then a SQLite immediate transaction, rechecks scope ownership and active work, validates the complete request before writing, and publishes each new current pointer atomically without overwrite. Files use 0600, directories 0700 and avoid symlink traversal. Read-only status checks may inspect non-secret pointers/stat metadata but never value contents. Partial filesystem failure leaves any successfully created values intact; reload permits completing only remaining missing fields.

## Verification and release

Write failing backend tests for authentication/origin, malformed requests with no echo, scope ownership, active work, create-only races, unsafe paths and no values in jobs/events/API. Write frontend tests for rendering, generation, input preservation, clearing, late responses, saving before submission and retry after submit failure. Run Python and frontend regression suites and lint; review independently. Release 0.6.0 from a fixed clean pushed commit, test installed wheel on Linux, activate only while idle with backups and invariant checks. Verify inline form with a local fixture using dummy secrets, and read-only real requirements online. Do not generate/store real customer secrets or submit their deployment as part of acceptance.
