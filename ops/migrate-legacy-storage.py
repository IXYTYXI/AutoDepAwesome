#!/usr/bin/env python3
"""One-service maintenance migration. Default is read-only planning.

Run as the worker account, with API/worker stopped by the operator. This script
never changes service settings, snapshots, images, or source-volume contents.
Only flat ordinary files owned by runtime_user are accepted. The copy image must
be a reviewed immutable local image containing python3. Source volumes are kept.
"""

import argparse
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

from awesome_deploy.cli import load_settings
from awesome_deploy.errors import DeployError
from awesome_deploy.runtime import DeploymentBackend
from awesome_deploy.storage import ManagedStorage
from awesome_deploy.storage_migration import StorageMigrations
from awesome_deploy.store import Store


COPY = r'''
import hashlib,json,os,stat
def inventory(root):
 rows=[]
 for name in sorted(os.listdir(root)):
  path=root+'/'+name
  s=os.lstat(path)
  assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==os.getuid() and s.st_gid==os.getgid(), 'unsupported-source-shape'
  assert not s.st_mode & 0o7000, 'unsupported-permissions'
  with open(path,'rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
  rows.append([name,stat.S_IMODE(s.st_mode),s.st_uid,s.st_gid,s.st_size,digest])
 return rows
source=inventory('/source'); prior=inventory('/data')
assert sum(row[4] for row in source)<=50*1024*1024, 'source-too-large'
assert {r[0] for r in prior}<={r[0] for r in source}, 'unexpected-target-content'
for name,mode,uid,gid,size,digest in source:
 p='/source/'+name; target='/data/'+name
 if os.path.exists(target): os.chmod(target,0o600,follow_symlinks=False)
 fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
 with open(p,'rb') as src,os.fdopen(fd,'wb') as dst:
  while chunk:=src.read(1024*1024): dst.write(chunk)
  dst.flush();os.fchmod(dst.fileno(),mode);os.fsync(dst.fileno())
 s=os.stat(p,follow_symlinks=False);os.utime(target,ns=(s.st_atime_ns,s.st_mtime_ns),follow_symlinks=False)
target=inventory('/data');assert source==target, 'copy-verification-failed'
fd=os.open('/data',os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
digest=hashlib.sha256(json.dumps(source,sort_keys=True,separators=(',',':')).encode()).hexdigest()
print(json.dumps({'source_digest':digest,'target_digest':digest,'files':len(source),'bytes':sum(r[4] for r in source)}))
'''


def require(value, message):
    if not value:
        raise DeployError('STORAGE_MIGRATION_OPERATOR', message)


def operation(migrations, operation_id):
    with migrations._connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_migrations'").fetchone():
            return None
        row = db.execute('SELECT app_id FROM storage_migrations WHERE operation_id=?', (operation_id,)).fetchone()
        return next((item for item in migrations._rows(db, row['app_id'])
                     if item['operation_id'] == operation_id), None) if row else None


def validate_release(migrations, release_id, saved=None):
    with migrations._connection() as db:
        job = migrations._release(db, release_id)
        if saved:
            require(saved['release_id'] == release_id and migrations._release_digest(job) == saved['release_digest']
                    and job['execution'].get('storage_volumes', {}).get(saved['service']) == saved['source'],
                    'Migration release or immutable inputs changed')


def require_maintenance():
    for unit in ('awesome-api.service', 'awesome-worker.service'):
        output = subprocess.check_output(['systemctl', 'show', unit, '--property=LoadState,ActiveState,SubState'], text=True)
        state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        require(state == {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead'},
                'Stop the loaded API and worker services for this maintenance window')


def inspect_target(storage, app_id, service, spec, target):
    """Maintenance only: inspect the exact prepared target without admitting it."""
    storage.validate_reference(app_id, service, spec, target)
    return storage._record(app_id, service, spec, storage.quota.call('inspect', expected=target), target)


def copy_data(backend, storage, job, service, source, target, copy_image, operation_id):
    record = inspect_target(storage, job['app_id'], service,
                            job['snapshot']['manifest']['services'][service]['storage'], target)
    inventory = storage.inventory_record(job['app_id'], service,
                                         job['snapshot']['manifest']['services'][service]['storage'], target)
    require(not inventory.get('writers'), 'Target still has registered writers; stop and retire them before copy')
    release = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
    name = f'awesome-{release}-{service}'
    found = backend._docker(['container', 'ls', '-a', '--no-trunc', '--filter', f'name=^{name}$', '--format', '{{.ID}}']).split()
    require(not found, 'A previous migration copy container remains; inspect and retire it before retry')
    labels = {'awesome.managed': 'true', 'awesome.app': job['app_id'], 'awesome.job': release, 'awesome.service': service}
    args = ['container', 'create', '--name', name, '--read-only', '--network=none', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user', backend.settings.runtime_user,
            '--restart=no', '--memory=64m', '--memory-swap=64m', '--pids-limit=32',
            '--mount', f"type=volume,source={source['name']},destination=/source,readonly,volume-nocopy",
            '--mount', f"type=bind,source={record['path']},destination=/data,bind-propagation=rprivate",
            '--entrypoint=python3']
    for key, value in labels.items():
        args += ['--label', f'{key}={value}']
    identifier = backend._docker([*args, copy_image, '-I', '-B', '-c', COPY]).strip()
    require(re.fullmatch(r'[0-9a-f]{64}', identifier), 'Copy container ID is not exact')
    row = json.loads(backend._docker(['container', 'inspect', identifier]))[0]
    mounts = row.get('Mounts', [])
    require(row['Id'] == identifier and row['Name'] == '/' + name
            and all(row['Config']['Labels'].get(k) == v for k, v in labels.items())
            and len(mounts) == 2
            and any(m.get('Type') == 'volume' and m.get('Name') == source['name']
                    and m.get('Destination') == '/source' and m.get('RW') is False for m in mounts)
            and any(m.get('Type') == 'bind' and m.get('Source') == record['path']
                    and m.get('Destination') == '/data' and m.get('RW') is True for m in mounts),
            'Migration copy container identity or mounts changed')
    writer = {'container_id': identifier, 'container_name': name, 'app_id': job['app_id'],
              'release_id': release, 'service': service}
    # The ordinary startup guard rejects extra source mounts. This reviewed copy
    # path validates its two fixed mounts before using the same helper admission.
    with storage.quota.session('guard', items=[{'expected': target, 'writer': writer}]) as session:
        backend.quota_guard_fd = session.fd
        try:
            output = backend._docker(['container', 'start', '--attach', identifier], timeout=120)
            exited = json.loads(backend._docker(['container', 'inspect', identifier]))[0]
            require(exited['Id'] == identifier and exited['State']['Running'] is False
                    and exited['State']['ExitCode'] == 0, 'Migration copy did not exit successfully')
            result = json.loads(output)
            require(result['source_digest'] == result['target_digest'], 'Copy digests differ')
        finally:
            backend.quota_guard_fd = None
    backend._docker(['container', 'rm', identifier])
    require(not backend._docker(['container', 'ls', '-a', '--no-trunc', '--filter', f'id={identifier}', '--format', '{{.ID}}']).split(),
            'Copy container removal is unconfirmed')
    storage.retire(identifier)
    inspect_target(storage, job['app_id'], service, job['snapshot']['manifest']['services'][service]['storage'], target)
    return result


def migrate(settings, args, lock_fd):
    with sqlite3.connect(settings.database_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM jobs WHERE id=?', (args.release_id,)).fetchone()
        job = Store._job(row) if row else None
    require(job is not None and job['status'] == 'succeeded', 'An existing active release is required')
    services = job['snapshot']['manifest']['services']
    require(len(services) == 1 and args.service in services, 'This reviewed operator supports one-service applications only')
    spec = services[args.service]['storage']
    migrations = StorageMigrations(settings)
    saved = operation(migrations, args.operation_id)
    validate_release(migrations, job['id'], saved)
    source = saved['source'] if saved else job['execution']['storage_volumes'][args.service]
    legacy = DeploymentBackend(replace(settings, storage_backend='local'))
    backend = DeploymentBackend(replace(settings, storage_backend='xfs-project'))
    legacy.lock_fd = backend.lock_fd = lock_fd
    storage = ManagedStorage(backend._docker, backend.settings)
    ManagedStorage(legacy._docker).verify_legacy(job['app_id'], args.service, spec, source)
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', args.copy_image), 'Copy image must be an immutable local image ID')
    backend._docker(['image', 'inspect', args.copy_image, '--format', '{{.Id}}'])
    if not args.apply:
        print(json.dumps({'plan': True, 'release_id': job['id'], 'app_id': job['app_id'],
                          'service': args.service, 'source': source, 'migration_state': saved['state'] if saved else None,
                          'size_mib': 50, 'max_inodes': args.max_inodes}, sort_keys=True))
        return
    require_maintenance()
    store = Store(settings)
    require(saved is None or saved['state'] != 'rolled_back', 'A rolled-back operation cannot be silently resumed')
    if saved is None:
        target = storage.ensure(job['app_id'], args.service, {**spec, 'sizeMiB': 50, 'maxInodes': args.max_inodes},
                                operation_id='migration:' + args.operation_id + ':allocate')
        fresh = storage.inspect(job['app_id'], args.service, spec, target)
        require(fresh['allocated_bytes'] == 0 and fresh['used_inodes'] == 1,
                'An unassociated target must be empty before migration')
        migrations.prepare(job['id'], args.service, source, target, operation_id=args.operation_id)
        saved = operation(migrations, args.operation_id)
    require(saved['release_id'] == job['id'] and saved['service'] == args.service, 'Operation identity changed')
    target = saved['target']
    if saved['state'] == 'prepared':
        legacy.stop_for_storage_handoff(job)
        identifiers = legacy._docker(['ps', '-a', '--no-trunc', '--filter', 'volume=' + source['name'], '--format', '{{.ID}}']).split()
        expected_id = job['execution']['port_forwards'][args.service]['container_id']
        require(identifiers == [expected_id], 'Unexpected source volume references require operator review')
        legacy._confirm_stopped(expected_id)
        ManagedStorage(legacy._docker).verify_legacy(job['app_id'], args.service, spec, source)
        result = copy_data(backend, storage, job, args.service, source, target, args.copy_image, args.operation_id)
        migrations.commit(args.operation_id, source_digest=result['source_digest'], target_digest=result['target_digest'],
                          stopped_container_ids=identifiers)
        saved = operation(migrations, args.operation_id)
    # Once committed, remove only recorded old writers. New target writers are
    # recovered forward; the old source is never reused after startup intent.
    old_ids = saved['evidence']['stopped_container_ids']
    forwards = job['execution'].get('port_forwards', {})
    if forwards and all(value['container_id'] in old_ids for value in forwards.values()):
        for forward in forwards.values():
            legacy.port_forwarder.remove(forward['spec'], require_present=not forward['confirmed'])
        # Persist removal before removing the original container. A retry after
        # either boundary must never resurrect a stale RootlessKit target.
        store.checkpoint(job['id'], execution={'port_forwards': {}, 'started': []})
    for identifier in old_ids:
        found = backend._docker(['container', 'ls', '-a', '--no-trunc', '--filter', f'id={identifier}', '--format', '{{.ID}}']).split()
        if found:
            require(found == [identifier], 'Ambiguous original writer')
            legacy._confirm_stopped(identifier)
            backend._docker(['container', 'rm', identifier])
    job = store.get(job['id'])
    validate_release(migrations, job['id'], saved)
    backend.start(job, lambda delta: store.checkpoint(job['id'], execution=delta))
    job = store.get(job['id'])
    backend.health(job)
    backend.recover_active(job, lambda delta: store.checkpoint(job['id'], execution=delta))
    print(json.dumps({'migrated': True, 'release_id': job['id'], 'target': target, 'source_retained': True}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--service', default='web')
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--copy-image', required=True)
    parser.add_argument('--max-inodes', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', args.operation_id), 'Invalid operation ID')
    require(args.max_inodes > 1, 'Positive inode budget required')
    settings = load_settings(args.config)
    descriptor = os.open(settings.state_dir / 'worker.lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1, 'Unsafe worker lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        migrate(settings, args, descriptor)
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
