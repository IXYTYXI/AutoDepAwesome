"""Controlled Docker backend with application-owned persistent storage. Requires a dedicated rootless Docker daemon.

Internal bridge networks restrict external routing and isolate dependency groups, but
are not a directional firewall or a hostile-code VM boundary. Never share this daemon
with privileged workloads. No host networking, host mounts, build secrets or SSH agent.
"""

import copy
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time

from .errors import DeployError
from .gateway import Gateway
from .port_forward import RootlessPortForwarder
from .processes import run_command, http_status
from .secrets_store import SecretStore
from .storage import ManagedStorage
from .manifest import validate_storage_transition
from .egress import EgressGuard, bridge_name, policy, validate_policy


IMMUTABLE = re.compile(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,64}")
IMAGE_REF = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,511}")


class DeploymentBackend:
    def __init__(self, settings):
        self.settings = settings
        self.gateway = Gateway(settings)
        self.port_forwarder = RootlessPortForwarder(settings)
        self.egress_guard = EgressGuard(settings)
        self.lock_fd = None
        self.storage_root = None

    def _docker(self, args, *, timeout=120, secrets=(), check=True):
        config = self.settings.state_dir / "docker-config"
        config.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = run_command(
            [
                self.settings.docker_binary,
                "--config",
                str(config.resolve()),
                "--host",
                self.settings.docker_host,
                *args,
            ],
            timeout=timeout,
            secrets=secrets,
            check=check,
            pass_fds=(self.lock_fd,) if self.lock_fd is not None else (),
        )
        return result.stdout

    def _check_socket(self):
        host = self.settings.docker_host
        if sys.platform != "linux":
            raise DeployError("HOST_UNSUPPORTED", "Deployment requires a Linux host with rootless Docker")
        if not host.startswith("unix:///"):
            raise DeployError("HOST_UNSAFE", "A dedicated rootless Docker Unix socket is required")
        try:
            info = Path(host[7:]).lstat()
        except OSError as exc:
            raise DeployError("HOST_UNAVAILABLE", "Configured rootless Docker socket is unavailable") from exc
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid == 0
            or info.st_uid != os.getuid()
            or info.st_mode & 0o002
        ):
            raise DeployError(
                "HOST_UNSAFE", "Docker socket must be owned by the nonroot worker and not world-writable"
            )

    def _runtime_preflight(self, timeout=120, deadline=None):
        if not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", self.settings.runtime_user):
            raise DeployError("HOST_UNSAFE", "Runtime user must be a numeric nonroot UID and GID")
        self._check_socket()
        info = json.loads(self._docker(["info", "--format", "{{json .}}"], timeout=timeout))
        options = info.get("SecurityOptions", [])
        if not any("rootless" in str(option) for option in options):
            raise DeployError("HOST_UNSAFE", "Dedicated rootless Docker is required")
        if (
            str(info.get("CgroupVersion")) != "2"
            or info.get("CgroupDriver") not in {"systemd", "cgroupfs"}
            or any(
                info.get(flag) is not True
                for flag in ("MemoryLimit", "CpuCfsPeriod", "CpuCfsQuota", "PidsLimit")
            )
            or any("limit" in str(w).lower() or "quota" in str(w).lower() for w in info.get("Warnings") or [])
        ):
            raise DeployError(
                "HOST_UNSAFE", "Rootless Docker requires cgroup v2 CPU, memory and pids limit support"
            )
        if not any("seccomp" in str(option) for option in options):
            raise DeployError("HOST_UNSAFE", "Docker default seccomp enforcement is required")
        forward_timeout = min(5, timeout)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeployError("ACTIVE_RECOVERY_TIMEOUT", "Active recovery exceeded its time limit")
            forward_timeout = min(forward_timeout, remaining)
        self.port_forwarder.check(timeout=forward_timeout)
        return info

    def preflight(self):
        info = self._runtime_preflight()
        self.settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.settings.state_dir).free < self.settings.required_free_mib * 1024 * 1024:
            raise DeployError("DISK_CAPACITY", "Insufficient free disk space for deployment")
        self._check_storage(info)
        if self.settings.build_network != "none":
            raise DeployError("UNSUPPORTED_CAPABILITY", "First-phase builds require build_network=none")
        return info

    def _check_storage(self, info, require_free=True):
        root = Path(info.get("DockerRootDir") or "/nonexistent-docker-root").resolve()
        try:
            if not os.path.ismount(root) or root.stat().st_dev == self.settings.state_dir.stat().st_dev:
                raise DeployError(
                    "STORAGE_UNSAFE",
                    "DockerRootDir must be a dedicated mount on a different filesystem from platform state",
                )
            usage = os.statvfs(root)
        except OSError as exc:
            raise DeployError(
                "STORAGE_UNSAFE", "Cannot inspect the dedicated Docker storage filesystem"
            ) from exc
        if usage.f_blocks * usage.f_frsize > self.settings.docker_storage_limit_mib * 1024 * 1024:
            raise DeployError(
                "STORAGE_UNSAFE", "Docker storage mount exceeds configured hard capacity boundary"
            )
        self.storage_root = root
        if require_free and (
            usage.f_bavail * usage.f_frsize < self.settings.required_free_mib * 1024 * 1024
            or usage.f_files
            and usage.f_favail < 1024
        ):
            raise DeployError("DISK_CAPACITY", "Docker storage has insufficient free bytes or inodes")

    def check(self):
        info = self.preflight()
        network_policy = policy(self.settings)
        if network_policy is not None:
            self._check_live_restore(info)
            self.egress_guard.lock_fd = self.lock_fd
            self.egress_guard.check(network_policy)
        self._builder()
        self.gateway.lock_fd = self.lock_fd
        self.gateway.check()
        return {
            "ready": True,
            "runtime": "rootless-docker",
            "build_network": "none",
            "runtime_network": "internal" if network_policy is None else network_policy["mode"],
            "limitations": [
                "Persistent storage is a single owned local volume per service at /data; updates briefly stop the previous release.",
                "Internal dependency networks are bidirectional, not an application firewall.",
                "Containers share the host kernel; this is not a hostile-code VM sandbox.",
            ],
        }

    def _image(self, reference):
        if not isinstance(reference, str) or not IMAGE_REF.fullmatch(reference):
            raise DeployError("IMAGE_INVALID", "Invalid image reference")
        result = json.loads(self._docker(["image", "inspect", reference]))
        if not isinstance(result, list) or not result:
            raise DeployError("IMAGE_INVALID", "Image inspection failed")
        image = result[0]
        config = image.get("Config") or {}
        if config.get("Volumes"):
            raise DeployError(
                "UNSUPPORTED_CAPABILITY", "Images declaring volumes (VOLUME) are unsupported; declare controlled service.storage instead"
            )
        if config.get("OnBuild"):
            raise DeployError(
                "UNSUPPORTED_CAPABILITY", "Image ONBUILD triggers are unsupported; use an explicit Dockerfile"
            )
        return image

    def _pin(self, reference, pins, checkpoint):
        if reference == "scratch":
            return reference
        if reference in pins:
            return pins[reference]
        if not IMAGE_REF.fullmatch(reference):
            raise DeployError("IMAGE_INVALID", "Invalid literal Docker image reference")
        self._docker(["pull", reference], timeout=self.settings.build_timeout)
        image = self._image(reference)
        if "@sha256:" in reference:
            pinned = reference
        else:
            digests = image.get("RepoDigests") or []
            if not digests or not IMMUTABLE.fullmatch(digests[0]):
                raise DeployError("IMAGE_UNPINNED", "Registry did not provide an immutable image digest")
            pinned = digests[0]
        pins[reference] = pinned
        checkpoint({"frozen_images": dict(pins)})
        return pinned

    @staticmethod
    def _build_args(build):
        args = build.get("args") or {}
        for key, value in args.items():
            if key.upper().startswith(("BUILDKIT_", "BUILDX_")):
                raise DeployError(
                    "UNSUPPORTED_BUILD_ARGUMENT", "BuildKit/Buildx control arguments are reserved"
                )
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise DeployError(
                    "UNSUPPORTED_BUILD_ARGUMENT", "Build arguments require literal names and strings"
                )
        return args

    def _dockerfile(self, text, pins, checkpoint):
        aliases, output, from_count = set(), [], 0
        allowed = {
            "FROM",
            "RUN",
            "COPY",
            "ENV",
            "WORKDIR",
            "USER",
            "EXPOSE",
            "LABEL",
            "CMD",
            "ENTRYPOINT",
            "ARG",
            "STOPSIGNAL",
        }
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                if re.match(r"#\s*(syntax|escape|check)\s*=", stripped, re.I):
                    raise DeployError(
                        "UNSUPPORTED_DOCKERFILE",
                        "Custom Dockerfile frontend/parser directives are unsupported",
                    )
                output.append(line)
                continue
            if stripped.endswith("\\") or "<<" in stripped:
                raise DeployError(
                    "UNSUPPORTED_DOCKERFILE", "Dockerfile continuations/heredocs are unsupported in phase one"
                )
            parts = stripped.split(None, 1)
            instruction, rest = parts[0].upper(), parts[1] if len(parts) > 1 else ""
            if instruction not in allowed or not rest:
                raise DeployError(
                    "UNSUPPORTED_DOCKERFILE", f"Unsupported Dockerfile instruction: {instruction}"
                )
            if instruction == "FROM":
                match = re.fullmatch(r"([^\s$]+)(?:\s+[aA][sS]\s+([A-Za-z][A-Za-z0-9_-]*))?", rest)
                if not match or rest.startswith("--"):
                    raise DeployError(
                        "UNSUPPORTED_DOCKERFILE", "FROM requires a literal image or preceding named stage"
                    )
                reference, alias = match.groups()
                if reference.lower() not in aliases:
                    reference = self._pin(reference, pins, checkpoint)
                if alias:
                    if alias.lower() in aliases:
                        raise DeployError("UNSUPPORTED_DOCKERFILE", "Duplicate Dockerfile stage alias")
                    aliases.add(alias.lower())
                output.append(f"FROM {reference}" + (f" AS {alias}" if alias else ""))
                from_count += 1
                continue
            if instruction == "RUN" and rest.startswith("--"):
                raise DeployError(
                    "UNSUPPORTED_DOCKERFILE", "RUN flags/mounts/network overrides are unsupported"
                )
            if instruction == "COPY":
                for flag in re.findall(r"--[^\s]+", rest):
                    if not flag.startswith("--from=") or flag[7:].lower() not in aliases:
                        raise DeployError(
                            "UNSUPPORTED_DOCKERFILE", "COPY flags may reference only a preceding named stage"
                        )
            if from_count == 0:
                raise DeployError(
                    "UNSUPPORTED_DOCKERFILE",
                    "Instructions before FROM, including dynamic ARG bases, are unsupported",
                )
            output.append(line)
        if not from_count:
            raise DeployError("UNSUPPORTED_DOCKERFILE", "Dockerfile requires FROM")
        return "\n".join(output) + "\n"

    def freeze(self, snapshot, job, checkpoint):
        info = self.preflight()
        self._ids(job, require_app=False)
        checkpoint({"build_storage_root": str(self.storage_root)})
        validate_storage_transition(snapshot["manifest"], job.get("execution", {}).get("previous_manifest"))
        frozen = copy.deepcopy(snapshot)
        network_policy = validate_policy(job) if "runtime_network" in snapshot else policy(self.settings)
        if network_policy is not None:
            self._check_live_restore(info)
            frozen["runtime_network"] = copy.deepcopy(network_policy)
        pins = dict(job.get("execution", {}).get("frozen_images", {}))
        frozen["build_contexts"], frozen["secret_versions"] = {}, {}
        source = Path(snapshot["source_dir"]).resolve()
        for name, service in frozen["manifest"]["services"].items():
            refs = service.get("secretRefs") or {}
            if refs:
                frozen["secret_versions"][name] = SecretStore(self.settings).versions(
                    frozen["repository"], frozen["manifest"]["app"]["subdomain"], refs
                )
            if "image" in service:
                service["image"] = self._pin(service["image"], pins, checkpoint)
                continue
            build = service["build"]
            self._build_args(build)
            context = (source / build["context"]).resolve()
            dockerfile = (context / build["dockerfile"]).resolve()
            if not context.is_relative_to(source) or not dockerfile.is_relative_to(context):
                raise DeployError(
                    "UNSAFE_BUILD_PATH",
                    "Build context and Dockerfile must remain within the approved repository",
                )
            if not dockerfile.is_file() or dockerfile.stat().st_size > 262144:
                raise DeployError("DOCKERFILE_MISSING", "A bounded regular Dockerfile is required")
            pinned_text = self._dockerfile(dockerfile.read_text(), pins, checkpoint)
            staging = self.storage_root / "awesome-build-contexts" / job["id"] / name
            # SourceManager rejects escaping links. Recheck the copied context independently.
            for path in context.rglob("*"):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise DeployError(
                        "UNSAFE_BUILD_PATH", "Build contexts may contain only regular files and directories"
                    )
            if staging.exists():
                shutil.rmtree(staging)
            staging.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(context, staging, ignore=shutil.ignore_patterns(".git"))
            staged_file = staging / build["dockerfile"]
            staged_file.write_text(pinned_text)
            frozen["build_contexts"][name] = str(staging)
        checkpoint(
            {
                "frozen_images": pins,
                "build_contexts": frozen["build_contexts"],
                "secret_versions": frozen["secret_versions"],
            }
        )
        return frozen

    def _builder(self):
        # `buildx inspect` has no --format option. Parse its bounded C-locale output,
        # then independently inspect the actual Docker container for enforcement.
        result = self._docker(["buildx", "inspect", self.settings.builder_name])
        names = re.findall(r"^Name:\s*(\S+)\s*$", result, re.M)
        endpoints = re.findall(r"^Endpoint:\s*(\S+)\s*$", result, re.M)
        if (
            not re.search(r"^Driver:\s*docker-container\s*$", result, re.M)
            or len(names) != 2
            or names[0] != self.settings.builder_name
            or endpoints != [self.settings.docker_host]
            or not re.search(r"^Status:\s*running\s*$", result, re.M)
            or not re.search(r"worker\.oci\.process-mode:\s*sandbox\s*$", result, re.M)
        ):
            raise DeployError(
                "BUILDER_UNSAFE",
                "A running dedicated docker-container builder with process sandbox on the rootless socket is required",
            )
        name = names[1]
        if not IDENTIFIER.fullmatch(name):
            raise DeployError("BUILDER_UNSAFE", "Invalid dedicated builder node")
        container = json.loads(self._docker(["container", "inspect", f"buildx_buildkit_{name}"]))[0]
        config, limits = container.get("Config") or {}, container.get("HostConfig") or {}
        image = config.get("Image", "")
        cpu = limits.get("NanoCpus", 0) / 1_000_000_000
        if not cpu and limits.get("CpuPeriod", 0) > 0:
            cpu = limits.get("CpuQuota", 0) / limits["CpuPeriod"]
        if (
            not re.fullmatch(r"(?:docker.io/)?moby/buildkit:rootless@sha256:[0-9a-f]{64}", image)
            or config.get("User") not in {"1000", "1000:1000"}
            or not 0 < limits.get("Memory", 0) <= self.settings.max_memory_mib * 1024 * 1024
            or not 0 < cpu <= self.settings.max_cpu
            or limits.get("NetworkMode", "").startswith(("host", "container:"))
            or limits.get("Binds")
            or any(m.get("Type") == "bind" for m in container.get("Mounts", []))
            or "no-process-sandbox" in json.dumps(config)
        ):
            raise DeployError(
                "BUILDER_UNSAFE",
                "Builder must use pinned rootless BuildKit, nonroot UID, CPU/memory bounds and process sandbox",
            )

    def build(self, job, checkpoint):
        self.preflight()
        images = dict(job["execution"].get("images", {}))
        snapshot = job["snapshot"]
        checked_builder = False
        for name, service in snapshot["manifest"]["services"].items():
            if "build" in service:
                self._build_args(service["build"])
            if name in images:
                self._image(images[name])
                continue
            if "image" in service:
                image = self._image(service["image"])
                images[name] = image["Id"]
            else:
                if not checked_builder:
                    self._builder()
                    checked_builder = True
                context = Path(snapshot["build_contexts"][name])
                iid = context.parent / f"{name}.iid"
                iid.unlink(missing_ok=True)
                tag = f"awesome/{job['id'].lower()}-{name}:candidate"
                options = []
                if service["build"].get("target"):
                    options.extend(["--target", service["build"]["target"]])
                for key, value in sorted(self._build_args(service["build"]).items()):
                    options.extend(["--build-arg", f"{key}={value}"])
                self._docker(
                    [
                        "buildx",
                        "build",
                        "--builder",
                        self.settings.builder_name,
                        "--network=none",
                        *options,
                        "--load",
                        "--provenance=false",
                        "--iidfile",
                        str(iid),
                        "--tag",
                        tag,
                        *self._label_flags(job),
                        "--label",
                        f"awesome.service={name}",
                        "--file",
                        str(context / service["build"]["dockerfile"]),
                        str(context),
                    ],
                    timeout=self.settings.build_timeout,
                )
                image_id = iid.read_text().strip()
                if not IMMUTABLE.fullmatch(image_id):
                    raise DeployError("BUILD_UNPINNED", "Build did not return an immutable image ID")
                self._image(image_id)
                images[name] = image_id
            if not IMMUTABLE.fullmatch(images[name]):
                raise DeployError("IMAGE_UNPINNED", "Image is not immutable")
            checkpoint({"images": dict(images)})
        return images

    def _ids(self, job, require_app=True):
        keys = ("id", "app_id") if require_app else ("id",)
        if any(not isinstance(job.get(key), str) or not IDENTIFIER.fullmatch(job[key]) for key in keys):
            raise DeployError("RESOURCE_ID_INVALID", "Invalid application or job identifier")

    def _labels(self, job):
        self._ids(job)
        return {"awesome.managed": "true", "awesome.job": job["id"], "awesome.app": job["app_id"]}

    def _owned(self, labels, job):
        return all((labels or {}).get(key) == value for key, value in self._labels(job).items())

    def _label_flags(self, job):
        return [arg for key, value in self._labels(job).items() for arg in ("--label", f"{key}={value}")]

    def _container(self, job, service):
        self._ids(job)
        return f"awesome-{job['id']}-{service}"

    @staticmethod
    def _check_live_restore(info):
        if info.get("LiveRestoreEnabled") is not False:
            raise DeployError("EGRESS_UNSAFE", "Public HTTPS requires Docker live restore to be disabled")

    def _ensure_egress(self, job, info):
        network_policy = validate_policy(job)
        if network_policy is not None:
            self._check_live_restore(info)
            self.egress_guard.lock_fd = self.lock_fd
            identifiers = self._docker([
                "container", "ls", "-a", "--filter", f"label=awesome.job={job['id']}",
                "--format", "{{.ID}}",
            ]).split()
            running = False
            if identifiers:
                rows = json.loads(self._docker(["container", "inspect", *identifiers]))
                if (not isinstance(rows, list) or len(rows) != len(identifiers)
                        or any(not self._owned(row.get("Config", {}).get("Labels"), job) for row in rows)):
                    raise DeployError("EGRESS_NETWORK_IDENTITY", "Cannot identify existing release containers")
                running = any(row.get("State", {}).get("Running") is True for row in rows)
            if running:
                # Never repair a missing guard while business containers are running.
                # Failure stops this release; the next attempt can install after exit.
                self.egress_guard.check(network_policy)
            else:
                self.egress_guard.ensure(network_policy)
        return network_policy

    def _check_egress(self, job):
        network_policy = validate_policy(job)
        if network_policy is not None:
            self.egress_guard.lock_fd = self.lock_fd
            self.egress_guard.check(network_policy)

    def _stop_after_network_failure(self, job, error):
        if (validate_policy(job) is not None and
                (error.code.startswith("EGRESS_") or error.code in {
                    "RESOURCE_CONFLICT", "PORT_FORWARD_IDENTITY", "ACTIVE_NETWORK_CHANGED",
                    "ACTIVE_CONTAINER_CHANGED",
                })):
            self._stop_owned_containers(job, require_existing=False)

    def _external_network(self, job, name):
        return validate_policy(job) is not None and name in {
            f"aw-{job['id']}-x-{len(service)}-{service}"
            for service in job["snapshot"]["manifest"]["services"]
        }

    def _network_identity(self, job, row, name):
        external = self._external_network(job, name)
        result = {"id": row.get("Id"), "bridge": bridge_name(name, external), "internal": not external}
        if (row.get("Name") != name or row.get("Internal") is not (not external)
                or row.get("Driver") != "bridge" or not self._owned(row.get("Labels"), job)
                or row.get("EnableIPv6") is not False
                or row.get("Options") != {"com.docker.network.bridge.name": result["bridge"]}
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("Id", "")))):
            raise DeployError("EGRESS_NETWORK_IDENTITY", "Managed network does not match its frozen exit policy")
        saved = job.get("execution", {}).get("network_identities", {})
        if not isinstance(saved, dict) or (name in saved and saved[name] != result):
            raise DeployError("EGRESS_NETWORK_IDENTITY", "Persisted managed network identity changed")
        return result

    def _validate_container_network_policy(self, job, name, row):
        network_policy = validate_policy(job)
        if network_policy is None:
            return  # Legacy containers keep their original identity contract.
        if row.get("HostConfig", {}).get("Dns") != network_policy["dns"]:
            raise DeployError("EGRESS_NETWORK_IDENTITY", "Container DNS differs from its frozen exit policy")
        expected = self._service_networks(job)[name]
        state = row.get("State") or {}
        unallocated = (
            state.get("Status") == "created" and state.get("Running") is False
            and not any(state.get(flag) for flag in ("Paused", "Restarting", "Dead"))
            and name not in job["execution"].get("started", [])
            and name not in job["execution"].get("port_forwards", {})
        )
        pending = []
        for network, endpoint in (row.get("NetworkSettings", {}).get("Networks") or {}).items():
            if network not in expected:
                raise DeployError("EGRESS_NETWORK_IDENTITY", "Container has an unexpected network attachment")
            if endpoint.get("GwPriority", 0) != (1 if self._external_network(job, network) else 0):
                raise DeployError("EGRESS_NETWORK_IDENTITY", "Container exit gateway priority changed")
            saved = job["execution"].get("network_identities", {}).get(network)
            if saved is None:
                raise DeployError("EGRESS_NETWORK_IDENTITY", "Missing persisted managed network identity")
            if (unallocated and endpoint.get("NetworkID") == ""
                    and endpoint.get("EndpointID") == "" and endpoint.get("IPAddress") == ""):
                pending.append(network)
            elif endpoint.get("NetworkID") != saved.get("id"):
                raise DeployError("EGRESS_NETWORK_IDENTITY", "Container network differs from its persisted identity")
        if pending:
            # Docker 29 records named attachments on a created container before it
            # allocates endpoints. Validate those names against the frozen network
            # instances now; _forward_identity requires actual IDs after start.
            rows = json.loads(self._docker(["network", "inspect", *sorted(pending)]))
            if (not isinstance(rows, list) or len(rows) != len(pending)
                    or {network.get("Name") for network in rows} != set(pending)):
                raise DeployError("EGRESS_NETWORK_IDENTITY", "Created container network intent is ambiguous")
            for network in rows:
                self._network_identity(job, network, network["Name"])

    def _connect_network(self, job, network, name, container):
        self._check_egress(job)
        priority = ["--gw-priority=1"] if self._external_network(job, network) else []
        self._docker(["network", "connect", *priority, "--alias", name, network, container])

    def _ensure_network(self, job, name):
        network_policy = validate_policy(job)
        ids = self._docker(["network", "ls", "--filter", f"name=^{name}$", "--format", "{{.ID}}"]).split()
        if ids:
            if len(ids) != 1:
                raise DeployError("RESOURCE_CONFLICT", "Conflicting network identity")
            network = json.loads(self._docker(["network", "inspect", ids[0]]))[0]
            if network_policy is not None:
                return self._network_identity(job, network, name)
            if not self._owned(network.get("Labels"), job) or network.get("Internal") is not True:
                raise DeployError("RESOURCE_CONFLICT", "Refusing to use an unowned or external network")
        else:
            if network_policy is not None and name in job["execution"].get("network_identities", {}):
                raise DeployError("EGRESS_NETWORK_IDENTITY", "A persisted managed network is missing")
            external = self._external_network(job, name)
            options = (["--ipv6=false", "--opt", f"com.docker.network.bridge.name={bridge_name(name, external)}"]
                       if network_policy is not None else [])
            self._docker(
                ["network", "create", "--driver", "bridge", *([] if external else ["--internal"]),
                 *options, *self._label_flags(job), name]
            )
            if network_policy is not None:
                rows = json.loads(self._docker(["network", "inspect", name]))
                if not isinstance(rows, list) or len(rows) != 1:
                    raise DeployError("EGRESS_NETWORK_IDENTITY", "Cannot confirm created managed network")
                return self._network_identity(job, rows[0], name)

    def _service_networks(self, job):
        services = job["snapshot"]["manifest"]["services"]
        networks = {name: [f"aw-{job['id']}-s-{len(name)}-{name}"] for name in services}
        for name, service in services.items():
            for dependency in service.get("dependsOn") or {}:
                edge = f"aw-{job['id']}-e-{len(name)}-{name}-{len(dependency)}-{dependency}"
                networks[name].append(edge)
                networks[dependency].append(edge)
        if validate_policy(job) is not None:
            for name in services:
                networks[name].append(f"aw-{job['id']}-x-{len(name)}-{name}")
        return networks

    @staticmethod
    def _private_ipv4(value):
        try:
            address = ipaddress.IPv4Address(value)
            return (
                isinstance(value, str)
                and str(address) == value
                and any(
                    address in ipaddress.IPv4Network(block)
                    for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                )
            )
        except (ValueError, TypeError):
            return False

    def _validate_forward_record(self, job, name, record):
        services = job["snapshot"]["manifest"]["services"]
        expected_networks = self._service_networks(job)
        network = record.get("network") if isinstance(record, dict) else None
        spec = record.get("spec") if isinstance(record, dict) else None
        if (
            name not in services
            or not isinstance(record, dict)
            or set(record) != {"container_id", "network", "spec", "confirmed"}
            or type(record.get("confirmed")) is not bool
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("container_id", "")))
            or not isinstance(network, dict)
            or set(network) != {"name", "id"}
            or network.get("name") != expected_networks[name][0]
            or not re.fullmatch(r"[0-9a-f]{64}", str(network.get("id", "")))
            or not set(expected_networks[name]).issubset(set(job["execution"].get("networks", [])))
            or not isinstance(spec, dict)
            or set(spec) != {"proto", "parentIP", "parentPort", "childIP", "childPort"}
            or spec.get("proto") != "tcp4"
            or spec.get("parentIP") != "127.0.0.1"
            or type(spec.get("parentPort")) is not int
            or spec["parentPort"] != job["execution"].get("ports", {}).get(name)
            or not self.settings.port_start <= spec["parentPort"] <= self.settings.port_end
            or type(spec.get("childPort")) is not int
            or spec["childPort"] != services[name]["port"]
            or not 1 <= spec["childPort"] <= 65535
            or not self._private_ipv4(spec.get("childIP"))
        ):
            raise DeployError("PORT_FORWARD_IDENTITY", "Invalid persisted service forwarding identity")
        return record

    def _forward_identity(self, job, name, *, timeout=5, deadline=None):
        """Derive an endpoint only from the running owned container and its frozen networks."""

        def bounded():
            if deadline is None:
                return timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeployError("ACTIVE_RECOVERY_TIMEOUT", "Active recovery exceeded its time limit")
            return min(timeout, remaining)

        rows = json.loads(
            self._docker(["container", "inspect", self._container(job, name)], timeout=bounded())
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise DeployError("PORT_FORWARD_IDENTITY", "Ambiguous service container identity")
        row = rows[0]
        self._validate_container_network_policy(job, name, row)
        attached = row.get("NetworkSettings", {}).get("Networks") or {}
        expected = self._service_networks(job)[name]
        if (
            row.get("Name") != "/" + self._container(job, name)
            or not self._owned(row.get("Config", {}).get("Labels"), job)
            or row.get("State", {}).get("Running") is not True
            or any(row.get("State", {}).get(flag) for flag in ("Paused", "Restarting", "Dead"))
            or row.get("HostConfig", {}).get("PortBindings") not in (None, {})
            or set(attached) != set(expected)
        ):
            raise DeployError("PORT_FORWARD_IDENTITY", "Service container forwarding identity changed")
        inspected = json.loads(self._docker(["network", "inspect", *sorted(attached)], timeout=bounded()))
        if (
            len(inspected) != len(attached)
            or {network.get("Name") for network in inspected} != set(attached)
            or any(
                network.get("Internal") is not (not self._external_network(job, network.get("Name")))
                or network.get("Driver") != "bridge"
                or not self._owned(network.get("Labels"), job)
                or network.get("Id") != attached[network["Name"]].get("NetworkID")
                for network in inspected
            )
        ):
            raise DeployError(
                "PORT_FORWARD_IDENTITY", "Service requires its recorded owned internal networks"
            )
        if validate_policy(job) is not None:
            for inspected_network in inspected:
                self._network_identity(job, inspected_network, inspected_network["Name"])
        network = expected[0]
        record = {
            "confirmed": False,
            "container_id": row.get("Id"),
            "network": {"name": network, "id": attached[network].get("NetworkID")},
            "spec": {
                "proto": "tcp4",
                "parentIP": "127.0.0.1",
                "parentPort": job["execution"]["ports"][name],
                "childIP": attached[network].get("IPAddress"),
                "childPort": job["snapshot"]["manifest"]["services"][name]["port"],
            },
        }
        return self._validate_forward_record(job, name, record)

    @staticmethod
    def _forward_target(record):
        return {key: record[key] for key in ("container_id", "network", "spec")}

    def _checkpoint_forward(self, job, name, forward, confirmed, checkpoint):
        record = {**copy.deepcopy(forward), "confirmed": confirmed}
        self._validate_forward_record(job, name, record)
        forwards = copy.deepcopy(job["execution"].get("port_forwards", {}))
        forwards[name] = record
        checkpoint({"port_forwards": forwards})
        # Failed durability must never make an in-memory confirmation look committed.
        job["execution"]["port_forwards"] = forwards

    def _reconcile_forward(self, job, name, forward, checkpoint, *, deadline=None):
        def bounded():
            if deadline is None:
                return 5
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeployError("ACTIVE_RECOVERY_TIMEOUT", "Active recovery exceeded its time limit")
            return min(5, remaining)

        forwards = job["execution"].get("port_forwards", {})
        if not isinstance(forwards, dict):
            raise DeployError("PORT_FORWARD_IDENTITY", "Invalid persisted forwarding intentions")
        if name in forwards:
            saved = forwards[name]
            self._validate_forward_record(job, name, saved)
            if self._forward_target(saved) != self._forward_target(forward):
                raise DeployError("PORT_FORWARD_IDENTITY", "Refusing to change a recorded forward target")
            try:
                # An unconfirmed POST may still be running server-side. Never issue another one.
                self.port_forwarder.ensure(forward["spec"], timeout=bounded(), create=False)
            except DeployError as error:
                if not saved["confirmed"] or error.code != "PORT_FORWARD_UNCONFIRMED":
                    raise
                # A previously confirmed mapping disappeared; journal its replacement before POST.
            else:
                if not saved["confirmed"]:
                    self._checkpoint_forward(job, name, forward, True, checkpoint)
                return
        self._checkpoint_forward(job, name, forward, False, checkpoint)
        self.port_forwarder.ensure(forward["spec"], timeout=bounded(), create=True)
        self._checkpoint_forward(job, name, forward, True, checkpoint)

    def start(self, job, checkpoint):
        validate_policy(job)
        try:
            return self._start(job, checkpoint)
        except DeployError as error:
            self._stop_after_network_failure(job, error)
            raise

    def _start(self, job, checkpoint):
        info = self.preflight()
        network_policy = self._ensure_egress(job, info)
        services = job["snapshot"]["manifest"]["services"]
        storage = ManagedStorage(self._docker)
        volumes = dict(job["execution"].get("storage_volumes", {}))
        for name, service in services.items():
            if service.get("storage"):
                previous = job["execution"].get("previous_manifest", {}).get("services", {}).get(name, {})
                if previous.get("storage") and name not in volumes:
                    raise DeployError("STORAGE_IDENTITY", "Previous storage identity is missing; operator repair is required")
                volumes[name] = storage.ensure(job["app_id"], name, service["storage"], volumes.get(name))
                checkpoint({"storage_volumes": dict(volumes)})
                job["execution"]["storage_volumes"] = dict(volumes)
        networks = self._service_networks(job)
        network_identities = copy.deepcopy(job["execution"].get("network_identities", {}))
        for network in sorted({n for group in networks.values() for n in group}):
            identity = self._ensure_network(job, network)
            if network_policy is not None:
                network_identities[network] = identity
                checkpoint({"network_identities": copy.deepcopy(network_identities)})
                job["execution"]["network_identities"] = copy.deepcopy(network_identities)
        network_names = sorted({n for group in networks.values() for n in group})
        checkpoint({"networks": network_names})
        job["execution"]["networks"] = network_names
        started = set()
        while len(started) < len(services):
            progress = False
            for name, service in services.items():
                if name in started or not set(service.get("dependsOn") or {}).issubset(started):
                    continue
                for dependency, condition in (service.get("dependsOn") or {}).items():
                    if condition == "healthy":
                        self._health_service(job, dependency, time.monotonic() + self.settings.health_timeout)
                image_id = job["execution"]["images"][name]
                if not IMMUTABLE.fullmatch(image_id):
                    raise DeployError("IMAGE_UNPINNED", "Only immutable image IDs may be started")
                image = self._image(image_id)
                container_name = self._container(job, name)
                existing = self._docker(
                    ["container", "ls", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.ID}}"]
                ).split()
                if existing:
                    row = json.loads(self._docker(["container", "inspect", container_name]))[0]
                    if not self._owned(row.get("Config", {}).get("Labels"), job):
                        raise DeployError("RESOURCE_CONFLICT", "Refusing to reuse an unowned container")
                    storage.validate_mounts(job, name, row)
                    self._validate_container_network_policy(job, name, row)
                    attached = row.get("NetworkSettings", {}).get("Networks") or {}
                    if any(network not in networks[name] for network in attached):
                        raise DeployError(
                            "RESOURCE_CONFLICT", "Owned container has unexpected network attachments"
                        )
                    for network in networks[name]:
                        if network not in attached:
                            self._connect_network(job, network, name, container_name)
                else:
                    self._create_container(job, name, service, image, networks[name])
                if service.get("storage") or network_policy is not None:
                    # Docker may create an empty volume if it vanished after ensure;
                    # recheck the persisted volume instance and actual mount before start.
                    created = json.loads(self._docker(["container", "inspect", container_name]))[0]
                    storage.validate_mounts(job, name, created)
                    self._validate_container_network_policy(job, name, created)
                self._check_egress(job)
                self._docker(["container", "start", container_name])
                forward = self._forward_identity(job, name)
                self._reconcile_forward(job, name, forward, checkpoint)
                started.add(name)
                checkpoint({"started": sorted(started)})
                progress = True
            if not progress:
                raise DeployError("DEPENDENCY_CYCLE", "Service dependency graph cannot be started")

    @staticmethod
    def recovery_boot_id():
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or "unknown-boot"
        except OSError:
            return "unknown-boot"  # Periodic checks still work if the boot identifier is unavailable.

    def recover_active(self, job, checkpoint):
        validate_policy(job)
        try:
            return self._recover_active(job, checkpoint)
        except DeployError as error:
            self._stop_after_network_failure(job, error)
            raise

    def _recover_active(self, job, checkpoint):
        """Start only existing frozen active containers; never rebuild, recreate or touch routes."""
        self._ids(job)
        if (
            job.get("status") != "succeeded"
            or job["execution"].get("resources_released")
            or job["execution"].get("failure_intent")
        ):
            raise DeployError(
                "ACTIVE_RECOVERY_STATE", "Only a successful unreleased active version may be recovered"
            )
        deadline = time.monotonic() + self.settings.health_timeout

        def bounded():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeployError("ACTIVE_RECOVERY_TIMEOUT", "Active recovery exceeded its time limit")
            return min(10, remaining)

        # Existing containers need the runtime identity/resource guards, not a builder or build disk budget.
        info = self._runtime_preflight(timeout=bounded(), deadline=deadline)
        self._ensure_egress(job, info)
        services = job["snapshot"]["manifest"]["services"]
        names = [self._container(job, name) for name in services]
        rows = json.loads(self._docker(["container", "inspect", *names], timeout=bounded()))
        containers = {row.get("Name", "").removeprefix("/"): row for row in rows}
        networks = set()
        expected_networks = self._service_networks(job)
        for name, service in services.items():
            row = containers.get(self._container(job, name))
            if row is None:
                raise DeployError(
                    "ACTIVE_CONTAINER_MISSING", "An active container is missing; operator repair is required"
                )
            config, host = row.get("Config", {}), row.get("HostConfig", {})
            self._validate_container_network_policy(job, name, row)
            image = job["execution"].get("images", {}).get(name, "")
            forward = self._validate_forward_record(
                job, name, job["execution"].get("port_forwards", {}).get(name)
            )
            attachments = row.get("NetworkSettings", {}).get("Networks") or {}
            attached = set(row.get("NetworkSettings", {}).get("Networks") or {})
            state = row.get("State", {})
            if (
                row.get("Id") != forward["container_id"]
                or attachments.get(forward["network"]["name"], {}).get("NetworkID")
                != forward["network"]["id"]
                or state.get("Running")
                and attachments.get(forward["network"]["name"], {}).get("IPAddress")
                != forward["spec"]["childIP"]
            ):
                raise DeployError("PORT_FORWARD_IDENTITY", "Active service forwarding identity changed")
            if (
                not self._owned(config.get("Labels"), job)
                or not IMMUTABLE.fullmatch(image)
                or not (row.get("Image") == image or "@sha256:" in image and config.get("Image") == image)
                or host.get("PortBindings") not in (None, {})
                or not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", config.get("User", ""))
                or host.get("ReadonlyRootfs") is not True
                or host.get("RestartPolicy", {}).get("Name", "no") not in {"no", ""}
                or attached != set(expected_networks[name])
                or not attached.issubset(set(job["execution"].get("networks", [])))
                or state.get("Paused")
                or state.get("Restarting")
                or state.get("Dead")
            ):
                raise DeployError(
                    "ACTIVE_CONTAINER_CHANGED",
                    "An active container no longer matches its frozen identity or isolation",
                )
            ManagedStorage(self._docker).validate_mounts(job, name, row, timeout=bounded())
            networks.update(attached)
        inspected = json.loads(self._docker(["network", "inspect", *sorted(networks)], timeout=bounded()))
        if (
            len(inspected) != len(networks)
            or {row.get("Name") for row in inspected} != networks
            or any(
                row.get("Internal") is not (not self._external_network(job, row.get("Name")))
                or row.get("Driver") != "bridge"
                or not self._owned(row.get("Labels"), job)
                for row in inspected
            )
        ):
            raise DeployError(
                "ACTIVE_NETWORK_CHANGED", "Active containers require their owned internal networks"
            )
        if validate_policy(job) is not None:
            for network in inspected:
                self._network_identity(job, network, network["Name"])
        completed, checked, restarted = set(), set(), []
        while len(completed) < len(services):
            progress = False
            for name, service in services.items():
                dependencies = service.get("dependsOn") or {}
                if name in completed or not set(dependencies).issubset(completed):
                    continue
                row = containers[self._container(job, name)]
                if not row.get("State", {}).get("Running"):
                    for dependency, condition in dependencies.items():
                        if condition == "healthy" and dependency not in checked:
                            bounded()
                            self._health_service(job, dependency, deadline)
                            checked.add(dependency)
                    self._check_egress(job)
                    self._docker(["container", "start", self._container(job, name)], timeout=bounded())
                    restarted.append(name)
                # Even a running container may have lost its RootlessKit mapping after restart.
                # Reinspect after any start; never silently retarget an existing forwarding intent.
                forward = self._forward_identity(job, name, timeout=min(5, bounded()), deadline=deadline)
                self._reconcile_forward(job, name, forward, checkpoint, deadline=deadline)
                completed.add(name)
                progress = True
            if not progress:
                raise DeployError("DEPENDENCY_CYCLE", "Active dependencies cannot be recovered")
        # A started dependency need not be healthy until its dependents have also restarted.
        for name in services:
            if name not in checked:
                bounded()
                self._health_service(job, name, deadline)
                checked.add(name)
        return {"restarted": restarted}

    def _stop_owned_containers(self, job, *, require_existing):
        """Stop by inspected ID, then confirm exit before another release may use its data."""
        self._ids(job)
        services = job["snapshot"]["manifest"]["services"]
        rows = []
        # Validate all identities before stopping anything. A crash after any stop is
        # covered by the worker's already durable storage_handoff_intent.
        for name in services:
            container = self._container(job, name)
            if not require_existing:
                found = self._docker(["container", "ls", "-a", "--filter", f"name=^{container}$", "--format", "{{.ID}}"]).split()
                if not found:
                    continue
                if len(found) != 1:
                    raise DeployError("STORAGE_HANDOFF_IDENTITY", "Ambiguous candidate container identity")
            inspected = json.loads(self._docker(["container", "inspect", container]))
            if not isinstance(inspected, list) or len(inspected) != 1:
                raise DeployError("STORAGE_HANDOFF_IDENTITY", "Container inspection failed during storage handoff")
            row = inspected[0]
            image = job["execution"].get("images", {}).get(name, "")
            config = row.get("Config", {})
            if (not self._owned(config.get("Labels"), job)
                    or row.get("Name", "").removeprefix("/") != container
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("Id", "")))
                    or not IMMUTABLE.fullmatch(image)
                    or not (row.get("Image") == image or "@sha256:" in image and config.get("Image") == image)):
                raise DeployError("STORAGE_HANDOFF_IDENTITY", "Refusing to stop an unrecognized container")
            forward = job["execution"].get("port_forwards", {}).get(name)
            if require_existing or forward is not None:
                forward = self._validate_forward_record(job, name, forward)
                if row["Id"] != forward["container_id"]:
                    raise DeployError("STORAGE_HANDOFF_IDENTITY", "The previous container was replaced")
            ManagedStorage(self._docker).validate_mounts(job, name, row)
            rows.append(row)
        for row in rows:
            state = row.get("State", {})
            if state.get("Paused") or state.get("Restarting"):
                raise DeployError("STORAGE_HANDOFF_STATE", "Cannot safely stop a paused or restarting container")
            if state.get("Running"):
                self._docker(["container", "stop", "--time", "30", row["Id"]], timeout=45)
            confirmed = json.loads(self._docker(["container", "inspect", row["Id"]]))
            if (len(confirmed) != 1 or confirmed[0].get("Id") != row["Id"]
                    or confirmed[0].get("State", {}).get("Running") is not False
                    or confirmed[0].get("State", {}).get("Paused")
                    or confirmed[0].get("State", {}).get("Restarting")):
                raise DeployError("STORAGE_HANDOFF_STATE", "Container termination could not be confirmed")

    def stop_for_storage_handoff(self, previous):
        if previous.get("status") != "succeeded" or previous["execution"].get("resources_released"):
            raise DeployError("STORAGE_HANDOFF_STATE", "Only the existing active release can hand off storage")
        self._runtime_preflight()
        self._stop_owned_containers(previous, require_existing=True)

    def quiesce_storage_candidate(self, candidate):
        self._runtime_preflight()
        self._stop_owned_containers(candidate, require_existing=False)

    def _create_container(self, job, name, service, image, networks):
        environment = dict(service.get("env") or {})
        refs = service.get("secretRefs") or {}
        secret_values = {}
        if refs:
            secret_values = SecretStore(self.settings).resolve(
                job["snapshot"]["repository"],
                job["snapshot"]["manifest"]["app"]["subdomain"],
                refs,
                job["snapshot"]["secret_versions"][name],
            )
            environment.update(secret_values)
        for key, value in environment.items():
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str)
                or any(c in value for c in "\n\r\x00")
            ):
                raise DeployError(
                    "UNSUPPORTED_ENVIRONMENT", "Environment values must be single-line strings in phase one"
                )
        private_dir = self.settings.state_dir / "runtime-env"
        private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, env_path = tempfile.mkstemp(dir=private_dir, prefix=job["id"] + "-")
        try:
            with os.fdopen(fd, "w") as stream:
                for key, value in environment.items():
                    stream.write(f"{key}={value}\n")
            limits = service["resources"]
            args = [
                "container",
                "create",
                "--name",
                self._container(job, name),
                *self._label_flags(job),
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--user",
                self.settings.runtime_user,
                "--cpus",
                str(limits["cpu"]),
                "--memory",
                f"{limits['memoryMiB']}m",
                "--memory-swap",
                f"{limits['memoryMiB']}m",
                "--pids-limit",
                "128",
                "--ulimit",
                "nofile=1024:1024",
                "--restart=no",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=5m",
                "--log-opt",
                "max-file=2",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                "--shm-size",
                "16m",
                "--no-healthcheck",
                "--network",
                networks[0],
                "--network-alias",
                name,
                *[f"--dns={address}" for address in (validate_policy(job) or {"dns": ["127.0.0.1"]})["dns"]],
                "--env-file",
                env_path,
            ]
            if service.get("storage"):
                volume = job["execution"].get("storage_volumes", {}).get(name)
                expected, _ = ManagedStorage.identity(job["app_id"], name, service["storage"])
                if not volume or volume.get("name") != expected:
                    raise DeployError("STORAGE_IDENTITY", "Container creation requires its persisted volume identity")
                args.extend(["--mount", f"type=volume,source={expected},destination=/data"])
            if "workingDir" in service:
                args.extend(["--workdir", service["workingDir"]])
            if service.get("entrypoint"):
                args.extend(["--entrypoint", service["entrypoint"][0]])
            args.append(job["execution"]["images"][name])
            if service.get("entrypoint"):
                args.extend(service["entrypoint"][1:])
            if "command" in service:
                args.extend(service["command"])
            elif service.get("entrypoint"):
                args.extend(image.get("Config", {}).get("Cmd") or [])
            self._docker(args, secrets=tuple(secret_values.values()))
            for network in networks[1:]:
                self._connect_network(job, network, name, self._container(job, name))
        finally:
            Path(env_path).unlink(missing_ok=True)

    def _health_service(self, job, name, deadline):
        service = job["snapshot"]["manifest"]["services"][name]
        health = service["healthcheck"]
        failures, grace = 0, time.monotonic() + health["startPeriodSeconds"]
        reason = "HEALTH_TIMEOUT"
        while time.monotonic() < deadline:
            success = False
            probe = "inspect"
            reason = "HEALTH_CHECK_ERROR"
            try:
                row = json.loads(
                    self._docker(
                        ["container", "inspect", self._container(job, name)],
                        timeout=min(health["timeoutSeconds"], max(0.01, deadline - time.monotonic())),
                    )
                )[0]
                if not self._owned(row.get("Config", {}).get("Labels"), job):
                    raise ValueError("Health check container ownership could not be verified")
                state = row.get("State", {})
                if state.get("Running") is False:
                    reason = "HEALTH_OOM_KILLED" if state.get("OOMKilled") is True else "HEALTH_CONTAINER_EXITED"
                elif state.get("Running") is not True:
                    raise ValueError("Health check container state is invalid")
                elif health["type"] == "exec":
                    probe = "exec"
                    self._docker(
                        ["container", "exec", self._container(job, name), *health["command"]],
                        timeout=min(health["timeoutSeconds"], max(0.01, deadline - time.monotonic())),
                    )
                    success = True
                else:
                    probe = "http"
                    status = http_status(
                        "127.0.0.1",
                        job["execution"]["ports"][name],
                        health["path"],
                        timeout=min(health["timeoutSeconds"], max(0.01, deadline - time.monotonic())),
                        headers={
                            "Host": f"{job['snapshot']['manifest']['app']['subdomain']}.{self.settings.base_domain}"
                        },
                    )
                    success = 200 <= status < 400
                    reason = "HEALTH_HTTP_STATUS"
            except TimeoutError:
                reason = "HEALTH_TIMEOUT"
            except DeployError as exc:
                if exc.code == "COMMAND_TIMEOUT":
                    reason = "HEALTH_TIMEOUT"
                elif probe == "exec" and exc.code == "COMMAND_FAILED":
                    reason = "HEALTH_EXEC_FAILED"
            except OSError:
                if probe == "http":
                    reason = "HEALTH_CONNECTION_FAILED"
            except (http.client.HTTPException, ValueError, TypeError, IndexError, KeyError, AttributeError):
                # Keep only a fixed category, never Docker output, HTTP bodies or exception text.
                reason = "HEALTH_CHECK_ERROR"
            if success:
                return
            if time.monotonic() >= grace:
                failures += 1
                if failures >= health["retries"]:
                    break
            time.sleep(min(health["intervalSeconds"], max(0, deadline - time.monotonic())))
        raise DeployError("HEALTH_FAILED", f"Service {name} failed candidate health checks",
                          f"services.{name}.healthcheck", reason=reason)

    def health(self, job):
        deadline = time.monotonic() + self.settings.health_timeout
        for name in job["snapshot"]["manifest"]["services"]:
            self._health_service(job, name, deadline)

    def _gateway(self, operation, job):
        self.gateway.lock_fd = self.lock_fd
        return self.gateway.call(operation, job)

    def ensure_https(self, job):
        self._gateway("ensure_https", job)

    def publish(self, job):
        self._gateway("publish", job)

    def verify(self, job):
        self._gateway("verify", job)

    def rollback(self, job):
        self._gateway("rollback", job)

    def offline(self, job, checkpoint):
        """Replay a durable deactivation without changing data or deployment inputs."""
        if job.get("status") != "succeeded" or job["execution"].get("resources_released"):
            raise DeployError("OFFLINE_STATE", "Only an unreleased successful application may go offline.")
        checkpoint({"phase": "offline_deactivate", "deactivate_intent": True})
        self._runtime_preflight()
        result = self._gateway("deactivate", job)
        if not isinstance(result, dict) or result.get("deactivated") is not True or result.get("release") != job["id"]:
            raise DeployError("GATEWAY_DEACTIVATE_FAILED", "Gateway did not confirm the exact release is offline.")
        checkpoint({"gateway_deactivated": True})
        # Missing containers are valid on replay after partially completed cleanup.
        # Every existing container still has its ownership, image and storage checked.
        checkpoint({"phase": "offline_stop", "stop_intent": True})
        self._stop_owned_containers(job, require_existing=False)
        checkpoint({"containers_stopped": True})
        checkpoint({"phase": "offline_cleanup", "cleanup_intent": True})
        self.cleanup(job)
        checkpoint({"cleanup_confirmed": True})

    def cleanup(self, job):
        # Filter is convenience only. Independently inspect ownership before every removal.
        self._ids(job, require_app=False)
        forwards = job.get("execution", {}).get("port_forwards", {})
        if not isinstance(forwards, dict):
            raise DeployError("PORT_FORWARD_IDENTITY", "Invalid persisted forwarding intentions")
        if forwards:
            self._ids(job)
            for name, forward in forwards.items():
                self._validate_forward_record(job, name, forward)
            # Remove every persisted exact mapping before destroying targets or releasing storage.
            # A failure leaves Docker resources and files for the worker's normal cleanup retry.
            for forward in forwards.values():
                self.port_forwarder.remove(forward["spec"], require_present=not forward["confirmed"])
        if job.get("app_id") and (
            job.get("execution", {}).get("ports") or job.get("execution", {}).get("images")
        ):
            self._ids(job)
            for kind in ("container", "network", "image"):
                args = [kind, "ls"] + (["-a"] if kind == "container" else [])
                ids = self._docker(
                    [*args, "--filter", f"label=awesome.job={job['id']}", "--format", "{{.ID}}"]
                ).split()
                for identifier in dict.fromkeys(ids):
                    row = json.loads(self._docker([kind, "inspect", identifier]))[0]
                    labels = (
                        row.get("Config", {}).get("Labels")
                        if kind in {"container", "image"}
                        else row.get("Labels")
                    )
                    if self._owned(labels, job):
                        if kind == "image":
                            tags = row.get("RepoTags") or []
                            allowed = {
                                f"awesome/{job['id'].lower()}-{service}:candidate"
                                for service in (job.get("snapshot") or {})
                                .get("manifest", {})
                                .get("services", {})
                            }
                            for tag in tags:
                                if tag in allowed:
                                    self._docker(["image", "rm", tag])
                            if not tags or tags == ["<none>:<none>"]:
                                self._docker(["image", "rm", identifier])
                        else:
                            self._docker([kind, "rm", *(["-f"] if kind == "container" else []), identifier])
        # No arbitrary snapshot paths: only job-ID children of platform-owned directories.
        bases = [self.settings.state_dir.resolve() / name for name in ("sources", "git", "builds")]
        if self.storage_root is None and job.get("execution", {}).get("build_storage_root"):
            self._check_storage({"DockerRootDir": job["execution"]["build_storage_root"]}, require_free=False)
        elif self.storage_root is None and (job.get("snapshot") or {}).get("build_contexts"):
            self._check_storage(
                json.loads(self._docker(["info", "--format", "{{json .}}"])), require_free=False
            )
        if self.storage_root is not None:
            bases.append(self.storage_root / "awesome-build-contexts")
        for base in bases:
            path = base / job["id"]
            if path.is_symlink():
                raise DeployError("CLEANUP_UNSAFE", "Refusing to remove a symlinked job directory")
            if path.exists():
                shutil.rmtree(path)
        environment_dir = self.settings.state_dir / "runtime-env"
        if environment_dir.exists():
            for path in environment_dir.glob(job["id"] + "-*"):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
