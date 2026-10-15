# S15P31A101

## 폴더 구조

| 경로 | 설명 |
| --- | --- |
| `front/` | 프론트엔드 (React + Three.js 뷰어, 반장 PWA) |
| `server/` | 백엔드 (Spring Boot) |
| `ai/` | AI (노트북 중계, RunPod Tier 0, 분석 워커) |
| `deploy/` | 배포 설정 |
| `docs/` | 프로젝트 문서 |

## 브랜치 전략

- `master` : 배포 브랜치
- `dev` : 개발 통합 브랜치
- 작업 브랜치는 `dev` 에서 따서 작업하고, MR 로 `dev` 에 merge 한다.
