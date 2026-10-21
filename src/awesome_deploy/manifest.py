"""Strict, bounded awesome.yaml parsing and semantic validation."""
from __future__ import annotations

import copy
import json
import math
from importlib.resources import files
from pathlib import PurePosixPath

import jsonschema
import yaml

from .config import Settings
from .errors import DeployError

MAX_MANIFEST_BYTES = 256 * 1024
MAX_YAML_DEPTH = 32
MAX_YAML_NODES = 10000
UNSUPPORTED = frozenset({"mounts", "migration", "lifecycle", "customegress", "customEgress", "egress"})


def schema_document() -> dict:
    """Return an independent, machine-readable v1 schema."""
    return json.loads(files("awesome_deploy").joinpath("schema/awesome-v1.json").read_text())


def _invalid(message: str, path: str = "") -> DeployError:
    return DeployError("INVALID_MANIFEST", message, path, "Correct awesome.yaml and submit again.")


class _StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise _invalid("All mapping keys must be strings.")
            if key in result:
                raise _invalid("Duplicate mapping key.", key)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _parse(text: str):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise _invalid("Manifest exceeds the 256 KiB size limit.")
    depth = nodes = 0
    try:
        for event in yaml.parse(text):
            nodes += 1
            if nodes > MAX_YAML_NODES:
                raise _invalid("Manifest contains too many nodes.")
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) or getattr(event, "tag", None):
                raise _invalid("YAML aliases, anchors and explicit tags are not supported.")
            if isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise _invalid("Manifest nesting exceeds the depth limit.")
            elif isinstance(event, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                depth -= 1
        return yaml.load(text, Loader=_StrictLoader)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise _invalid("Manifest is not valid supported YAML.") from exc


def _unsupported(data: dict):
    candidates = [("", data)]
    services = data.get("services")
    if isinstance(services, dict):
        candidates.extend((f"services.{name}", service) for name, service in services.items() if isinstance(service, dict))
        for name, service in services.items():
            if isinstance(service, dict) and isinstance(service.get("kind"), str) and service["kind"] != "web":
                raise DeployError("UNSUPPORTED_CAPABILITY", "Only web service kinds are available in phase one.",
                                  f"services.{name}.kind", "Use a web service or remove the unsupported service.")
    for prefix, value in candidates:
        for key in UNSUPPORTED.intersection(value):
            if value[key]:
                path = f"{prefix}.{key}".lstrip(".")
                raise DeployError("UNSUPPORTED_CAPABILITY", f"The {key} capability is not available in phase one.", path,
                                  "Remove this capability before deploying.")


def _relative_path(value: str, path: str, *, allow_dot=False) -> PurePosixPath:
    if "\\" in value or any(ord(c) < 32 for c in value) or value.startswith("/"):
        raise _invalid("Build paths must be safe repository-relative paths.", path)
    parts = value.split("/")
    if any(p in {"", "..", ".git"} for p in parts) or ("." in parts and value != "."):
        raise _invalid("Build paths cannot contain traversal or empty components.", path)
    if value == "." and not allow_dot:
        raise _invalid("Dockerfile must identify a file.", path)
    return PurePosixPath(value)


def load_manifest(text: str, settings: Settings, previous: dict | None = None) -> dict:
    """Validate text and return a detached normalized v1 manifest.

    Omitted resources inherit each existing service's last normalized resources,
    then fall back to 0.5 CPU and 512 MiB. Values and keys never interpolate.
    Build context is repository-relative; Dockerfile is relative to that context.
    """
    data = _parse(text)
    if not isinstance(data, dict):
        raise _invalid("Manifest root must be a mapping.")
    _unsupported(data)
    errors = sorted(jsonschema.Draft202012Validator(schema_document()).iter_errors(data),
                    key=lambda error: [str(p) for p in error.absolute_path])
    if errors:
        error = errors[0]
        path = ".".join(map(str, error.absolute_path))
        # jsonschema messages can contain env literals; report only safe structure.
        raise _invalid(f"Manifest field does not satisfy the {error.validator} rule.", path)
    data = copy.deepcopy(data)
    subdomain = data["app"]["subdomain"]
    if subdomain in settings.reserved_names or any(word in subdomain for word in settings.banned_words):
        raise _invalid("Subdomain is reserved or contains a prohibited word.", "app.subdomain")
    previous_services = (previous or {}).get("services", {})
    validate_storage_transition(data, previous)
    storage_names = [service["storage"]["name"] for service in data["services"].values() if "storage" in service]
    if len(storage_names) != len(set(storage_names)):
        raise _invalid("Storage names cannot be shared by services.", "services")
    for name, service in data["services"].items():
        prefix = f"services.{name}"
        old = previous_services.get(name, {}).get("resources", {})
        resources = {"cpu": old.get("cpu", .5), "memoryMiB": old.get("memoryMiB", 512)}
        resources.update(service.get("resources", {}))
        cpu, memory = resources["cpu"], resources["memoryMiB"]
        if (isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not math.isfinite(cpu)
                or not .05 <= cpu <= settings.max_cpu):
            raise _invalid("CPU allocation exceeds the configured limits.", prefix + ".resources.cpu")
        if isinstance(memory, bool) or not isinstance(memory, int) or not 32 <= memory <= settings.max_memory_mib:
            raise _invalid("Memory allocation exceeds the configured limits.", prefix + ".resources.memoryMiB")
        service["resources"] = resources
        health = service["healthcheck"]
        for key, value in {"intervalSeconds": 10, "timeoutSeconds": 3, "retries": 3, "startPeriodSeconds": 30}.items():
            health.setdefault(key, value)
        if health["timeoutSeconds"] > health["intervalSeconds"]:
            raise _invalid("Healthcheck timeout cannot exceed its interval.", prefix + ".healthcheck.timeoutSeconds")
        service.setdefault("env", {})
        service.setdefault("secretRefs", {})
        if set(service["env"]) & set(service["secretRefs"]):
            raise _invalid("An environment name cannot also be a secret reference.", prefix + ".secretRefs")
        service.setdefault("dependsOn", {})
        if "build" in service:
            build = service["build"]
            _relative_path(build["context"], prefix + ".build.context", allow_dot=True)
            _relative_path(build["dockerfile"], prefix + ".build.dockerfile")
        if "workingDir" in service and ".." in service["workingDir"].split("/"):
            raise _invalid("workingDir cannot contain traversal.", prefix + ".workingDir")
        for dependency in service["dependsOn"]:
            if dependency not in data["services"]:
                raise _invalid("Dependency references an unknown service.", prefix + ".dependsOn")
    total_memory = sum(service["resources"]["memoryMiB"] for service in data["services"].values())
    if total_memory > settings.max_app_memory_mib:
        raise _invalid("Application memory allocation exceeds the configured limit.", "services")
    visited, visiting = set(), set()

    def visit(name):
        if name in visiting:
            raise _invalid("Service dependency graph contains a cycle.", f"services.{name}.dependsOn")
        if name in visited:
            return
        visiting.add(name)
        for dependency in data["services"][name]["dependsOn"]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in data["services"]:
        visit(name)
    seen = set()
    for index, route in enumerate(data["routes"]):
        path = route["path"]
        if "//" in path or any(part in {".", ".."} for part in path.split("/")):
            raise _invalid("Route path must be a literal canonical URL path.", f"routes.{index}.path")
        key = (path.rstrip("/") or "/", route["match"])
        if key in seen:
            raise _invalid("Duplicate route path and match mode.", f"routes.{index}")
        seen.add(key)
        if route["service"] not in data["services"]:
            raise _invalid("Route references an unknown service.", f"routes.{index}.service")
        route.setdefault("stripPrefix", False)
    return data


def validate_storage_transition(manifest, previous):
    """Keep existing data reachable across updates, including service rename/removal."""
    for name, service in (previous or {}).get("services", {}).items():
        if service.get("storage") and manifest.get("services", {}).get(name, {}).get("storage") != service["storage"]:
            raise _invalid("Existing storage cannot be removed, renamed or moved.", f"services.{name}.storage")
