/* global process, console */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

function files(dir, prefix = '') {
  return readdirSync(join(dir, prefix), { withFileTypes: true }).flatMap((entry) => {
    const path = join(prefix, entry.name);
    return entry.isDirectory() ? files(dir, path) : [path];
  });
}
const production = files('dist');
const mock = files('dist-mock');
assert(
  !production.some((name) => /mockServiceWorker|startBackend|_virtual_msw|stompBroker/i.test(name)),
);
assert(mock.includes('mockServiceWorker.js'));
assert(mock.includes('404.html'));
const base = process.env.MOCK_BASE || '/simai-mock/';
const index = readFileSync('dist-mock/index.html', 'utf8');
const js = mock
  .filter((name) => name.endsWith('.js'))
  .map((name) => readFileSync(join('dist-mock', name), 'utf8'))
  .join('\n');
assert(index.includes(base + 'assets/'));
assert(js.includes(base + 'mockServiceWorker.js'));
assert(js.includes('https://mock.simai.invalid'));
assert(js.includes('wss://mock.simai.invalid/ws'));
const prodJs = production
  .filter((name) => name.endsWith('.js'))
  .map((name) => readFileSync(join('dist', name), 'utf8'))
  .join('\n');
assert(!prodJs.includes('simaiMock'));
assert(!prodJs.includes('Mock Service Worker'));
assert.equal(
  readFileSync('dist-mock/index.html', 'utf8'),
  readFileSync('dist-mock/404.html', 'utf8'),
);
assert(!mock.some((name) => /\.glb$/i.test(name)));
assert(mock.some((name) => /scene-demo.*\.gltf$/i.test(name)));
for (const dir of ['dist', 'dist-mock']) {
  const list = files(dir);
  console.log(
    `${dir}: ${list.length}개, ${list.reduce((sum, name) => sum + statSync(join(dir, name)).size, 0)} bytes`,
  );
  for (const name of list.filter((name) =>
    /\.(?:glb|gltf|png|jpe?g|webp|webm|mp4|svg)$/i.test(name),
  )) {
    console.log(`  ${name}: ${statSync(join(dir, name)).size} bytes`);
  }
}
