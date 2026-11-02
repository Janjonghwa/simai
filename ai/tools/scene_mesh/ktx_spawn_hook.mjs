/**
 * Node.js `child_process.spawn` 프리로드 훅 (`NODE_OPTIONS="--import ..."`).
 *
 * Windows 에서 `@gltf-transform/cli@4.5.1` 은 `which` 로 `ktx.cmd` 존재를 확인한 뒤
 * `child_process.spawn("ktx", args)` (shell: false) 를 호출하므로,
 * `cmd === "ktx"` 호출을 `node ktx_cli.mjs ...args` 로 연결한다.
 */

import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ktxCliPath = path.join(__dirname, 'ktx_cli.mjs');

const origSpawn = childProcess.spawn;

function patchedSpawn(command, args, options) {
  if (command === 'ktx' || command === 'ktx.exe' || command === 'ktx.cmd') {
    const actualArgs = Array.isArray(args) ? args : [];
    const actualOpts = Array.isArray(args) ? options : args;
    return origSpawn.call(this, process.execPath, [ktxCliPath, ...actualArgs], actualOpts);
  }
  return origSpawn.apply(this, arguments);
}

childProcess.spawn = patchedSpawn;
syncBuiltinESMExports();
