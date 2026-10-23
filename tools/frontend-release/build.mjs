import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { lstat, mkdir, open, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import JavaScriptObfuscator from 'javascript-obfuscator';

export const projectRoot = fileURLToPath(new URL('../../', import.meta.url));
const packageInfo = JSON.parse(await readFile(new URL('./package.json', import.meta.url), 'utf8'));
const extensions = new Set(['.js', '.css', '.html', '.txt', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.ico', '.woff', '.woff2']);
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

export const obfuscatorOptions = Object.freeze({
  target: 'browser-no-eval',
  compact: true,
  identifierNamesGenerator: 'mangled-shuffled',
  renameGlobals: false,
  renameProperties: false,
  controlFlowFlattening: false,
  deadCodeInjection: false,
  debugProtection: false,
  debugProtectionInterval: 0,
  selfDefending: false,
  disableConsoleOutput: false,
  sourceMap: false,
  simplify: true,
  numbersToExpressions: false,
  splitStrings: false,
  stringArray: true,
  stringArrayThreshold: 0.25,
  stringArrayEncoding: [],
  stringArrayRotate: false,
  stringArrayShuffle: true,
  stringArrayIndexShift: false,
  stringArrayCallsTransform: false,
  stringArrayWrappersCount: 0,
  stringArrayWrappersChainedCalls: false,
  transformObjectKeys: false,
  unicodeEscapeSequence: false,
  log: false,
  seed: 279849,
});

async function inspect(relative, root, { missing = false } = {}) {
  let current = root;
  for (const part of relative.split('/')) {
    current = path.join(current, part);
    let info;
    try { info = await lstat(current); }
    catch (error) {
      if (missing && error.code === 'ENOENT') return null;
      throw error;
    }
    if (info.isSymbolicLink()) throw new Error(`Refusing symbolic link: ${current}`);
    if (!info.isDirectory()) throw new Error(`Not a directory: ${current}`);
  }
  return lstat(current);
}

async function collect(directory, prefix = '', allowManifest = false) {
  const files = [];
  for (const entry of (await readdir(directory)).sort()) {
    const relative = prefix + entry;
    const filename = path.join(directory, entry);
    const info = await lstat(filename);
    if (info.isSymbolicLink()) throw new Error(`Refusing symbolic link: ${relative}`);
    if (entry.startsWith('.')) throw new Error(`Unsupported static entry: ${relative}`);
    if (info.isDirectory()) {
      files.push(...await collect(filename, relative + '/', allowManifest));
      continue;
    }
    if (!info.isFile() || info.nlink !== 1 || (!extensions.has(path.extname(entry))
        && !(allowManifest && relative === 'manifest.json'))) {
      throw new Error(`Unsupported static entry: ${relative}`);
    }
    const handle = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const before = await handle.stat();
      if (!before.isFile() || before.ino !== info.ino || before.dev !== info.dev) {
        throw new Error(`Static input changed while reading: ${relative}`);
      }
      const bytes = await handle.readFile();
      const after = await handle.stat();
      if (before.size !== after.size || before.mtimeMs !== after.mtimeMs || bytes.length !== before.size) {
        throw new Error(`Static input changed while reading: ${relative}`);
      }
      files.push({ relative, bytes });
    } finally { await handle.close(); }
  }
  return files;
}

export async function buildProtectedStatic(root = projectRoot) {
  if (JavaScriptObfuscator.version !== packageInfo.devDependencies['javascript-obfuscator']) {
    throw new Error('Installed obfuscator differs from the pinned version; run npm ci --ignore-scripts.');
  }
  root = await realpath(root);
  const input = path.join(root, 'src/awesome_deploy/static');
  const output = path.join(root, 'dist/protected-static');
  await inspect('src/awesome_deploy/static', root);
  const existing = await inspect('dist/protected-static', root, { missing: true });
  if (existing) throw new Error('Output already exists; preserve or explicitly remove the previous protected-static release first.');
  const assets = await collect(input);
  if (!assets.length) throw new Error('Static input is empty.');
  const manifest = {
    format: 1,
    tool: { name: 'javascript-obfuscator', version: packageInfo.devDependencies['javascript-obfuscator'] },
    source: 'src/awesome_deploy/static',
    options: obfuscatorOptions,
    files: {},
  };
  const outputs = [];
  for (const { relative, bytes } of assets) {
    let protectedBytes = bytes;
    if (relative.endsWith('.js')) {
      const code = JavaScriptObfuscator.obfuscate(bytes.toString('utf8'), {
        ...obfuscatorOptions,
        identifiersPrefix: 'adp_' + sha256(relative).slice(0, 12) + '_',
      }).getObfuscatedCode();
      new vm.Script(code, { filename: relative });
      if (/sourceMappingURL\s*=/.test(code)) throw new Error(`Unexpected source map reference: ${relative}`);
      protectedBytes = Buffer.from(code + '\n');
    }
    manifest.files[relative] = {
      sourceSha256: sha256(bytes), sha256: sha256(protectedBytes),
      sourceBytes: bytes.length, bytes: protectedBytes.length,
      transformed: relative.endsWith('.js'),
    };
    outputs.push({ relative, bytes: protectedBytes });
  }
  // No caller-controlled output path and no recursive deletion of an old release.
  await inspect('dist', root, { missing: true });
  await mkdir(path.dirname(output), { recursive: true });
  await inspect('dist', root);
  await mkdir(output, { mode: 0o755 }); // Exclusive: EEXIST also protects a concurrent build.
  const created = await lstat(output);
  try {
    for (const { relative, bytes } of outputs) {
      const destination = path.join(output, relative);
      await mkdir(path.dirname(destination), { recursive: true });
      await writeFile(destination, bytes, { flag: 'wx', mode: 0o644 });
    }
    await writeFile(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n', { flag: 'wx' });
  } catch (error) {
    const now = await lstat(output).catch(() => null);
    if (now?.ino === created.ino && now?.dev === created.dev && now.isDirectory()) {
      await rm(output, { recursive: true });
    }
    throw error;
  }
  return { output, manifest };
}

export async function verifyProtectedStatic(root = projectRoot) {
  root = await realpath(root);
  await inspect('src/awesome_deploy/static', root);
  await inspect('dist/protected-static', root);
  const sourceFiles = await collect(path.join(root, 'src/awesome_deploy/static'));
  const output = path.join(root, 'dist/protected-static');
  const assets = await collect(output, '', true);
  const manifestAsset = assets.find(file => file.relative === 'manifest.json');
  if (!manifestAsset) throw new Error('Artifact manifest is missing.');
  const manifest = JSON.parse(manifestAsset.bytes.toString('utf8'));
  if (manifest.format !== 1 || manifest.tool?.name !== 'javascript-obfuscator'
      || manifest.tool.version !== packageInfo.devDependencies['javascript-obfuscator']
      || JSON.stringify(manifest.options) !== JSON.stringify(obfuscatorOptions)
      || !manifest.files || typeof manifest.files !== 'object' || Array.isArray(manifest.files)) {
    throw new Error('Artifact manifest does not match this release configuration.');
  }
  const names = Object.keys(manifest.files).sort();
  if (JSON.stringify(names) !== JSON.stringify(assets.filter(file => file.relative !== 'manifest.json').map(file => file.relative).sort())
      || JSON.stringify(names) !== JSON.stringify(sourceFiles.map(file => file.relative).sort())) {
    throw new Error('Artifact file list differs from the manifest or current source.');
  }
  for (const file of assets.filter(file => file.relative !== 'manifest.json')) {
    const item = manifest.files[file.relative];
    if (item?.sha256 !== sha256(file.bytes) || item.bytes !== file.bytes.length) {
      throw new Error(`Artifact digest mismatch: ${file.relative}`);
    }
  }
  for (const file of sourceFiles) {
    const item = manifest.files[file.relative];
    if (item.sourceSha256 !== sha256(file.bytes) || item.sourceBytes !== file.bytes.length) {
      throw new Error(`Source digest changed; rebuild the release: ${file.relative}`);
    }
  }
  return { output, manifest };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 2) throw new Error('This command accepts no paths or options; input and output locations are fixed.');
    const { output, manifest } = await buildProtectedStatic();
    const files = Object.values(manifest.files);
    console.log(JSON.stringify({ output, files: files.length,
      javascriptFiles: files.filter(file => file.transformed).length,
      sourceBytes: files.reduce((sum, file) => sum + file.sourceBytes, 0),
      outputBytes: files.reduce((sum, file) => sum + file.bytes, 0) }, null, 2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
