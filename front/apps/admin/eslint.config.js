import config from '@simai/eslint-config/react';

/** @type {import("eslint").Linter.Config[]} */
export default [
  {
    ignores: ['dist/**', 'src/routeTree.gen.ts'],
  },
  ...config,
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/__tests__/**'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [{ name: 'vitest', message: '테스트 코드는 src/__tests__ 에만 둔다.' }],
        },
      ],
    },
  },
];
