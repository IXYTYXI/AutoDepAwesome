"""Unprivileged client for the installed, root-owned, narrow gateway helper."""

import json

from .errors import DeployError
from .processes import run_command


class Gateway:
    def __init__(self, settings):
        self.settings = settings
        self.lock_fd = None

    def check(self):
        return self._request({"op": "check"})

    def call(self, operation, job):
        manifest = job["snapshot"]["manifest"]
        request = {
            "op": operation,
            "base_domain": self.settings.base_domain,
            "subdomain": manifest["app"]["subdomain"],
            "release": job["id"],
            "ports": job["execution"]["ports"],
            "routes": manifest["routes"],
            "previous_release": job["execution"].get("previous_release"),
        }
        return self._request(request)

    def _request(self, request):
        result = run_command(
            list(self.settings.gateway_command),
            timeout=360,
            input_text=json.dumps(request),
            pass_fds=(self.lock_fd,) if self.lock_fd is not None else (),
        )
        try:
            response = json.loads(result.stdout)
        except ValueError as exc:
            raise DeployError("GATEWAY_PROTOCOL", "Gateway returned invalid JSON") from exc
        if response.get("ok") is not True:
            raise DeployError("GATEWAY_FAILED", response.get("error", "Gateway operation failed"))
        return response
