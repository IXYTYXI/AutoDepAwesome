"""First-phase stateless Docker backend. Requires a dedicated rootless Docker daemon.

Internal bridge networks restrict external routing and isolate dependency groups, but
are not a directional firewall or a hostile-code VM boundary. Never share this daemon
with privileged workloads. No host networking, host mounts, build secrets or SSH agent.
"""

import copy
import http.client
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
from .processes import run_command, http_status
from .secrets_store import SecretStore


IMMUTABLE = re.compile(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,64}")
IMAGE_REF = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,511}")


class DeploymentBackend:
    def __init__(self, settings):
        self.settings = settings
        self.gateway = Gateway(settings)
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

    def _runtime_preflight(self, timeout=120):
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
        return info

    def preflight(self):
        info = self._runtime_preflight()
        self.settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.settings.state_dir).free < self.settings.required_free_mib * 1024 * 1024:
            raise DeployError("DISK_CAPACITY", "Insufficient free disk space for deployment")
        self._check_storage(info)
        if self.settings.build_network != "none":
            raise DeployError("UNSUPPORTED_CAPABILITY", "First-phase builds require build_network=none")

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
        self.preflight()
        self._builder()
        self.gateway.lock_fd = self.lock_fd
        self.gateway.check()
        return {
            "ready": True,
            "runtime": "rootless-docker",
            "build_network": "none",
            "limitations": [
                "Stateless services only; no persistent volumes.",
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
                "UNSUPPORTED_CAPABILITY", "Images declaring volumes are unsupported in stateless deployments"
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
        self.preflight()
        self._ids(job, require_app=False)
        checkpoint({"build_storage_root": str(self.storage_root)})
        frozen = copy.deepcopy(snapshot)
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

    def _ensure_network(self, job, name):
        ids = self._docker(["network", "ls", "--filter", f"name=^{name}$", "--format", "{{.ID}}"]).split()
        if ids:
            if len(ids) != 1:
                raise DeployError("RESOURCE_CONFLICT", "Conflicting network identity")
            network = json.loads(self._docker(["network", "inspect", ids[0]]))[0]
            if not self._owned(network.get("Labels"), job) or network.get("Internal") is not True:
                raise DeployError("RESOURCE_CONFLICT", "Refusing to use an unowned or external network")
        else:
            self._docker(
                ["network", "create", "--driver", "bridge", "--internal", *self._label_flags(job), name]
            )

    def start(self, job, checkpoint):
        self.preflight()
        services = job["snapshot"]["manifest"]["services"]
        networks = {name: [f"aw-{job['id']}-s-{len(name)}-{name}"] for name in services}
        for name, service in services.items():
            for dependency in service.get("dependsOn") or {}:
                edge = f"aw-{job['id']}-e-{len(name)}-{name}-{len(dependency)}-{dependency}"
                networks[name].append(edge)
                networks[dependency].append(edge)
        for network in sorted({n for group in networks.values() for n in group}):
            self._ensure_network(job, network)
        checkpoint({"networks": sorted({n for group in networks.values() for n in group})})
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
                    attached = row.get("NetworkSettings", {}).get("Networks") or {}
                    if any(network not in networks[name] for network in attached):
                        raise DeployError(
                            "RESOURCE_CONFLICT", "Owned container has unexpected network attachments"
                        )
                    for network in networks[name]:
                        if network not in attached:
                            self._docker(["network", "connect", "--alias", name, network, container_name])
                else:
                    self._create_container(job, name, service, image, networks[name])
                self._docker(["container", "start", container_name])
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

    def recover_active(self, job):
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
        self._runtime_preflight(timeout=bounded())
        services = job["snapshot"]["manifest"]["services"]
        names = [self._container(job, name) for name in services]
        rows = json.loads(self._docker(["container", "inspect", *names], timeout=bounded()))
        containers = {row.get("Name", "").removeprefix("/"): row for row in rows}
        networks = set()
        for name, service in services.items():
            row = containers.get(self._container(job, name))
            if row is None:
                raise DeployError(
                    "ACTIVE_CONTAINER_MISSING", "An active container is missing; operator repair is required"
                )
            config, host = row.get("Config", {}), row.get("HostConfig", {})
            image = job["execution"].get("images", {}).get(name, "")
            expected_binding = {
                str(service["port"]) + "/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": str(job["execution"]["ports"][name])}
                ]
            }
            attached = set(row.get("NetworkSettings", {}).get("Networks") or {})
            state = row.get("State", {})
            if (
                not self._owned(config.get("Labels"), job)
                or not IMMUTABLE.fullmatch(image)
                or not (row.get("Image") == image or "@sha256:" in image and config.get("Image") == image)
                or host.get("PortBindings") != expected_binding
                or not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", config.get("User", ""))
                or host.get("ReadonlyRootfs") is not True
                or host.get("RestartPolicy", {}).get("Name", "no") not in {"no", ""}
                or any(mount.get("Type") in {"bind", "volume"} for mount in row.get("Mounts", []))
                or not attached
                or not attached.issubset(set(job["execution"].get("networks", attached)))
                or state.get("Paused")
                or state.get("Restarting")
                or state.get("Dead")
            ):
                raise DeployError(
                    "ACTIVE_CONTAINER_CHANGED",
                    "An active container no longer matches its frozen identity or isolation",
                )
            networks.update(attached)
        inspected = json.loads(self._docker(["network", "inspect", *sorted(networks)], timeout=bounded()))
        if (
            len(inspected) != len(networks)
            or {row.get("Name") for row in inspected} != networks
            or any(
                row.get("Internal") is not True or not self._owned(row.get("Labels"), job)
                for row in inspected
            )
        ):
            raise DeployError(
                "ACTIVE_NETWORK_CHANGED", "Active containers require their owned internal networks"
            )
        completed, checked, restarted = set(), set(), []
        retry_health = job["execution"].get("active_recovery", {}).get("retry_health", False)
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
                            self._health_service(job, dependency, deadline)
                            checked.add(dependency)
                    self._docker(["container", "start", self._container(job, name)], timeout=bounded())
                    restarted.append(name)
                    self._health_service(job, name, deadline)
                    checked.add(name)
                elif retry_health and name not in checked:
                    self._health_service(job, name, deadline)
                    checked.add(name)
                completed.add(name)
                progress = True
            if not progress:
                raise DeployError("DEPENDENCY_CYCLE", "Active dependencies cannot be recovered")
        return {"restarted": restarted}

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
                "--dns=127.0.0.1",
                "--publish",
                f"127.0.0.1:{job['execution']['ports'][name]}:{service['port']}",
                "--env-file",
                env_path,
            ]
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
                self._docker(["network", "connect", "--alias", name, network, self._container(job, name)])
        finally:
            Path(env_path).unlink(missing_ok=True)

    def _health_service(self, job, name, deadline):
        service = job["snapshot"]["manifest"]["services"][name]
        health = service["healthcheck"]
        failures, grace = 0, time.monotonic() + health["startPeriodSeconds"]
        while time.monotonic() < deadline:
            success = False
            try:
                row = json.loads(
                    self._docker(
                        ["container", "inspect", self._container(job, name)],
                        timeout=min(health["timeoutSeconds"], max(0.01, deadline - time.monotonic())),
                    )
                )[0]
                if not self._owned(row.get("Config", {}).get("Labels"), job) or not row.get("State", {}).get(
                    "Running"
                ):
                    raise DeployError("HEALTH_FAILED", f"Service {name} is not running")
                if health["type"] == "exec":
                    self._docker(
                        ["container", "exec", self._container(job, name), *health["command"]],
                        timeout=min(health["timeoutSeconds"], max(0.01, deadline - time.monotonic())),
                    )
                    success = True
                else:
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
            except (OSError, http.client.HTTPException, DeployError):
                success = False
            if success:
                return
            if time.monotonic() >= grace:
                failures += 1
                if failures >= health["retries"]:
                    break
            time.sleep(min(health["intervalSeconds"], max(0, deadline - time.monotonic())))
        raise DeployError("HEALTH_FAILED", f"Service {name} failed candidate health checks")

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

    def cleanup(self, job):
        # Filter is convenience only. Independently inspect ownership before every removal.
        self._ids(job, require_app=False)
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
