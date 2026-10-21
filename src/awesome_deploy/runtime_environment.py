"""Short-lived private Docker env files, reclaimed under the worker lock."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .errors import DeployError


class EnvironmentFiles:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _unsafe():
        return DeployError('UNSAFE_RUNTIME_ENV', '运行环境临时目录不安全，无法传递密钥。')

    def _path(self):
        if self.settings.secret_backend == 'encrypted' and sys.platform == 'linux':
            # Production workers run on Linux. No persistent-disk fallback for a
            # missing tmpfs: an operator must repair the runtime mount first.
            mounts = Path('/proc/self/mountinfo').read_text().splitlines()
            if not any(line.split()[4] == '/dev/shm' and line.split(' - ', 1)[1].split()[0] == 'tmpfs'
                       for line in mounts if ' - ' in line):
                raise self._unsafe()
            identity = hashlib.sha256(str(self.settings.state_dir.absolute()).encode()).hexdigest()[:16]
            return Path('/dev/shm') / f'awesome-deploy-{os.getuid()}-{identity}'
        return self.settings.state_dir / 'runtime-env'

    def directory(self):
        path = self._path()
        try:
            for ancestor in reversed(path.parent.absolute().parents):
                if ancestor.is_symlink():
                    raise self._unsafe()
            if path.parent.is_symlink():
                raise self._unsafe()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o777 != 0o700):
                raise self._unsafe()
        except OSError:
            raise self._unsafe() from None
        return path

    @contextmanager
    def create(self, job_id, environment):
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job_id):
            raise self._unsafe()
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
               or not isinstance(value, str) or any(c in value for c in '\r\n\0')
               for key, value in environment.items()):
            raise DeployError('UNSUPPORTED_ENVIRONMENT', '运行环境变量必须是单行文本。')
        fd, name = tempfile.mkstemp(dir=self.directory(), prefix=job_id + '-')
        path = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                for key, value in environment.items():
                    stream.write(f'{key}={value}\n')
            yield path
        finally:
            path.unlink(missing_ok=True)

    def cleanup(self, job_id=None):
        path = self._path()
        if not path.exists() and not path.is_symlink():
            return
        directory = self.directory()
        if job_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job_id):
            raise self._unsafe()
        for path in directory.iterdir():
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}-[a-z0-9_]{8}', path.name):
                continue
            if job_id is not None and path.name.rsplit('-', 1)[0] != job_id:
                continue
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o777 != 0o600 or info.st_nlink != 1):
                raise self._unsafe()
            path.unlink()
