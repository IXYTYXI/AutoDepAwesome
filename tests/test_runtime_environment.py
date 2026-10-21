import pytest

from awesome_deploy.config import Settings
from awesome_deploy.errors import DeployError
from awesome_deploy.runtime_environment import EnvironmentFiles


def test_environment_file_is_private_and_deleted_after_failure(tmp_path):
    files = EnvironmentFiles(Settings(state_dir=tmp_path))
    path = None
    with pytest.raises(RuntimeError):
        with files.create('job-one', {'KEY': 'not-logged-value'}) as path:
            assert path.read_text() == 'KEY=not-logged-value\n'
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
            raise RuntimeError('simulated runtime failure')
    assert not path.exists()


def test_worker_cleanup_removes_only_private_generated_files(tmp_path):
    files = EnvironmentFiles(Settings(state_dir=tmp_path))
    directory = files.directory()
    stale = directory / 'job-one-abcdefgh'
    stale.write_text('secret fixture')
    stale.chmod(0o600)
    unrelated = directory / 'operator-file'
    unrelated.write_text('retain')
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    (directory / 'job-two-abcdefgh').symlink_to(outside)
    with pytest.raises(DeployError):
        files.cleanup()
    assert outside.read_text() == 'untouched'
    assert unrelated.exists()
    (directory / 'job-two-abcdefgh').unlink()
    files.cleanup()
    assert not stale.exists()


def test_environment_directory_symlink_is_rejected(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    (tmp_path / 'runtime-env').symlink_to(other)
    with pytest.raises(DeployError):
        EnvironmentFiles(Settings(state_dir=tmp_path)).directory()
    assert list(other.iterdir()) == []


def test_invalid_environment_does_not_create_file(tmp_path):
    files = EnvironmentFiles(Settings(state_dir=tmp_path))
    for environment in ({'KEY':'one\nTWO=bad'}, {'bad name':'x'}, {'KEY':'\0'}):
        with pytest.raises(DeployError):
            with files.create('job-one', environment):
                pytest.fail('unsafe environment accepted')
    assert not (tmp_path / 'runtime-env').exists()
