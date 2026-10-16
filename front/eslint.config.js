import config from '@simai/eslint-config';
import globals from 'globals';

// 루트 수준 설정 파일(이 파일, .prettierrc.mjs, packages/*의 설정)만 검사한다. 앱은 각자 eslint.config.js를 둔다.
/** @type {import("eslint").Linter.Config[]} */
export default [
  {
    ignores: ['apps/**', '**/dist/**', '**/.turbo/**'],
  },
  ...config,
  {
    languageOptions: {
      globals: {
        ...globals.node,
      },
    },
  },
];
