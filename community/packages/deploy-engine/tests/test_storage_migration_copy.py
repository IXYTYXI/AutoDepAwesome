import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / 'ops' / 'migrate-legacy-storage.py'


def copy_program(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    code = runpy.run_path(str(SCRIPT))['COPY'].replace('/source', str(source)).replace('/data', str(target))
    return source, target, code


def test_copy_verifies_content_modes_and_supports_interrupted_partial_copy(tmp_path):
    source, target, code = copy_program(tmp_path)
    (source / 'sqlite.db').write_bytes(b'database bytes')
    (source / 'sqlite.db').chmod(0o640)
    (source / '.empty').touch()
    (target / 'sqlite.db').write_bytes(b'partial')
    output = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, check=True)
    result = json.loads(output.stdout)
    assert result['source_digest'] == result['target_digest']
    assert result['files'] == 2 and result['bytes'] == len(b'database bytes')
    assert (target / 'sqlite.db').read_bytes() == b'database bytes'
    assert (target / 'sqlite.db').stat().st_mode & 0o777 == 0o640
    (target / 'sqlite.db').chmod(0o444)
    subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, check=True)
    assert (target / 'sqlite.db').stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('shape', ['directory', 'symlink', 'hardlink', 'setuid', 'extra-target'])
def test_copy_rejects_unreviewed_file_shapes(tmp_path, shape):
    source, target, code = copy_program(tmp_path)
    (source / 'data').write_bytes(b'original')
    if shape == 'directory':
        (source / 'directory').mkdir()
    elif shape == 'symlink':
        (source / 'symlink').symlink_to('data')
    elif shape == 'hardlink':
        (source / 'linked').hardlink_to(source / 'data')
    elif shape == 'setuid':
        (source / 'data').chmod(0o4644)
        if not (source / 'data').stat().st_mode & 0o4000:
            pytest.skip('Local filesystem does not retain setuid; Linux acceptance must verify this case')
    else:
        (target / 'unexpected').write_bytes(b'other data')
    output = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True)
    assert output.returncode != 0
    assert (source / 'data').read_bytes() == b'original'
    assert not (target / 'data').exists()
