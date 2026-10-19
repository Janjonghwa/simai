import tailwindcss from '@tailwindcss/vite';
import { tanstackRouter } from '@tanstack/router-plugin/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [
    tailwindcss(),
    // react() 보다 앞에 둔다. 라우트 파일을 먼저 변환해야 코드 분할이 된다.
    tanstackRouter({ target: 'react', autoCodeSplitting: true }),
    react(),
  ],
  resolve: {
    // @ 경로는 tsconfig.json 의 paths 하나에서만 정한다.
    tsconfigPaths: true,
  },
});
