# 13. 뷰어

Three.js가 유일한 렌더러다. 배경·사람·구역·경보(확장 시 장비·PPE)가 같은 장면과 깊이 버퍼에 있어야 책상 뒤 사람의 가림이 맞기 때문이다. 뷰어는 경보 경로가 아니며, 뷰어가 멈춰도 경보는 계속된다.

## 13.1 장면 구성

| 요소 | 표현 |
|---|---|
| 정적 배경 | 정적 3D glTF(MapAnything + Open3D TSDF + `texrecon` 디지털 트윈, 확장 시 Unreal에서 조명을 구운 glTF). 사진 텍스처가 있는 배경 메시는 이중 조명·톤매핑 왜곡을 막기 위해 비조명 재질(`MeshBasicMaterial`, `SRGBColorSpace`, `toneMapped: false`, `DoubleSide`, 캔버스 `NoToneMapping`)로 그리고, 정합 미확인(`DIMMED`) 때는 알파 정렬이 깨지지 않게 불투명(깊이 가림 유지) 상태에서 명도(0.65)·채도(0.25)만 낮춘다(무텍스처 메시는 기존 반투명 0.35 + 법선 계산). 표면 출처 지도(`surface_source_map_urls`)가 있으면 최근접 샘플링으로 관측(`2`)·추정 평면(`3`)·보간(`1`)·미관측(`0`, 어둡게 + 사선 빗금)을 구분 표시하며, 선택 후처리(`N8AO` + `SMAA`, 기본 꺼짐)를 지원한다(`docs/design/viewer-background-quality.md`). 정적 3D 전에는 BEV 평면도 바닥 이미지. 2D 개요 화면은 BEV 평면도 |
| 사람 | 기본·폴백은 Tier 0 골격의 관절 사이를 잇는 캡슐/막대 아바타(InstancedMesh). 시연 범위(EXP-02)에서는 `/topic/avatar`(`avatar_frame`)로 받는 MHR 리깅 메시(Three.js SkinnedMesh)와 포즈 스트림을 점진 도입한다(1단계 MHR 리깅 메시 + 포즈 스트림·상하의 대표색, 2단계 관측 픽셀 UV 텍스처 누적, 3DGS 미사용). 사람의 존재·root 위치·접지·관측 상태·안전 판단 근거는 계속 `/topic/world`(Tier 0)가 정본이다 |
| 작업면·정적 객체 | 등록 OBB 를 반투명 상자로, 상판 가장자리(R2·R3 기준, 7.5)를 선으로 |
| 구역 | 반투명 입체(다각형 + 높이 범위), 유형별 색 |
| 경보 | 대상 아바타 강조 + 이벤트 목록, 같은 사람의 여러 규칙은 한 묶음 |
| 장비·불확실 점유 | 확장(부록 C) |

## 13.2 상태 표시 규칙

- 관측 상태: OBSERVED 실선 / PREDICTED 반투명 + 예측 궤적 / UNKNOWN 마지막 위치에 물음표 표식(아바타 제거하지 않음).
- 관절 가시성이 거짓인 관절은 흐리게 처리한다. 아바타는 접지가 확인되면 발을 ground_anchor_xyz에 맞추고 발아래 지지면(`ground_anchor_xyz`)에 방사형 접지 그림자(`GroundShadowLayer`)를 그리며, 접지가 미확인이면 그림자를 숨기고 pelvis_xyz 기준으로 배치하며 “접지 미확인”을 표시한다(5.2, `docs/design/viewer-background-quality.md`).
- MHR 메시 아바타 구동(`/topic/avatar`): 사람의 존재·위치·접지·관측 상태·ID 신뢰도는 `/topic/world` 만 따른다. root 위치는 지연 없이 `/topic/world` 최신 위치(접지 VALID 면 발을 `ground_anchor_xyz` 에 맞춤)를 쓰고, 관절 회전(축각 라디안, null 은 bind 회전)만 재생 지연 버퍼 D = 250 ms 로 두 표본 사이를 slerp 한다(외삽 금지, Three.js 좌표 변환은 어댑터 한 곳에서만 수행). 외형은 텍스처가 없으면 기본 재질(상·하의 대표색)과 아바타 전용 소형 PMREM 환경맵으로 그리고, `albedo.png`(`SRGBColorSpace`, `LinearFilter`)와 `surface_source.png`(`NoColorSpace`, `NearestFilter`, 밉맵 없음)가 있으면 사람·버전별로 두 장이 모두 준비될 때 깜빡임 없이 교체해 부호별로 표시한다: `2`(관측) 원본 색, `1`(보간) 채도 0.45·명도 0.85 감쇠, `0`·`128`(미관측) 기본 재질, `130`(얼굴 관측)은 `face_policy` 에 따라 `OBSERVED` 원본·`BLURRED` 얼굴 관측 텍셀 한정 3×3 가우시안+채도 완화·`EXCLUDED` 기본 재질로 그린다(20.3, `docs/design/avatar-pose-stream.md`). 아래 5조건 중 하나라도 해당하면 그 사람은 즉시 캡슐 아바타로 되돌아가고, 해소되면 다음 포즈부터 다시 메시로 그린다(세부는 `docs/design/avatar-pose-stream.md`·`contracts/`).
  1. 새 포즈 수신이 0.5 s 넘게 끊김
  2. HMR 촬영 시각 지연(`world_state` 최신 시각과의 차)이 500 ms 초과(상태 표시줄에 데이터 연령과 함께 표시)
  3. 트랙이 `IDENTITY_UNCERTAIN` 이거나 포즈의 `identity_revision` 이 트랙과 다름
  4. 포즈의 `calibration_version` 이 `world_state` 와 다름
  5. 아바타 자산이 없거나 내려받기·무결성·관절 수 검증 실패
- “확인 필요” 이벤트는 이유(`confidence_reasons`, 7.1)와 불확실성 범위를 함께 보여준다.
- 데이터 연령(staleness)과 System Health를 화면 상단에 항상 표시한다. EC2 연결(STOMP 하트비트)이 끊기면 연결 끊김을 표시한다.
- 현재 Scene 버전과 배경 자산 버전이 다르면 경고한다. 관리자가 새 Scene 버전을 활성화하면 서버 재기동 없이 `/topic/health`(`viewer_health.scene.mismatch`)에 1초 안으로 반영되어 열린 뷰어에 버전 불일치 경고가 뜨고, 새로고침하면 새 배경 자산을 로드한다(`docs/design/scene-version-from-job.md`).
- 시험 프로파일(시험용 `rule_version`, 7.4)이 적용돼 있으면 화면에 표시한다.
- 워치 채널 아이콘과 “추적 인원 vs 활성 워치 수” 표시는 이동(v1.3) → 부록 C.

## 13.3 PPE 표현 (확장)

이동(v1.3) → 부록 C.

## 13.4 화면

- 실시간 관제: 3D 장면, 이벤트 목록·상세(VLM 상향 제안 승인·거절 `EscalationControl` 포함, 5.4·12.6), 상태 표시줄
- 구역 편집: 다각형 그리기, 높이·유효 시간·요구 조건 지정
- 정적 객체·작업면 등록: 단상·책상 등을 OBB 로 그리고 상판 높이를 입력한다(7.5). 정적 3D OBB 에서 뽑은 책상 상판 후보를 확인하는 보조 기능은 선택
- 레이아웃 편집(F-02): 개구부·비계·장비 배치, 보고서 생성 요청
- 최소 리플레이(M3): 시각 슬라이더로 world_track_state·risk_event 재생, 기록 없는 구간은 “데이터 없음”. 이벤트 점프·재생 속도·버전 재현은 확장(부록 C)
- 시뮬레이션·What-if(확장, 부록 C): PLANNED·WHAT_IF 결과 표시. 실시간 화면과 시각적으로 구분한다.

## 13.5 성능 목표

\[MVP 캡슐 아바타\] 아바타 10명에서 30 FPS 이상(G5). 발 위치 정확도는 뷰어가 아니라 Tier 0 지표로 잰다: 지지 표면이 확인된 프레임에서 관측 발목 Z − 지지면 Z(G5). \[EXP-02 HMR(시연 범위)\] 동일 입력에서 별도 추론 지연·메시 갱신 빈도·VRAM·렌더링 FPS를 E-GATE-0\~3 으로 검증하며(지연·끊김 시 캡슐 아바타 폴백, 13.2·20.6), 기본 G5 목표를 자동 변경하지 않는다.

## 13.6 비상 화면

Three.js 뷰어가 멈췄을 때를 대비해, World 상태만으로 그리는 가벼운 2D 평면 상태 화면을 둔다. BEV 평면도 2D 개요 화면(4.1)과 같은 컴포넌트로 만든다. 무거운 렌더러 장애가 상황 인식 장애가 되지 않게 하기 위함이다.
