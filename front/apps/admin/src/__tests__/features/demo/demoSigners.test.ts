import { demoWorkerSignatures } from '@/features/demo/demoSigners';
import { expect, it } from 'vitest';

it('방금 직접 서명한 작업자는 자동으로 다시 추가하지 않는다', async () => {
  expect(await demoWorkerSignatures([], '추창만')).toEqual([]);
});
