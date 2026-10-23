import { copyFile, mkdtemp, mkdir, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { projectRoot, verifyProtectedStatic } from './build.mjs';

if (process.argv.length !== 2) throw new Error('This command accepts no paths or options.');
const { output } = await verifyProtectedStatic();
const temporary = await mkdtemp(path.join(tmpdir(), 'awesome-protected-ui-'));
try {
  const tests = (await readdir(path.join(projectRoot, 'tests'))).filter(name => /^ui_.*\.test\.cjs$/.test(name)).sort();
  if (!tests.length) throw new Error('No existing UI tests found.');
  await mkdir(path.join(temporary, 'tests'));
  await mkdir(path.join(temporary, 'src/awesome_deploy'), { recursive: true });
  await symlink(output, path.join(temporary, 'src/awesome_deploy/static'));
  for (const name of tests) await copyFile(path.join(projectRoot, 'tests', name), path.join(temporary, 'tests', name));
  // The original tests are copied byte-for-byte. Their VM sandboxes additionally reject
  // eval/new Function, so a generated helper cannot silently require unsafe-eval.
  const guard = path.join(temporary, 'csp-guard.cjs');
  await writeFile(guard, "const vm = require('node:vm');\nconst original = vm.createContext;\nvm.createContext = (sandbox, options = {}) => original(sandbox, {...options, codeGeneration: {strings: false, wasm: false}});\n");
  console.log(`Verifying ${tests.length} unchanged UI test files against ${output}; VM string code generation disabled.`);
  const exitCode = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--require', guard, '--test', ...tests.map(name => path.join(temporary, 'tests', name))], {
      stdio: 'inherit', cwd: temporary,
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => signal ? reject(new Error(`UI tests interrupted: ${signal}`)) : resolve(code));
  });
  process.exitCode = exitCode;
} finally {
  await rm(temporary, { recursive: true, force: true });
}
