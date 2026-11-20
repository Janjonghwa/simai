# S15P31A101-824 설정·목업 데이터

## 배경·근거
- 2026-11-20 목업 UX 최종 결정(⑤ 설정·목업 데이터)을 실제 코드에 반영한다.
- 공통 틀(① S15P31A101-820)의 버튼 3단 위계(`flow`·`action`·`quiet`), 상단 바 스피커 단추(`SoundButton`), 위험·주의 2단 표시(`SeverityBadge`)를 그대로 사용한다.

## 정한 수치·구현 결정
1. **설정 화면(`SettingsPage.tsx`, `GpuSettingsSection.tsx`)**
   - GPU 설정은 기본 접힘 `<details>` 「고급 설정 · GPU」로 감싸 평소 화면 복잡도를 낮추고, 요약 줄에 `공급자 · 선호 GPU · 자동 정지`를 보인다. 펼친 양식 하단에는 `sticky` 저장 바(`되돌리기` quiet, `저장` flow)를 둔다.
   - 경보 소리 구역은 ①의 스피커 단추와 같은 `alarmSound`(`localStorage` `simai.sound`) 상태를 구독한다. 상태 우선순위도 `SoundButton`과 맞춰 `muted → 경보음 꺼짐`, `!ready → 경보음 차단됨`(+ `소리 켜기` 단추로 `alarmSound.unlock()` 호출), `ready → 경보음 켜짐`으로 표시한다.
2. **목업 시연 경보 시나리오(`mocks/startBackend.ts`)**
   - `VITE_MOCK_BUILD === 'true'` 목업 빌드에서는 초기 경보 시드를 비우고(`seed: []`), 작업 시작 시 아래 순서로 경보를 송출한다:
     - `5초`: `R9` 안전모 미착용(`CAUTION`, `w-0015` 이영희)
     - `12초`: `R5` 위험구역 접근(`CAUTION`, `w-0012` 김철수, `z-04`)
     - `14초`: `R2` 추락 위험(가장자리)(`CAUTION`, `w-0012` 김철수, `z-04`)
     - `20초`: `R1` 위험구역 진입(`CRITICAL`) + `R4` 낙상 의심(`CRITICAL`, `w-0012` 김철수, `z-04`)
   - 작업 시작·종료 감지는 `mocks/work.ts`(④ 범위)를 수정하지 않고 MSW `network.events.on('response:mocked')`에서 2xx `POST /api/work-units/:id/(start|end)` 응답을 관찰해 수행한다.
3. **11월 목업 데이터(`mocks/history.ts`, `mocks/zones.ts`)**
   - `history.ts`의 `stableEvent`에서 `HIGH`를 `CAUTION`으로 변환해 지난 이벤트가 위험·주의 2단으로만 기록되도록 하고, `countEvents`의 `HIGH` 집계 칸에 `HIGH`·`CAUTION`을 합쳐 달력 칸(11-18 위험 9·주의 27, 11-19 위험 17·주의 51)과 월간 요약(작업 2일 · 위험 26건 · 주의 78건) 숫자가 리플레이 이벤트와 일치하도록 했다.
   - `zones.ts`는 타 티켓 단위 시험이 `MOCK_ZONES[0].name === '개구부 구역'`에 의존하므로 기본 상수는 유지하고, `createZonesMock({ mockBuild })`(목업 빌드 기본 `true`)에서만 `z-04` 이름을 `단상 개구부`로 시드한다.

## 버린 대안
- `lib/alarmSound.ts`에 `test()` 메서드를 추가하는 대안: 본 티켓 범위 밖 파일이므로 버리고 기존 `alarmSound.unlock()`을 사용해 소리 차단을 해제한다.
- `mocks/work.ts`에 `onRunChange` 훅을 추가하는 대안: ④ 범위 파일과 충돌을 피하기 위해 버리고 MSW `network.events` 응답 관찰로 분리했다.
- `MOCK_ZONES` 상수 이름을 직접 바꾸는 대안: ②·③·④의 기존 구역 테스트(`ZonesPage.test.tsx`, `ZoneList.test.tsx` 등)가 깨지므로 `createZonesMock`의 `mockBuild` 분기로 국한했다.

## 남은 위험
- `SettingsPage`의 `소리 켜기`는 `alarmSound.unlock()`으로 오디오 컨텍스트만 깨우며 시험 비프음은 내지 않는다(시연에서 시험음이 필요하면 추후 `lib/alarmSound.ts`에서 추가 가능).
- 목업 빌드 시연 경보의 14초 `R2`는 사용자 결정(12~14초 주의)에 맞춰 `CAUTION`으로 두어 20초 `R1`+`R4` 전까지 경보음·빨간 배너가 뜨지 않도록 했다.
- 달력 화면의 `주의` 문구·노란 점 색상 자체는 ④ 티켓이 머지된 뒤 최종 반영되며, 본 티켓에서는 `event_counts` 숫자와 지난 이벤트 `CAUTION` 데이터 정합을 보장한다.
