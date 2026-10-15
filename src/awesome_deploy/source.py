"""Acquire one pinned Git commit and export a bounded, metadata-free build tree."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import time
from urllib.parse import urlsplit

from .config import Settings, validate_git_proxy_settings
from .errors import DeployError
from .manifest import MAX_MANIFEST_BYTES, load_manifest

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_OUTPUT_LIMIT = 4 * 1024 * 1024


def validate_repository(url: str, settings: Settings) -> str:
    """Accept allowlisted HTTPS GitHub/GitLab repository URLs without credentials."""
    error = DeployError("INVALID_REPOSITORY", "Repository must be an allowlisted HTTPS Git URL.", "repository",
                        "Use https://github.com/owner/repository or an allowed GitLab repository URL.")
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise error
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in settings.git_hosts
                or parsed.username is not None or parsed.password is not None or parsed.port is not None
                or parsed.query or parsed.fragment or ":" in parsed.netloc):
            raise error
    except ValueError as exc:
        raise error from exc
    # Percent encodings, shell syntax, dot components and a trailing slash are rejected.
    parts = parsed.path.split("/")[1:]
    if len(parts) < 2 or (parsed.hostname.lower() == "github.com" and len(parts) != 2):
        raise error
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}", part) or part in {".", ".."}:
            raise error
    if parts[-1] == ".git" or parts[-1].endswith(".git.git"):
        raise error
    path = "/".join(parts)
    if not path.endswith(".git"):
        path += ".git"
    return f"https://{parsed.hostname.lower()}/{path}"


def validate_ref(ref: str) -> str:
    """Accept a literal branch/tag/full commit, never a Git revision expression."""
    if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref)
            or ".." in ref or "//" in ref or ref.endswith(("/", "."))
            or any(p.startswith(".") or p.endswith(".lock") for p in ref.split("/"))):
        raise DeployError("INVALID_REF", "Ref must be a literal branch, tag or full commit hash.", "ref")
    return ref


def _size_within(path: Path, limit: int) -> None:
    total = count = 0
    if not path.exists():
        return
    for root, dirs, filenames in os.walk(path, followlinks=False):
        for name in dirs + filenames:
            item = Path(root) / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise DeployError("UNSAFE_SOURCE", "Symbolic links are not supported in source state.")
            count += 1
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            if total > limit or count > 100000:
                raise DeployError("SOURCE_TOO_LARGE", "Repository exceeds its source size or file-count budget.")


def _bounded_run(args, *, cwd, env, timeout, budget_path=None, budget_bytes=None, lock_fd=None):
    """No shell; drain both pipes, cap output, kill the process group on limits."""
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            pass_fds=(lock_fd,) if lock_fd is not None else ())
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    start, next_budget_check = time.monotonic(), 0.0
    try:
        while selector.get_map() or proc.poll() is None:
            now = time.monotonic()
            if now - start > timeout:
                raise DeployError("GIT_TIMEOUT", "Git operation exceeded the configured timeout.")
            if budget_path and now >= next_budget_check:
                _size_within(budget_path, budget_bytes)
                next_budget_check = now + .25
            for key, _ in selector.select(.1):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if len(output["stdout"]) + len(output["stderr"]) > _OUTPUT_LIMIT:
                    raise DeployError("GIT_OUTPUT_LIMIT", "Git operation exceeded its output budget.")
        if budget_path:
            _size_within(budget_path, budget_bytes)
        return subprocess.CompletedProcess(args, proc.wait(), bytes(output["stdout"]), bytes(output["stderr"]))
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


class GitSource:
    """Snapshot provider. runner is injectable for isolated transport tests only.

    runner(args, cwd=..., env=..., timeout=..., budget_path=..., budget_bytes=..., lock_fd=...)
    returns a CompletedProcess. Production always uses the bounded local Git runner;
    repository URLs can never opt into a different transport or local file access.
    """
    def __init__(self, settings: Settings, runner=None):
        try:
            validate_git_proxy_settings(settings)
        except ValueError as exc:
            raise DeployError("INVALID_CONFIG", "Git 代理配置无效。") from exc
        self.settings = settings
        self.runner = runner or _bounded_run
        self.lock_fd = None

    def _git(self, args, git_dir: Path, *, check=True):
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(git_dir.parent / "empty-home"),
            "XDG_CONFIG_HOME": str(git_dir.parent / "empty-home"),
            "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1", "GIT_OPTIONAL_LOCKS": "0",
        }
        if self.settings.git_askpass:
            env["GIT_ASKPASS"] = self.settings.git_askpass
        else:
            env["GIT_ASKPASS"] = "/usr/bin/false"
        options = {
            "core.hooksPath": "/dev/null", "core.attributesFile": "/dev/null", "core.askPass": env["GIT_ASKPASS"],
            "credential.helper": "", "protocol.allow": "never", "protocol.https.allow": "always",
            "protocol.file.allow": "never", "http.followRedirects": "false", "fetch.recurseSubmodules": "false",
            "http.sslVerify": "true",
            "submodule.recurse": "false", "transfer.fsckObjects": "true", "fetch.fsckObjects": "true",
            "core.protectNTFS": "true", "core.protectHFS": "true",
        }
        command = ["git"]
        for key, value in options.items():
            command.extend(["-c", f"{key}={value}"])
        # Only platform-owned network commands receive the operator transport setting.
        # HTTPS-proxy TLS verification retains libcurl's default; Git has no proxySSLVerify setting.
        command_index = 0
        if args and args[0] == "--git-dir":
            command_index = 2
        elif args and args[0].startswith("--git-dir="):
            command_index = 1
        if self.settings.git_proxy_url is not None and args[command_index:command_index + 1] in (
            ["fetch"], ["ls-remote"],
        ):
            env["AWESOME_DEPLOY_GIT_PROXY_URL"] = self.settings.git_proxy_url
            command.append("--config-env=http.proxy=AWESOME_DEPLOY_GIT_PROXY_URL")
        command.extend(args)
        try:
            result = self.runner(command, cwd=git_dir.parent, env=env, timeout=self.settings.git_timeout,
                                 budget_path=git_dir, budget_bytes=self.settings.repo_size_limit_mib * 1024 * 1024,
                                 lock_fd=self.lock_fd)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeployError("GIT_FAILED", "Git could not complete the source operation.", "repository",
                              "Check repository access and the configured operator credentials.") from exc
        if check and result.returncode != 0:
            # Never surface raw Git output: credential helpers and remote servers can echo secrets.
            raise DeployError("GIT_FAILED", "Git could not complete the source operation.", "repository",
                              "Check repository URL, ref, and configured operator credentials.")
        if len(result.stdout or b"") + len(result.stderr or b"") > _OUTPUT_LIMIT:
            raise DeployError("GIT_OUTPUT_LIMIT", "Git operation exceeded its output budget.")
        return result

    @staticmethod
    def _safe_directory(path: Path):
        for component in [path, *path.parents]:
            if component.is_symlink():
                raise DeployError("UNSAFE_SOURCE", "Source state cannot traverse symbolic links.")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def prepare(self, job: dict, checkpoint) -> dict:
        inputs = job.get("input", job)
        repository = validate_repository(inputs["repository"], self.settings)
        ref = validate_ref(inputs["ref"])
        job_id = job["id"]
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            raise DeployError("INVALID_JOB", "Job identifier is not safe for source state.")
        # Resolve configured roots once; job identifiers cannot influence their parents.
        base = self.settings.state_dir.absolute()
        git_dir = base / "git" / job_id
        source = base / "sources" / job_id
        self._safe_directory(git_dir)
        self._safe_directory(source.parent)
        execution = job.get("execution", {})
        commit = execution.get("commit")
        if commit is not None and (not isinstance(commit, str) or not _SHA.fullmatch(commit)):
            raise DeployError("INVALID_SNAPSHOT", "Persisted commit is not a full commit hash.")
        if not (git_dir / "HEAD").exists():
            self._git(["init", "--bare", "--template=", str(git_dir)], git_dir)
        prefix = ["--git-dir", str(git_dir)]
        if commit:
            available = self._git([*prefix, "cat-file", "-e", commit + "^{commit}"], git_dir, check=False)
            if available.returncode != 0:
                self._git([*prefix, "fetch", "--no-tags", "--depth=1", "--no-recurse-submodules", repository, commit], git_dir)
        else:
            self._git([*prefix, "fetch", "--no-tags", "--depth=1", "--no-recurse-submodules", repository, ref], git_dir)
            raw = self._git([*prefix, "rev-parse", "--verify", "FETCH_HEAD^{commit}"], git_dir).stdout
            try:
                commit = raw.decode("ascii").strip()
            except (UnicodeDecodeError, AttributeError) as exc:
                raise DeployError("INVALID_SNAPSHOT", "Git returned an invalid commit hash.") from exc
            if not _SHA.fullmatch(commit):
                raise DeployError("INVALID_SNAPSHOT", "Git returned an invalid commit hash.")
            checkpoint({"commit": commit})
        # All work below follows durable pinning; retries can only fetch this exact commit.
        tree = self._git([*prefix, "ls-tree", "-r", "-z", commit], git_dir).stdout
        for entry in tree.split(b"\0"):
            if entry.startswith(b"160000 "):
                raise DeployError("UNSUPPORTED_CAPABILITY", "Git submodules are not supported in phase one.", "repository")
            if entry.startswith(b"120000 "):
                raise DeployError("UNSAFE_SOURCE", "Source symbolic links are not supported.", "repository")
        _size_within(git_dir, self.settings.repo_size_limit_mib * 1024 * 1024)
        archive_path = git_dir / "source.tar"
        staging = source.parent / (job_id + ".partial")
        try:
            self._git([*prefix, "archive", "--format=tar", f"--output={archive_path}", commit], git_dir)
            self._safe_directory(staging)
            shutil.rmtree(staging)
            staging.mkdir(mode=0o700)
            self._extract(archive_path, staging)
            manifest_path = staging / "awesome.yaml"
            if not manifest_path.is_file():
                raise DeployError("INVALID_MANIFEST", "Repository root must contain an awesome.yaml file.",
                                  "awesome.yaml", reason="MANIFEST_MISSING")
            if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
                raise DeployError("INVALID_MANIFEST", "Manifest exceeds the 256 KiB size limit.",
                                  "awesome.yaml", reason="MANIFEST_TOO_LARGE")
            try:
                manifest = load_manifest(manifest_path.read_text(encoding="utf-8"), self.settings,
                                         previous=execution.get("previous_manifest"))
            except UnicodeError as exc:
                raise DeployError("INVALID_MANIFEST", "awesome.yaml must contain UTF-8 text.",
                                  "awesome.yaml", reason="MANIFEST_ENCODING") from exc
            if manifest["app"]["subdomain"] != inputs["subdomain"]:
                raise DeployError("SUBDOMAIN_MISMATCH", "Requested subdomain does not match awesome.yaml.", "app.subdomain")
            self._check_build_paths(staging, manifest)
            if source.is_symlink():
                raise DeployError("UNSAFE_SOURCE", "Source destination cannot be a symbolic link.")
            if source.exists():
                shutil.rmtree(source)
            staging.rename(source)
        except (tarfile.TarError, OSError) as exc:
            raise DeployError("UNSAFE_SOURCE", "Source archive could not be exported safely.") from exc
        finally:
            archive_path.unlink(missing_ok=True)
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging)
        return {"repository": repository, "commit": commit, "source_dir": str(source), "manifest": manifest}

    def _extract(self, archive_path: Path, target: Path):
        total = count = 0
        names = set()
        limit = self.settings.repo_size_limit_mib * 1024 * 1024
        with tarfile.open(archive_path, "r:") as archive:
            for item in archive:
                count += 1
                name = item.name.rstrip("/")
                parts = name.split("/")
                if (not name or PurePosixPath(name).is_absolute() or "\\" in name
                        or any(p in {"", ".", ".."} or p.casefold() in {".git", ".gitmodules"} for p in parts)
                        or any(ord(c) < 32 for c in name) or name in names
                        or not (item.isfile() or item.isdir())):
                    raise DeployError("UNSAFE_SOURCE", "Repository contains an unsafe path or unsupported file type.")
                names.add(name)
                total += item.size
                if total > limit or count > 100000:
                    raise DeployError("SOURCE_TOO_LARGE", "Repository exceeds its source size or file-count budget.")
                path = target.joinpath(*parts)
                if item.isdir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o755)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    with archive.extractfile(item) as reader, path.open("xb") as writer:
                        shutil.copyfileobj(reader, writer, length=64 * 1024)
                    path.chmod(0o755 if item.mode & 0o111 else 0o644)

    @staticmethod
    def _check_build_paths(source: Path, manifest: dict):
        for name, service in manifest["services"].items():
            if "build" not in service:
                continue
            build = service["build"]
            context = source / build["context"]
            dockerfile = context / build["dockerfile"]
            if (not context.is_dir() or not dockerfile.is_file() or not context.resolve().is_relative_to(source.resolve())
                    or not dockerfile.resolve().is_relative_to(context.resolve())):
                raise DeployError("INVALID_BUILD_PATH", "Build context or Dockerfile is missing or outside the source context.",
                                  f"services.{name}.build")
            for candidate in [context, dockerfile, *dockerfile.parents]:
                if candidate == source.parent:
                    break
                if candidate.is_symlink():
                    raise DeployError("INVALID_BUILD_PATH", "Build paths cannot traverse symbolic links.", f"services.{name}.build")
