import tailwindcss from '@tailwindcss/vite';
import { tanstackRouter } from '@tanstack/router-plugin/vite';
import react from '@vitejs/plugin-react';
import { msw } from 'msw/vite';
import { copyFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig, loadEnv, type Plugin } from 'vite';
import { devProxy } from './src/lib/devProxy.ts';
import { parseEnv } from './src/lib/parseEnv.ts';

// 빌드할 때 환경변수를 검사한다. 틀린 값으로 만든 빌드는 배포한 뒤에야 빈 화면으로 드러난다.
// 화면을 그리기 전 검사(src/lib/env.ts)와 같은 값(config.env)·같은 기준(isProduction = import.meta.env.PROD)을 쓴다.
const checkEnv = (): Plugin => ({
  name: 'simai-check-env',
  apply: 'build',
  configResolved(config) {
    parseEnv(config.env, { production: config.isProduction });
  },
});

export default defineConfig(({ command, mode, isPreview }) => {
  const mockBuild = command === 'build' && mode === 'mock';
  process.env.VITE_MOCK_BUILD = mockBuild ? 'true' : 'false';
  if (mockBuild) {
    // 로컬 환경변수가 있더라도 목업 빌드는 실제 서버로 연결되지 않는다.
    Object.assign(process.env, {
      VITE_MOCK_BUILD: 'true',
      VITE_API_MOCK: 'true',
      VITE_API_REAL: '',
      VITE_API_BASE_URL: 'https://mock.simai.invalid',
      VITE_STOMP_URL: 'wss://mock.simai.invalid/ws',
    });
  }
  const base =
    mockBuild || (isPreview && mode === 'mock') ? process.env.MOCK_BASE || '/simai-mock/' : '/';
  if (mockBuild && !/^\/(?:[a-zA-Z0-9._~-]+\/)*$/.test(base)) {
    throw new Error('MOCK_BASE 는 / 또는 /simai-mock/ 같은 절대 경로여야 한다.');
  }
  return {
    base,
    build: mockBuild || (isPreview && mode === 'mock') ? { outDir: 'dist-mock' } : undefined,
    plugins: [
      checkEnv(),
      ...(mockBuild
        ? [
            {
              name: 'simai-mock-assets',
              enforce: 'pre',
              transform(code, id) {
                if (!id.replaceAll('\\', '/').endsWith('/features/demo/demoMap.ts')) return;
                const pattern =
                  /import\.meta\.glob<string>\('\.\/assets\/\*\.glb',\s*\{[\s\S]*?\}\)/;
                if (!pattern.test(code)) throw new Error('시연 GLB glob 을 찾지 못했다.');
                return code.replace(
                  pattern,
                  "{ mock: new URL('../viewer3d/dev-harness/fixtures/scene-demo.gltf', import.meta.url).href }",
                );
              },
              closeBundle() {
                copyFileSync(resolve('dist-mock/index.html'), resolve('dist-mock/404.html'));
              },
            } satisfies Plugin,
          ]
        : []),
      tailwindcss(),
      // react() 보다 앞에 둔다. 라우트 파일을 먼저 변환해야 코드 분할이 된다.
      tanstackRouter({ target: 'react', autoCodeSplitting: true }),
      react(),
      // 개발 서버와 연습 빌드에만 msw 워커를 넣는다.
      msw(),
    ],
    resolve: {
      // @ 경로는 tsconfig.json 의 paths 하나에서만 정한다.
      tsconfigPaths: true,
    },
    // 개발 서버·preview 가 /api 를 실서버로 넘긴다(src/lib/devProxy.ts). preview 는 server.proxy 를 물려받는다.
    server:
      command === 'serve' && mode !== 'mock'
        ? { proxy: devProxy(loadEnv(mode, import.meta.dirname, ''), { preview: isPreview }) }
        : undefined,
  };
});
