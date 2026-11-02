#!/usr/bin/env node
/**
 * Khronos `ktx` CLI 호환 래퍼 (`@gltf-transform/cli@4.5.1` 의 `uastc` / `etc1s` 명령용).
 *
 * Windows 노트북에 KTX-Software 네이티브 바이너리(`ktx.exe`)가 전역 설치되어 있지 않을 때,
 * WebAssembly로 컴파일된 공식 `libktx`(`ris-ktx2`)와 `sharp`를 사용해
 * `ktx --version` 및 `ktx create ... <src> <dst>` 호출을 처리한다.
 */

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

globalThis.require = createRequire(import.meta.url);
globalThis.__filename = fileURLToPath(import.meta.url);
globalThis.__dirname = path.dirname(globalThis.__filename);

const args = process.argv.slice(2);

if (args.includes('--version') || args.includes('-v')) {
  process.stdout.write('ktx version: 4.4.0\n');
  process.exit(0);
}

if (args[0] !== 'create') {
  process.stderr.write(`Unsupported ktx subcommand: ${args[0] ?? ''}\n`);
  process.exit(1);
}

function parseCreateArgs(argv) {
  const opts = {
    encode: 'uastc',
    uastcQuality: 0,
    uastcRdo: false,
    uastcRdoLambda: 1.0,
    zstd: 0,
    qlevel: 128,
    clevel: 1,
    srgb: true,
    positional: [],
  };

  const flagsWithValue = new Set([
    '--encode',
    '--uastc-quality',
    '--uastc-rdo-l',
    '--uastc-rdo-d',
    '--uastc-rdo-b',
    '--uastc-rdo-s',
    '--zstd',
    '--qlevel',
    '--clevel',
    '--max-endpoints',
    '--max-selectors',
    '--endpoint-rdo-threshold',
    '--selector-rdo-threshold',
    '--assign-tf',
    '--assign-primaries',
    '--format',
    '--threads',
    '--mipmap-filter',
    '--mipmap-filter-scale',
  ]);

  for (let i = 1; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--uastc-rdo') {
      opts.uastcRdo = true;
    } else if (
      arg === '--generate-mipmap' ||
      arg === '--uastc-rdo-m' ||
      arg === '--no-endpoint-rdo' ||
      arg === '--no-selector-rdo'
    ) {
      // boolean flags without value
    } else if (flagsWithValue.has(arg)) {
      const val = argv[++i];
      if (arg === '--encode') opts.encode = val;
      else if (arg === '--uastc-quality') opts.uastcQuality = Number(val);
      else if (arg === '--uastc-rdo-l') opts.uastcRdoLambda = Number(val);
      else if (arg === '--zstd') opts.zstd = Number(val);
      else if (arg === '--qlevel') opts.qlevel = Number(val);
      else if (arg === '--clevel') opts.clevel = Number(val);
      else if (arg === '--assign-tf') opts.srgb = val === 'srgb';
      else if (arg === '--format') opts.srgb = String(val).includes('SRGB');
    } else if (!arg.startsWith('-')) {
      opts.positional.push(arg);
    }
  }

  return opts;
}

async function resolveDependencies() {
  const sharpHint = process.env.SIMAI_SHARP_MODULE_PATH;
  const risKtx2Hint = process.env.SIMAI_RIS_KTX2_MODULE_PATH;

  let sharpMod;
  if (sharpHint && fs.existsSync(sharpHint)) {
    const req = createRequire(sharpHint);
    sharpMod = req('sharp');
  } else {
    const req = createRequire(import.meta.url);
    sharpMod = req('sharp');
  }

  let ktxMod;
  if (risKtx2Hint && fs.existsSync(risKtx2Hint)) {
    ktxMod = await import(pathToFileURL(risKtx2Hint).href);
  } else {
    ktxMod = await import('ris-ktx2');
  }

  return { sharp: sharpMod.default ?? sharpMod, ktx: ktxMod };
}

async function main() {
  const opts = parseCreateArgs(args);
  if (opts.positional.length < 2) {
    process.stderr.write('ktx create requires <srcPath> <dstPath>\n');
    process.exit(1);
  }
  const srcPath = opts.positional[opts.positional.length - 2];
  const dstPath = opts.positional[opts.positional.length - 1];

  const { sharp, ktx } = await resolveDependencies();
  const factory = new ktx.Ktx2Factory();
  await factory.initializeAsync();

  const srcBytes = fs.readFileSync(srcPath);
  const { data, info } = await sharp(srcBytes)
    .ensureAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });

  const vkFormat = opts.srgb ? ktx.VkFormat.R8G8B8A8_SRGB : ktx.VkFormat.R8G8B8A8_UNORM;
  const tex = factory.create({
    baseWidth: info.width,
    baseHeight: info.height,
    vkFormat,
    numLevels: 1,
  });

  try {
    const setErr = tex.setImageFromMemory(0, 0, 0, data);
    if (setErr !== ktx.KtxErrorCode.SUCCESS) {
      throw new Error(`setImageFromMemory failed with code ${setErr}`);
    }

    const isUastc = opts.encode === 'uastc';
    if (isUastc) {
      const levelMap = {
        0: ktx.KtxUastcFlags.LEVEL_FASTEST,
        1: ktx.KtxUastcFlags.LEVEL_FASTER,
        2: ktx.KtxUastcFlags.LEVEL_DEFAULT,
        3: ktx.KtxUastcFlags.LEVEL_SLOWER,
        4: ktx.KtxUastcFlags.LEVEL_VERYSLOW,
      };
      const uastcFlags = levelMap[opts.uastcQuality] ?? ktx.KtxUastcFlags.LEVEL_FASTEST;
      const compErr = tex.compressBasis({
        uastc: true,
        uastcFlags,
        uastcRDO: opts.uastcRdo,
        uastcRDOQualityScalar: opts.uastcRdoLambda,
      });
      if (compErr !== ktx.KtxErrorCode.SUCCESS) {
        throw new Error(`compressBasis (UASTC) failed with code ${compErr}`);
      }
      const zstdLevel = opts.zstd > 0 ? Math.min(opts.zstd, 15) : 3;
      const defErr = tex.deflateZstd(zstdLevel);
      if (defErr !== ktx.KtxErrorCode.SUCCESS) {
        throw new Error(`deflateZstd failed with code ${defErr}`);
      }
    } else {
      const compErr = tex.compressBasis({
        uastc: false,
        qualityLevel: opts.qlevel,
        compressionLevel: Math.min(opts.clevel, 1),
      });
      if (compErr !== ktx.KtxErrorCode.SUCCESS) {
        throw new Error(`compressBasis (ETC1S) failed with code ${compErr}`);
      }
    }

    const outView = tex.writeToMemory();
    const outBuf = Buffer.from(outView.buffer, outView.byteOffset, outView.byteLength);
    fs.mkdirSync(path.dirname(dstPath), { recursive: true });
    fs.writeFileSync(dstPath, outBuf);
  } finally {
    tex.delete();
  }
}

main().catch((err) => {
  process.stderr.write(`${err?.stack || err}\n`);
  process.exit(1);
});
