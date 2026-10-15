# Source export permissions and health failure diagnosis

The worker runs with `UMask=0077`. Source export normalized file modes, but
directory creation still inherited that umask. Nested directories copied into a
root-owned image therefore became inaccessible to the nonroot application UID.

Production failure `5c78ad942d754d2a8f8eddfedce950a5` used model-studio commit
`caab2378c543b29b6a8df60c4ba53b0798d04713`. Its candidate was cleaned before any
container error details were recorded. An isolated reproduction using the same
commit, current exporter and private umask exited with `ERR_MODULE_NOT_FOUND`
for the existing `vendor/jose/dist/webapi/index.js` dependency.

Implementation:

1. Reproduce private-umask export in regression tests, including implicit parent
   directories. Normalize only directories inside the export to 0755. Preserve
   private export/state roots, regular file bytes and Git executable bits.
2. Repeat the actual Docker build with the unchanged customer commit, network
   disabled and disposable configuration/data. Verify the failed baseline and
   successful health check after the exporter fix.
3. Retain only allowlisted health failure reasons through the existing durable
   failure record, and present specific Chinese guidance. Never store raw
   container output, environment values or HTTP response bodies.
4. Test the platform package, build an immutable offline release, validate the
   installed Linux wheel, activate while idle, and verify existing applications.
5. Retry the failed application using its existing identity, configuration and
   data with the original pinned source commit, then verify public HTTPS.

Customer business code, configured secrets and existing data are not changed by
this platform repair. Temporary diagnostic containers, images and volumes are
removed after verification. Git does not track directory permission modes; this
normalization supplies traversable source directories inside the image while
keeping host-side deployment state private.

## Follow-up from the actual platform build

The first live retry after 0.6.2 still exited during import. Capturing its startup
before cleanup and inspecting its image showed `vendor/jose` at 0700 even though
both exported source and frozen build context were 0755. An isolated run of the
actual `DeploymentBackend.freeze` and `build` methods reproduced that stale
output. Building the very same context and arguments with `--no-cache` produced
0755 directories and the readable 0644 dependency file.

Version 0.6.3 therefore explicitly disables build-layer reuse for source builds.
Base images remain pinned and may be available locally; no `--pull`, network
access, source mutation or cache deletion is added. Dockerfile steps run again,
so builds can take longer. Existing deployed images and image-only services are
unaffected. Both platform issues must be validated through the normal worker
queue before declaring this application successfully deployed.
