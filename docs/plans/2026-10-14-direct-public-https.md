# Direct public HTTPS runtime access

User decision: business applications may connect directly to external model APIs. Do not change application source, behavior, authentication, SDKs or image contents. No HTTP proxy is required.

## Implementation

1. Freeze an operator-selected `public-https-v1` policy (DNS and host addresses) in new deployment snapshots. Missing policy means the historical internal-only behavior.
2. Keep service and dependency networks internal and add one dedicated external bridge per service. Give new bridges deterministic interface names. Verify bridge roles, IDs, ownership, DNS and frozen policy before starting, resuming, forwarding or recovering containers.
3. In the worker's own Rootless Docker user/network namespace, install an independent nftables table atomically before creating or attaching networks. Permit public IPv4 TCP 443 and approved public DNS only. Deny reserved/private/host addresses, unsolicited local access, cross-network traffic and IPv6. Preserve same dependency bridge communication and replies to platform ingress. Never modify Docker-owned firewall tables.
4. Ensure recovery verifies the guard before resuming public-mode applications. Fail closed if namespace, configuration, rules or resources differ. Never remove global protection during application cleanup.
5. Publish accurate capabilities, Chinese diagnostics and operator instructions. Preserve the existing protocol's historical guarantees; update its current capability section only after acceptance.

## Verification

- Write failing unit tests for policy validation, nft rules, atomic application and namespace identity first.
- Test legacy snapshots, public-mode creation/recovery, identity tampering, missing guard, failure/cleanup and forward compatibility.
- On the server, use isolated platform-owned fixtures to verify public TLS/DNS, private/host/metadata/non-443 rejection, dependency traffic and inbound health/HTTPS. Do not alter or redeploy customer applications for acceptance.
- Run the full Python suite, lint and relevant frontend checks. Deploy a versioned release only after these pass; verify installed readiness and retain rollback.

## Boundaries

TCP port 443 filtering is not TLS inspection or a domain allowlist. Runtime access does not enable Dockerfile build networking. Existing deployments keep their frozen network mode until redeployed. Containers still share the host kernel.
