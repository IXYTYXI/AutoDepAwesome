import copy
import json

import pytest
import yaml

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError


def valid():
    return {
        "schemaVersion": 1,
        "app": {"name": "Demo", "description": "A demo app", "subdomain": "demo"},
        "services": {"web": {"kind": "web", "image": "nginx:alpine", "port": 8080, "healthcheck": {"type": "http", "path": "/"}}},
        "routes": [{"path": "/", "match": "prefix", "service": "web"}],
    }


def load(data, previous=None):
    from awesome_deploy.manifest import load_manifest
    return load_manifest(yaml.safe_dump(data), Settings(), previous)


def test_schema_module_available():
    from importlib.util import find_spec
    assert find_spec("awesome_deploy.manifest") is not None, "manifest validation is required"


def test_defaults_and_resource_inheritance():
    result = load(valid())
    assert result["services"]["web"]["resources"] == {"cpu": .5, "memoryMiB": 512}
    assert result["services"]["web"]["healthcheck"] == {
        "type": "http", "path": "/", "intervalSeconds": 10,
        "timeoutSeconds": 3, "retries": 3, "startPeriodSeconds": 30,
    }
    assert result["routes"][0]["stripPrefix"] is False
    previous = copy.deepcopy(result)
    previous["services"]["web"]["resources"] = {"cpu": 1.2, "memoryMiB": 700}
    changed = valid()
    changed["services"]["web"]["resources"] = {"cpu": 2}
    assert load(changed, previous)["services"]["web"]["resources"] == {"cpu": 2, "memoryMiB": 700}


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("second_memory", [512, 513])
def test_application_memory_limit_sums_all_services(version, second_memory):
    data = valid()
    data["schemaVersion"] = version
    data["services"]["api"] = copy.deepcopy(data["services"]["web"])
    data["services"]["api"]["resources"] = {"memoryMiB": second_memory}
    if second_memory == 512:
        assert sum(service["resources"]["memoryMiB"] for service in load(data)["services"].values()) == 1024
    else:
        with pytest.raises(DeployError) as caught:
            load(data)
        assert caught.value.code == "INVALID_MANIFEST"
        assert caught.value.path == "services"


def test_application_memory_limit_includes_inherited_resources():
    data = valid()
    data["services"]["api"] = copy.deepcopy(data["services"]["web"])
    data["services"]["web"]["resources"] = {"memoryMiB": 324}
    data["services"]["api"]["resources"] = {"memoryMiB": 700}
    previous = load(data)
    data["services"]["web"]["resources"] = {"memoryMiB": 325}
    del data["services"]["api"]["resources"]
    with pytest.raises(DeployError) as caught:
        load(data, previous)
    assert caught.value.code == "INVALID_MANIFEST"


def test_application_memory_limit_is_configurable():
    from awesome_deploy.manifest import load_manifest

    data = valid()
    data["services"]["web"]["resources"] = {"memoryMiB": 513}
    with pytest.raises(DeployError) as caught:
        load_manifest(yaml.safe_dump(data), Settings(max_app_memory_mib=512))
    assert caught.value.code == "INVALID_MANIFEST"


@pytest.mark.parametrize("key,value", [
    ("kind", "worker"), ("port", True), ("port", 0), ("port", 65536),
    ("command", "npm start"), ("entrypoint", []), ("unknown", "x"),
    ("env", {"FOO": 1}), ("secretRefs", {"TOKEN": {"value": "secret"}}),
    ("resources", {"cpu": 0}), ("resources", {"memoryMiB": 99999}),
    ("healthcheck", {"type": "tcp"}), ("healthcheck", {"type": "http", "path": "/", "retries": 0}),
])
def test_reject_invalid_service(key, value):
    data = valid()
    data["services"]["web"][key] = value
    with pytest.raises(DeployError):
        load(data)


@pytest.mark.parametrize("subdomain", ["Upper", "toolong8", "-foo", "foo-", "a.b", "admin", "ohshit", ""])
def test_subdomain_policy(subdomain):
    data = valid()
    data["app"]["subdomain"] = subdomain
    with pytest.raises(DeployError):
        load(data)


@pytest.mark.parametrize("capability", ["mounts", "migration", "lifecycle", "customegress"])
def test_unsupported_features_get_actionable_error(capability):
    data = valid()
    data["services"]["web"][capability] = {"enabled": True}
    with pytest.raises(DeployError) as raised:
        load(data)
    assert raised.value.code == "UNSUPPORTED_CAPABILITY"
    assert capability in raised.value.path


@pytest.mark.parametrize("path", ["/a;return 200", "/$uri", "/../secret", "/a\nlocation /", "/a{", "/a%2fb", "/a//b", "relative"])
def test_routes_cannot_inject_nginx(path):
    data = valid()
    data["routes"][0]["path"] = path
    with pytest.raises(DeployError):
        load(data)


def test_route_references_and_duplicates():
    data = valid()
    data["routes"][0]["service"] = "missing"
    with pytest.raises(DeployError):
        load(data)
    data = valid()
    data["routes"].append(dict(data["routes"][0]))
    with pytest.raises(DeployError):
        load(data)


def test_dependency_graph_and_healthy_requirement():
    data = valid()
    data["services"]["web"]["dependsOn"] = {"missing": "healthy"}
    with pytest.raises(DeployError):
        load(data)
    data["services"]["other"] = {"kind": "web", "image": "x", "port": 1234, "healthcheck": {"type": "http", "path": "/"}, "dependsOn": {"web": "started"}}
    data["services"]["web"]["dependsOn"] = {"other": "healthy"}
    with pytest.raises(DeployError) as raised:
        load(data)
    assert raised.value.code == "INVALID_MANIFEST"
    data["services"]["other"].pop("dependsOn")
    assert load(data)["services"]["web"]["dependsOn"] == {"other": "healthy"}


@pytest.mark.parametrize("context,dockerfile", [("../x", "Dockerfile"), ("/tmp", "Dockerfile"), (".", "../Dockerfile"), (".", "a\\Dockerfile")])
def test_build_paths_are_repo_relative_and_contained(context, dockerfile):
    data = valid()
    data["services"]["web"].pop("image")
    data["services"]["web"]["build"] = {"context": context, "dockerfile": dockerfile}
    with pytest.raises(DeployError):
        load(data)


def test_build_image_exclusivity():
    data = valid()
    data["services"]["web"]["build"] = {"context": ".", "dockerfile": "Dockerfile"}
    with pytest.raises(DeployError):
        load(data)
    del data["services"]["web"]["image"]
    assert load(data)["services"]["web"]["build"]["context"] == "."


@pytest.mark.parametrize("name", ["BUILDKIT_SYNTAX", "BUILDKIT_INLINE_CACHE", "BUILDX_EXPERIMENTAL", "buildkit_syntax"])
def test_build_arguments_cannot_override_builder_policy(name):
    from awesome_deploy.manifest import schema_document
    from jsonschema import Draft202012Validator
    data = valid()
    service = data["services"]["web"]
    service.pop("image")
    service["build"] = {"context": ".", "dockerfile": "Dockerfile", "args": {name: "custom"}}
    assert not Draft202012Validator(schema_document()).is_valid(data)
    with pytest.raises(DeployError):
        load(data)


@pytest.mark.parametrize("text", [
    "version: 1\nversion: 1\n", "a: &a [1]\nb: *a\n", "a: !!str test\n",
    "a: " + "[" * 80 + "1" + "]" * 80,
    "#" * 300000,
])
def test_reject_ambiguous_or_abusive_yaml(text):
    from awesome_deploy.manifest import load_manifest
    with pytest.raises(DeployError):
        load_manifest(text, Settings())


def test_machine_readable_schema_is_strict():
    from awesome_deploy.manifest import schema_document
    import jsonschema
    schema = schema_document()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(valid(), schema)
    data = valid()
    data["unexpected"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)
    json.dumps(schema)


def test_healthcheck_must_be_explicit():
    data = valid()
    del data["services"]["web"]["healthcheck"]
    with pytest.raises(DeployError):
        load(data)


def test_dockerfile_is_relative_to_context():
    data = valid()
    service = data["services"]["web"]
    del service["image"]
    service["build"] = {"context": "backend", "dockerfile": "Dockerfile"}
    assert load(data)["services"]["web"]["build"]["dockerfile"] == "Dockerfile"


@pytest.mark.parametrize("field", ["subdomain", "service", "route", "secret", "environment"])
def test_identifiers_reject_trailing_newline(field):
    data = valid()
    if field == "subdomain":
        data["app"]["subdomain"] = "demo\n"
    elif field == "service":
        data["services"]["web\n"] = data["services"].pop("web")
        data["routes"][0]["service"] = "web\n"
    elif field == "route":
        data["routes"][0]["path"] = "/health\n"
    elif field == "secret":
        data["services"]["web"]["secretRefs"] = {"TOKEN": "token\n"}
    else:
        data["services"]["web"]["env"] = {"NAME\n": "foo"}
    with pytest.raises(DeployError):
        load(data)


@pytest.mark.parametrize("kind", ["database", "worker"])
def test_non_web_service_kinds_report_unsupported(kind):
    data = valid()
    data["services"]["web"]["kind"] = kind
    with pytest.raises(DeployError) as raised:
        load(data)
    assert raised.value.code == "UNSUPPORTED_CAPABILITY"
    assert raised.value.path == "services.web.kind"


@pytest.mark.parametrize("placement,key", [("service", "egress"), ("root", "egress"), ("service", "customEgress")])
def test_nonempty_egress_reports_unsupported(placement, key):
    data = valid()
    target = data["services"]["web"] if placement == "service" else data
    target[key] = {"allow": ["https://example.invalid"]}
    with pytest.raises(DeployError) as raised:
        load(data)
    assert raised.value.code == "UNSUPPORTED_CAPABILITY"


@pytest.mark.parametrize("timings", [{"intervalSeconds": 2}, {"intervalSeconds": 5, "timeoutSeconds": 6}])
def test_healthcheck_timeout_cannot_exceed_interval(timings):
    data = valid()
    data["services"]["web"]["healthcheck"].update(timings)
    with pytest.raises(DeployError) as raised:
        load(data)
    assert raised.value.code == "INVALID_MANIFEST"
    assert raised.value.path == "services.web.healthcheck.timeoutSeconds"
