# 부록 B. 모델·도구·라이선스 목록

교육 프로젝트라 상업화 제약은 없지만 출처와 조건을 기록한다. “확인 필요”는 채택 전 저장소에서 라이선스 파일을 확인한다.

| 이름 | 용도 | 단계 | 라이선스 |
|---|---|---|---|
| YOLO26-pose (비교군 RTMO, 필요 시 RTMPose Halpe-26) | Tier 0 | MVP | 후보별 상이(MMPose 계열 Apache 2.0, Ultralytics YOLO는 AGPL-3.0) |
| MapAnything | 초기 보정 추정, 정적 3D | MVP | 코드 Apache 2.0. 가중치 `facebook/map-anything`은 CC-BY-NC 4.0, `facebook/map-anything-apache`는 Apache 2.0(선행 PoC에서 품질 크게 낮음) |
| MoGe-3 | 정적 3D 대조군 | 비교 | 코드 MIT, 체크포인트 확인 필요 |
| HumanCalib | 사람 기반 자동 보정 | MVP | 확인 필요 |
| CCVS | 카메라별 높이 검산 | MVP | 확인 필요 |
| DMMR | 사람 기반 내부 파라미터 추정(대안) | 대안 | 확인 필요 |
| ExtrCamCalib_PersonKeypoints | 운영 중 보정 감시(참고) | 참고 | 확인 필요 |
| Caliscope | 보정 대체 경로 | 대안 | 확인 필요 |
| Qwen3-VL / Cosmos 3 Reasoner (G7에서 하나 선택) | VLM | MVP | Qwen3-VL 확인 필요 / Cosmos 3: OpenMDW 1.1 |
| SAM 3D Body (Fast 변형) | MHR 인체 메시/포즈·관측 기반 외형 갱신 실험(EXP-02) | 선행 실험·확장 후보 | Fast 구현 코드 MIT(yangtiming/Fast-SAM-3D-Body), 가중치는 원본 `facebook/sam-3d-body-*`의 SAM License(HF 승인 필요) |
| MHR | SAM 3D Body 바디 표현 | 확장 | Apache 2.0 |
| Multi-HMR 2 | HMR 비교 기준선 | 비교 | 확인 필요 |
| SOMA-X | 바디 표현 변환 후보 | 확장 | Apache 2.0 |
| TrackTacular, MVTrajecter | 추적 비교 후보 | 비교 | 확인 필요 |
| 3DGS-U, GS-Pool, GaussianUpdate, CL-Splats | 정적 공간 갱신 | 확장 | 확인 필요 |
| Spring Boot | EC2 백엔드 | MVP | Apache 2.0 |
| PostgreSQL, TimescaleDB, PostGIS | Spatial DB | MVP | PostgreSQL License, Apache 2.0(일부 기능 Timescale License), GPL-2.0 |
| Three.js | 뷰어 | MVP | MIT |
| Unreal Engine / Blender | 배경 빌드 / 대안 | 확장 | Unreal EULA / GPL |
| Tapo C110 | 카메라 | MVP | 제조사 약관 |
| YOLO26 분류·검출(미세조정) | PPE·신호수 표식 | 확장 | AGPL-3.0 |
| YOLO26-seg / YOLOE-26 | 마스크(사람 지우기, Tier 1 프롬프트), 새 물체 분할 | MVP(설치 시)·확장 | AGPL-3.0 |
| NVIDIA AutoMagicCalib (DeepStream) | 사람 궤적 기반 자동 보정 비교 후보 | 비교(G1) | 확인 필요 |
| ViTPose / Sapiens | 오프라인 자동 주석(학습 데이터 준비) | 보조 | 확인 필요 |
