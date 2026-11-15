# GPU 패널·설정 UX 손질 (S15P31A101-733)

## 개요

라이브 E2E UX 감사(`2026-11-15/findings.md` GPU 구역 #1~#18)에서 드러난 GPU 켜기 표·준비 중 정보창·끄기 확인 창·정보창 이름/단가/자동 정지·관리자 설정(`/settings` GPU 구역)의 문구·표시·조작 불편을 정리한다. 서버 계약은 그대로 두고 관리자 프론트(`front/apps/admin/src/features/gpu/`)에서 표시와 입력 방식을 다듬는다.

## 항목별 변경 전·후 문구 및 동작

### 1. GPU 켜기 표 (`GpuLaunchForm.tsx`, `GpuPanel.tsx`)

| 항목 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 기본 표시 행 | 카탈로그 16종을 모두 펼쳐 보여「없음」인 행이 대부분을 차지함 | 기본은 켤 수 있는 칸(「켤 수 있음」·「모름」이면서 가격 상한 안)이 하나라도 있는 행만 표시하고, 숨겨진 행이 있으면 표 머리에서 `전체 보기 (N종)` / `켤 수 있는 줄만 보기`로 토글 |
| 「추천」 배지 | 모든 지역이「없음」인 행에도 `recommended: true` 이면「추천」배지가 붙음 | `row.recommended && rowUsable(offers, row.gpu_type)` 일 때만「추천」배지 표시 |
| 지역 머리글 및 확인 문구 | `AP-JP-1`, `OC-AU-1`, `US-CA-2`, `EU-RO-1` 등 코드만 표시, 6열에서 가로 스크롤 발생 | 나라 이름 + 코드(`일본 AP-JP-1`, `호주 OC-AU-1`, `미국 US-CA-2`, `루마니아 EU-RO-1`, `한국 ap-northeast-2a` 등)로 표시하고 패널 너비를 `w-[min(880px,calc(100vw-32px))]`로 넓혀 6열 가로 스크롤 방지 |
| 등급·추천 배지 hover 설명 | `권장 미만`·`제한`·`위험`·`추천` 배지에 마우스를 올려도 설명이 없음 | 각 배지에 `title` 속성으로 `tierWarning(tier)` 및 `RECOMMENDED_TOOLTIP`(`권장 사양(80GB급 H100·H200 계열): 관제·세팅·AI 근거·아바타를 모두 실행합니다.`) 제공 |
| 예상 준비 시간 안내 | 표와 켜기 확인 창에 준비 소요 시간 안내가 없음 | 표 하단 및 켜기 확인 창에 `예상 준비 시간: 캐시 있으면 약 4분, 첫 부팅 20분 이상` 안내 표시 |

### 2. 준비 중 정보창 (`GpuInfoLines.tsx`) · 끄기 확인 창 (`GpuPanel.tsx`)

| 항목 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 준비 중 단계별 예상 남은 시간 | `켜는 중 → 가중치 → 엔진 → 준비 완료` 단계만 보이고 얼마나 남았는지 알 수 없음 | 단계 아래에 단계별 예상 남은 시간 표시: `켜는 중`(`예상 남은 시간: 캐시 있으면 약 4분 (첫 부팅 20분 이상)`), `가중치`(`예상 남은 시간: 캐시 있으면 약 3~4분 (첫 부팅 15~20분)`), `엔진`(`예상 남은 시간: 캐시 있으면 약 2~3분 (첫 빌드 10~15분)`) |
| 끄기 확인 창 안내 | `지금 켜져 있는 GPU 를 끌까요? 진행 중인 추론이 멈춥니다.` 만 표시 | 재기동 소요 시간(`다시 켜면 준비에 약 4분(첫 부팅은 20분 이상) 걸립니다.`)과 현재 세션 누적 비용(`이번 세션 비용: 약 $X.XX (경과 N분 · $Y.YY/h)`)을 함께 표시 |

### 3. GPU 이름 통일 · 정보창 보강 (`gpuStatus.ts`, `GpuInfoLines.tsx`)

| 항목 | 변경 전 | 변경 후 |
| --- | --- | --- |
| GPU 이름 표기 | 켜기 표는 `H100 SXM`, 정보창·패널은 `NVIDIA H100 80GB HBM3`, 설정은 원문 줄바꿈 문자열로 서로 다름 | `shortGpuName()`으로 모든 화면(켜기 표·정보창·패널·설정 선택 목록)에서 짧은 이름(`H100 SXM`, `H100 PCIe`, `RTX 4090` 등)으로 통일 |
| 단가(`$/h`) 및 자동 정지 남은 시간 | 정보창에 `경과 1분 미만 · 약 $0.03` 만 보이고 시간당 단가와 자동 정지까지 남은 시간이 없음 | GPU·지역 줄은 나라 이름 지역(`H100 SXM · 루마니아 EU-RO-1`)으로 표시하고, 경과·비용 줄에 단가(`경과 N분 · 약 $X.XX ($Y.YY/h)`)를 적으며, 아래 줄에 `자동 정지까지 약 N분`(작업 중이면 `자동 정지 보류 (작업 중)`, 0분 설정이면 `자동 정지 꺼짐`)을 함께 표시 |
| 「판정 지연」 hover 설명 | `판정 지연 N ms`에 설명이 없음 | CCTV 카메라 배지(`CctvPage.tsx` `CameraBadge`) 설명을 그대로 재사용하여 `title="판정 지연: 이 카메라가 찍은 뒤 위험 판정(Tier 0)까지 걸린 시간, 최근 5초 평균. 300 ms 미만 초록, 600 ms 이상 빨강"` 부여 |

### 4. 설정 (`/settings` GPU 구역, `GpuSettingsSection.tsx`)

| 항목 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 선호 GPU·선호 지역 입력 | `<Textarea>`에 `NVIDIA H100 80GB HBM3`·`AP-JP-1` 같은 공급자 원문 식별자를 한 줄에 하나씩 직접 입력 | 재고 표 카탈로그 목록(`<select>`)에서 골라 추가하고, 선택된 목록(`1. H100 SXM`, `1. 일본 AP-JP-1` 등)에서 `↑`·`↓`·`제거` 버튼으로 순서와 항목을 조정 |
| 적용 시점 및「유휴」기준 설명 | 언제 적용되는지,「유휴 분」이 무엇을 기준으로 재는지 설명이 없음 | 상단에 `다음 켜기부터 적용됩니다(자동 정지 시간은 켜져 있는 GPU 에도 바로 적용).`, 자동 정지 칸 아래에 `유휴 기준: 관제(작업)·세팅·녹화 업로드·분석 작업과 관리자 화면 조작이 모두 없는 시간(준비 시간 제외).` 안내 추가 |
| 버전·템플릿 ID 배치 | 카드 머리글에 `버전 N`, 본문에 `RunPod 템플릿 ID`(또는 AWS 시작 템플릿·서브넷·보안 그룹)가 항상 노출됨 | 접힌 `<details>`(`자세히`) 안으로 버전과 템플릿/네트워크 ID 입력 칸을 이동 |
| 준비 안 된 공급자 | `AWS (준비 안 됨)`을 선택할 수 있어 잘못 저장할 위험이 있음 | `!p.configured` 인 공급자 `<option>`에 `disabled`를 걸어 선택할 수 없게 막음 |

## 기각한 대안

- **설정 화면 진입 시 `/api/gpu/offers`를 매번 자동 호출하여 목록 채우기**: `/api/gpu/offers`는 공급자 실시간 재고 조회를 수반하므로 설정 페이지 열 때마다 호출하면 불필요한 외부 API 지연이 생기고, 설정 전용 테스트(`SettingsPage.test.tsx`)에서도 추가 모킹이 필요해진다. 서버 `RunPodProvider`·`AwsGpuProvider`와 동일한 기본 카탈로그 상수(`RUNPOD_GPU_CATALOG`, `AWS_GPU_CATALOG`, `RUNPOD_REGION_CATALOG`, `AWS_REGION_CATALOG`)를 기본 목록으로 쓰고, 이미 조회된 `/api/gpu/offers` 캐시(`useCachedGpuOffers`)가 있으면 병합하는 방식을 택했다.
- **「자세히」 구역을 조건부 마운트(`useState(false)`)로 숨기기**: DOM에서 아예 제거하면 브라우저 기본 찾기나 통합 테스트(`SettingsPage.test.tsx`의 `getByText('버전 2')`)에서 접근하기 어렵다. 표준 `<details>`/`<summary>자세히</summary>` 요소를 사용하여 기본 접힘 상태를 유지하면서 접근성과 기존 테스트 호환성을 모두 지켰다.

## 코드 · 테스트 위치

- 코드
  - `front/apps/admin/src/features/gpu/gpuStatus.ts`
  - `front/apps/admin/src/features/gpu/types.ts`
  - `front/apps/admin/src/features/gpu/useGpu.ts`
  - `front/apps/admin/src/features/gpu/GpuLaunchForm.tsx`
  - `front/apps/admin/src/features/gpu/GpuInfoLines.tsx`
  - `front/apps/admin/src/features/gpu/GpuPanel.tsx`
  - `front/apps/admin/src/features/gpu/GpuSettingsSection.tsx`
- 테스트
  - `front/apps/admin/src/__tests__/features/gpu/gpuStatus.test.ts`
  - `front/apps/admin/src/__tests__/features/gpu/GpuIndicator.test.tsx`
  - `front/apps/admin/src/__tests__/features/gpu/GpuSettingsSection.test.tsx`
