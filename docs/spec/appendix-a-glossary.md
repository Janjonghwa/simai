# 부록 A. 용어집

| 용어 | 정의 |
|---|---|
| World | 첫 CALIBRATED 때 교실 바닥·벽으로 정한 단일 미터 좌표계(오른손, Z-up, 바닥 Z=0) |
| World ID | 카메라 간에 연결된 한 사람·객체의 세션 단위 익명 번호 |
| Tier 0 | 실시간 안전 경로. 관측에서 World 상태까지, RunPod Tier 0 서비스에서 실행(v1.2), 위치의 정본(5장). Tier 1은 비동기 3D 인체 강화(SAM 3D Body 등), 안전 판단 권한 없음 |
| 게이트웨이 | 교실망에서 카메라 영상을 받아 촬영 시각을 붙여 RunPod로 중계하고 현장 경보를 울리는 노트북 |
| 삼각측량 | 2대 이상 카메라의 같은 관절 2D 위치로 3D 위치를 구하는 계산 |
| 재투영 오차 | 3D 점을 카메라 화면에 다시 투영했을 때 실제 검출 위치와의 픽셀 차이 |
| 번들 조정 | 카메라 자세와 3D 점을 함께 최적화해 재투영 오차를 줄이는 방법 |
| 게이지 고정 | 번들 조정 결과가 매번 통째로 이동·회전·축척되지 않도록 좌표계를 제약으로 묶는 것 |
| observation_gap | 전체 카메라 최신 촬영 시각 − 객체의 마지막 유효 관측 시각 |
| pipeline_lag | 판정 시각 − 전체 카메라 최신 촬영 시각 |
| staleness | gap + lag. 객체 정보의 총 경과 시간 |
| OBSERVED / PREDICTED / UNKNOWN | 객체 관측 상태(6.1) |
| NOMINAL / DEGRADED / UNSAFE_UNKNOWN | System Health(6.2) |
| VALID / CHANGING / UNKNOWN | Region Geometry 상태 |
| CANDIDATE / VALIDATING / COMMITTED / REJECTED | Scene 버전 생명주기 |
| UNCALIBRATED / PROVISIONAL / CALIBRATED / DRIFTED | 보정 상태(4.4) |
| RAISED / UPDATED / RESOLVED | RiskEvent 생명주기 |
| VERIFIED / UNVERIFIABLE | 해소 여부를 관측으로 확인할 수 있는지 |
| ACKNOWLEDGED | 운영자·워치가 경보를 확인했다는 속성(해소 아님) |
| severity / confidence | 위험 정도 / 증거 신뢰도. 서로 독립 |
| “확인 필요” | 신뢰도가 낮은 경보에 붙는 표시. 등급은 유지 |
| LIVE / PLANNED / REPLAY / WHAT_IF | Risk Engine 입력 출처. LIVE만 실제 알림 가능 |
| Visual Hull 후보 | 팽창 실루엣 원뿔의 교집합으로 만든 임시 형상 후보 |
| Temporary Collider | 일관성 검사를 통과한 임시 형상. 안전 판단에 사용 |
| UNKNOWN_OCCUPANCY | 형상을 신뢰할 수 없는 불확실 점유 영역(방 형상으로 제한) |
| BOUND / UNBOUND / LOST | 개인 알림 채널 상태(10.2) |
| WORN / NOT_WORN / UNKNOWN | PPE 상태(5.5) |
| 움직임 대조 | 워치 가속도와 카메라 트랙의 움직임을 비교해 워치를 사람에 연결하는 방법 |
| VLM | 비전-언어 모델. v1.2부터 대형 후보(Qwen3-VL, Cosmos 3 Reasoner) |
| RAG | 안전 수칙 문서를 검색해 인용하게 하는 방식 |
| TBM | 작업 전 안전점검회의 |
| MV3DT 방식 | 카메라별 추적을 공유 바닥 평면에서 전역 ID로 융합하는 방식 |
| HMR | 영상에서 사람의 3D 메시·자세를 복원하는 모델 |
| MHR | SAM 3D Body의 파라메트릭 바디 표현 |
| 3DGS | 3D Gaussian Splatting. 시각용 정적 장면 표현 |
| Scene | 정적 3D 장면의 한 버전. 자산 주소, 생성에 쓴 보정 버전, 상태(CANDIDATE\~REJECTED)로 구성 |
| Operational Geometry | 안전 판단에 쓰는 검증된 형상(보정으로 확정한 바닥 평면, 등록 정적 객체의 보수적 OBB, 위험 구역). 보기용 형상(glTF, 3DGS)과 구분 |
| 링 버퍼 | RunPod Tier 0 서비스가 카메라별 최근 영상을 메모리에 잠시 보관하는 순환 저장 공간. 이벤트 클립의 출처 |
| 관제 화면 경보 | MVP의 현장 경보. 관제 화면의 소리·점멸 알림(3.10) |
| outbox | 이벤트 저널에서 아직 EC2 ACK를 받지 않은 구간(acked_offset 이후). 별도 저장소가 아님 |
| 구성 묶음(config bundle) | RunPod Tier 0 판정 기준(구역·규칙·보정·형상)의 버전 묶음 |
| CURRENT / PENDING / UNVERIFIED / STALE | Config Sync 상태. UNVERIFIED는 연결 단절로 최신 여부를 모르는 상태, STALE은 불일치가 확인된 상태. 어느 쪽이든 판정은 계속 |
| DELAYED_ACTIVE | 재동기화 후 최종 상태가 여전히 활성으로 확인된 이벤트의 지연 알림 표시 |
| observed / inferred | 관절 출처. observed는 Tier 0 삼각측량, inferred는 Tier 1 HMR 보강. 위험 판단은 observed만 |
| z_validity | 사람 높이(Z)의 유효성. 지지 표면을 모르면 UNAVAILABLE이며 Z=0을 가정하지 않음 |
| ContactEstimator | 발목 정보로 발의 접지 여부와 지지 표면을 추정하는 모듈. 근거가 부족하면 support_surface = UNKNOWN |
| TENTATIVE / CONFIRMED / OCCLUDED / LOST | World Track의 관리 생명주기. 관측 상태와 별도 |
| IDENTITY_UNCERTAIN | 카메라 간 매칭이 애매해 같은 사람인지 확신할 수 없다는 표시. 합치지 않고 이 표시를 붙인다 |
| stream_epoch | 카메라 스트림 재연결마다 증가하는 번호. camera_id·frame_sequence와 함께 프레임을 식별(12.7) |
| pelvis_xyz / position_xy / ground_anchor_xyz | 삼각측량 골반 위치 / 구역 판정용 수평 위치 / 검증된 지지 표면 위 위치(모르면 비움). 정의는 5.2 |
| queue_wait_ms / frame_age_ms | 추론 대기열에서 기다린 시간 / 촬영부터 추론 시점까지 전체 경과 시간 |
| 원격 관제 장애 | EC2만 끊긴 상태의 노트북 표시. 판정과 현장 경보는 정상이고 PWA·관제 화면 전달만 지연 |
| GROUND_FOOTPRINT / VOLUME_3D | 구역 판정 유형. 바닥 다각형만 보는 구역 / 높이 범위까지 보는 입체 구역(8.2) |
| 이벤트 저널 | 모든 이벤트 전이를 추가만 하는 내구성 로그. 재시작 복구의 정본이며 min(acked_offset, last_applied_journal_offset) 이전만 정리. outbox는 acked_offset 이후 구간 |
| journal_offset / acked_offset | 저널 전체의 전역 단조 증가 위치 / EC2가 받았다고 확인한 마지막 저널 위치. 이벤트별 event_sequence와 다름 |
| site_ack | 노트북 현장 확인 버튼의 확인. EC2 단절 중에도 소리를 멈추고 저널에 기록 |
| BEV 평면도 | 카메라·정적 3D 를 위에서 본 화면으로 자동 생성한 간략한 2D 개요. 좌표·스케일 기준이 아니며 안전 판단에 쓰지 않음(수정 6) |
