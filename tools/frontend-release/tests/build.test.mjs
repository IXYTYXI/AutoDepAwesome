import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, symlink, rename } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import vm from 'node:vm';

const source = 'function releaseExample(input) { const localNumber = input + 7; return localNumber * 3; } globalThis.result = releaseExample(2);';
const hash = data => createHash('sha256').update(data).digest('hex');
async function fixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), 'awesome-protected-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const input = path.join(root, 'src/awesome_deploy/static');
  const output = path.join(root, 'dist/protected-static');
  await mkdir(input, { recursive: true });
  for (const [file, contents] of Object.entries({ 'app.js': source, 'index.html': '<script src="/assets/app.js"></script>', 'app.css': 'body { color: black; }', 'guide.txt': 'public help' })) {
    await writeFile(path.join(input, file), contents);
  }
  return { root, input, output };
}
async function build(root) {
  const { buildProtectedStatic } = await import('../build.mjs');
  return buildProtectedStatic(root);
}
async function verify(root) {
  const { verifyProtectedStatic } = await import('../build.mjs');
  return verifyProtectedStatic(root);
}

test('builds only static assets, records SHA-256, preserves sources and executes without string code generation', async t => {
  const f = await fixture(t);
  await build(f.root);
  assert.equal(await readFile(path.join(f.input, 'app.js'), 'utf8'), source);
  assert.deepEqual((await readdir(f.output)).sort(), ['app.css', 'app.js', 'guide.txt', 'index.html', 'manifest.json']);
  const code = await readFile(path.join(f.output, 'app.js'), 'utf8');
  assert.notEqual(code, source);
  const context = vm.createContext({}, { codeGeneration: { strings: false, wasm: false } });
  vm.runInContext(code, context, { timeout: 1000 });
  assert.equal(context.result, 27);
  assert.equal(vm.runInContext('releaseExample(1)', context), 24);
  const manifest = JSON.parse(await readFile(path.join(f.output, 'manifest.json'), 'utf8'));
  assert.equal(manifest.tool.version, '5.9.0');
  assert.equal(manifest.files['app.js'].sourceSha256, hash(source));
  assert.equal(manifest.files['app.js'].sha256, hash(code));
  assert.equal(manifest.files['app.js'].bytes, Buffer.byteLength(code));
  assert.equal(await readFile(path.join(f.output, 'index.html'), 'utf8'), await readFile(path.join(f.input, 'index.html'), 'utf8'));
});

test('does not overwrite an existing output directory', async t => {
  const f = await fixture(t);
  await mkdir(f.output, { recursive: true });
  await writeFile(path.join(f.output, 'keep.txt'), 'keep');
  await assert.rejects(build(f.root), /already exists/);
  assert.equal(await readFile(path.join(f.output, 'keep.txt'), 'utf8'), 'keep');
});

test('rejects a symlinked output parent before writing to source', async t => {
  const f = await fixture(t);
  await symlink(f.input, path.join(f.root, 'dist'));
  await assert.rejects(build(f.root), /symbolic link/);
  assert.equal(await readFile(path.join(f.input, 'app.js'), 'utf8'), source);
  assert.equal((await readdir(f.input)).length, 4);
});

test('rejects a symlinked output directory and preserves its target', async t => {
  const f = await fixture(t);
  await mkdir(path.dirname(f.output));
  await symlink(f.input, f.output);
  await assert.rejects(build(f.root), /symbolic link/);
  assert.equal(await readFile(path.join(f.input, 'app.js'), 'utf8'), source);
});

test('rejects source symlinks, hidden files and non-static files before creating output', async t => {
  for (const kind of ['symlink', '.env', 'backend.py', 'app.js.map']) {
    const f = await fixture(t);
    if (kind === 'symlink') await symlink(path.join(f.input, 'guide.txt'), path.join(f.input, 'linked.txt'));
    else await writeFile(path.join(f.input, kind), 'not public');
    await assert.rejects(build(f.root), /symbolic link|Unsupported static/);
    await assert.rejects(readdir(f.output), { code: 'ENOENT' });
  }
});

test('the same source and pinned configuration produce identical artifacts', async t => {
  const first = await fixture(t), second = await fixture(t);
  await build(first.root);
  await build(second.root);
  for (const file of await readdir(first.output)) {
    assert.deepEqual(await readFile(path.join(first.output, file)), await readFile(path.join(second.output, file)));
  }
});

test('verification rejects modified artifacts and extra unlisted files', async t => {
  const f = await fixture(t);
  await build(f.root);
  await verify(f.root);
  await writeFile(path.join(f.output, 'extra.js'), 'alert(1)');
  await assert.rejects(verify(f.root), /Artifact file list/);
  await rm(path.join(f.output, 'extra.js'));
  await writeFile(path.join(f.output, 'app.js'), 'alert(1)');
  await assert.rejects(verify(f.root), /Artifact digest/);
});

test('verification rejects a stale build when clear source has changed', async t => {
  const f = await fixture(t);
  await build(f.root);
  await writeFile(path.join(f.input, 'app.js'), 'globalThis.result = 99;');
  await assert.rejects(verify(f.root), /Source digest/);
});

test('a JavaScript transform failure creates no partial release', async t => {
  const f = await fixture(t);
  await writeFile(path.join(f.input, 'app.js'), 'function {');
  await assert.rejects(build(f.root));
  await assert.rejects(readdir(f.output), { code: 'ENOENT' });
});

test('rejects a symlinked input directory', async t => {
  const f = await fixture(t);
  await rename(f.input, f.input + '-original');
  await symlink(f.input + '-original', f.input);
  await assert.rejects(build(f.root), /symbolic link/);
  await assert.rejects(readdir(f.output), { code: 'ENOENT' });
});

test('CLI rejects an attempted output override before changing any source', async t => {
  const f = await fixture(t);
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('../build.mjs', import.meta.url)), '--output', f.input], { encoding: 'utf8' });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /accepts no paths or options/);
  assert.equal(await readFile(path.join(f.input, 'app.js'), 'utf8'), source);
});
