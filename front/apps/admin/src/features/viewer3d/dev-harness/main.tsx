// 개발 서버에서만 여는 Spatial UI 목업 진입점. 운영 index.html·라우터와 무관하며 운영 빌드에 들어가지 않는다.
import '@/features/viewer3d/dev-harness/mock.css';
import '@/index.css';

import { MockApp } from '@/features/viewer3d/dev-harness/MockApp';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MockApp />
  </StrictMode>,
);
