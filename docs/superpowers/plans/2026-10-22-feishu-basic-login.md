# Feishu basic login implementation plan

> Scope approved by the user on 2026-10-22: postpone employee-disable handling and continuous remote eligibility checks. Continue basic Feishu login without new directory permissions.

**Goal:** Enable real OAuth login for the explicitly selected enterprise App, preserving tenant binding, platform sessions and local application authorization.

**Architecture:** Add explicit `feishu_eligibility_mode=login|continuous`, retaining continuous as the backward-compatible default. Production explicitly chooses login. OAuth code exchange and user_info remain mandatory; only the additional employee/visibility API checks are skipped in login mode. Local revisions, member roles and session protections remain enforced.

**Tech stack:** Python, FastAPI, HTTPX, SQLite, Feishu OAuth v2 user exchange / v3 application tokens and authen v1, Nginx.

- [x] Add failing config/provider/identity tests: invalid modes rejected; login mode calls only token+user_info; tenant mismatch rejected; five-minute and force paths make no extra provider request; local permission revocation still applies; continuous mode unchanged.
- [x] Implement the mode in config.py, feishu.py and identity.py, and document its exact scope in ops/FEISHU-LOGIN.md. No UI or business repository change.
- [x] Register the exact HTTPS callback. Obtain tenant identity through this App's real OAuth flow without additional tenant/directory permissions; do not reuse CLI tokens or trust an arbitrary unauthenticated user.
- [ ] Run focused tests and prepare an isolated target-server release preserving production resource limits and RootlessKit compatibility. Keep legacy secret and storage backends for the identity cutover.
- [ ] Back up platform configuration and database, drain the management queue, activate the explicit login mode, and verify actual HTTPS callback, session, protected API and logout. Initialize the operator's verified identity through the existing privileged CLI, never automatic first-user admin logic.
- [ ] Push the reviewed changes and report real verification separately from outstanding B key/volume migration. Do not claim the deferred continuous eligibility feature was tested or enabled.

Real target-server probe: v3 rejected a valid S256 flow with 20049; v2 JSON succeeded with the same App and verified user_info. A separate new authorization code with an incorrect verifier was rejected by v2 with 20049. User exchange therefore explicitly uses v2; no PKCE bypass or automatic endpoint fallback.
