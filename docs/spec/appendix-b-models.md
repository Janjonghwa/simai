# 부록 B. 모델·도구·라이선스 목록

교육 프로젝트라 상업화 제약은 없지만 출처와 조건을 기록한다. “확인 필요”는 채택 전 저장소에서 라이선스 파일을 확인한다. 내부 파라미터 추정 후보의 조사 근거는 `docs/design/markerless-intrinsics.md`(2026-10-17 확인)다.

| 이름 | 용도 | 단계 | 라이선스 |
|---|---|---|---|
| YOLO26x-pose(E2E) | Tier 0 검출·keypoint, 발 6점 미세조정(23점) | MVP | AGPL-3.0(Ultralytics) |
| COCO-WholeBody 발 주석 | 발 6점 미세조정 데이터·주석 형식 | MVP | 연구·비상업 |
| TrackTrack | Tier 0 Local Tracker(Ultralytics `tracktrack.yaml`) | MVP | AGPL-3.0(Ultralytics 구현) |
| AnyCalib (ICCV 2025) | 내부 파라미터 추정 학습 모델, 기본 경로(`anycalib_dist`, `radial:2`, 4.3) | MVP | 코드 Apache-2.0, 가중치 Apache-2.0 |
| GeoCalib (ECCV 2024) | 내부 파라미터 추정 학습 모델, 예비(AnyCalib 사용 불가 때, `distorted`·`radial`) | 예비 | 코드 Apache-2.0, 가중치 CC-BY-4.0(출처 표시 의무) |
| 고전 기준(plumb-line + 소실점, 저장소 코드) | 학습 모델이 모두 없을 때의 예비 경로와 학습 결과의 직선성 확인(4.3) | 예비·확인 | 저장소 코드(numpy, 가중치 없음) |
| Perspective Fields (CVPR 2023) | 내부 파라미터 추정 후보 | 제외 | Adobe Research License(비상업 전용, 재배포 금지). 렌즈 왜곡도 내지 않음 |
| WildCamera (NeurIPS 2023) | 내부 파라미터 추정 후보 | 제외 | 코드 Apache-2.0, 가중치 라이선스 표기 없음. 렌즈 왜곡을 내지 않음 |
| HumanCalib | 사람 기반 자동 보정(번들 조정) | 원리 참고(자체 구현, 4.3) | 코드 MIT. 코드·3D lifter 를 쓰지 않는다(기본 3D lifter MeTRAbs 는 비상업, VideoPose3D 는 CC BY-NC) |
| MapAnything | 초기 보정 추정, 정적 3D | MVP | 코드 Apache 2.0. 가중치 `facebook/map-anything`은 CC-BY-NC 4.0, `facebook/map-anything-apache`는 Apache 2.0(선행 PoC에서 품질 크게 낮음) |
| Depth Anything 3 | 정적 3D 비교군(pose-conditioned, MapAnything 외부 모델 인터페이스) | 비교 | DA3NESTED-GIANT-LARGE-1.1 은 CC BY-NC, Metric-Large 는 Apache 2.0 |
| MoGe-3 | 정적 3D 단안 대체 | 대안 | 코드 MIT, 체크포인트 확인 필요 |
| CCVS | 카메라별 높이 검산 | 보조 진단·재보정 후보 거름(자체 구현, 4.3) | 방법만 참고(코드 미사용) |
| DMMR | 사람 기반 내부 파라미터 추정 | 참고 | 확인 필요 |
| ExtrCamCalib_PersonKeypoints | 운영 중 보정 감시 | 참고 | 확인 필요 |
| NVIDIA Cosmos3-Nano(16B) Reasoner | VLM(vLLM 0.19.1 이상, `vllm-cosmos3`) | MVP | OpenMDW 1.1 |
| NVIDIA Cosmos3-Super(64B) | VLM 확장 후보(80GB 에 Tier 0 와 함께 안 올라감) | 확장 후보 | 확인 필요 |
| Qwen3-VL | VLM 참고(1순위 아님) | 참고 | 확인 필요 |
| BAAI/bge-m3 | RAG 임베딩(안전 수칙 조항·질문, dense 1024차원, L2 정규화, cosine, 11장). revision 은 운영 색인 생성 전에 고정 | M3 | MIT(채택 전 모델 저장소 라이선스 파일 확인) |
| GPT 6.1 sol(외부 API) | LLM 텍스트 생성(TBM 보고서 문장, RAG 답변, 운영자 질의응답) | MVP | 제공사 이용 약관(제공사 명칭·약관 확인 필요) |
| SAM 3D Body (Fast 변형) | MHR 인체 메시/포즈·관측 기반 외형 갱신 실험(EXP-02) | 선행 실험·확장 후보 | Fast 구현 코드 MIT(yangtiming/Fast-SAM-3D-Body), 가중치는 원본 `facebook/sam-3d-body-*`의 SAM License(HF 승인 필요) |
| MHR | SAM 3D Body 바디 표현 | 확장 | Apache 2.0 |
| PyTorch3D / nvdiffrast | EXP-02 2단계 UV 투영 | 실험 | 채택 전 확인(PyTorch3D 는 BSD 계열, nvdiffrast 는 NVIDIA 소스 코드 라이선스로 알려짐) |
| Multi-HMR 2 | HMR 비교 기준선 | 비교 | 확인 못 함. 바디 모델 Anny 는 Apache 2.0 |
| SOMA-X | 바디 표현 변환 후보 | 확장 후보 | Apache 2.0 |
| TrackTacular, MVTrajecter | 추적 연구 참고 | 참고 | 확인 필요 |
| 3DGS-U, GS-Pool, GaussianUpdate, CL-Splats | 정적 공간 갱신 | 확장 후보 | 확인 필요 |
| Spring Boot | EC2 백엔드(4.x, Java 21) | MVP | Apache 2.0 |
| PostgreSQL, TimescaleDB, PostGIS | Spatial DB(`timescale/timescaledb-ha` -all 이미지) | MVP | PostgreSQL License, Apache 2.0(일부 기능 Timescale License), GPL-2.0 |
| Three.js | 뷰어 | MVP | MIT |
| Unreal Engine / Blender | 배경 빌드 / 대안 | 확장 | Unreal EULA / GPL |
| Tapo C110 | 카메라 | MVP | 제조사 약관 |
| YOLO26 분류·검출(미세조정) | PPE·신호수 표식 | 확장 | AGPL-3.0 |
| Ultralytics Construction-PPE | PPE 공개 데이터 | 확장 | AGPL-3.0 |
| SH17 | PPE 공개 데이터 | 확장 | 확인 필요 |
| YOLO26-seg / YOLOE-26 | 마스크(사람 지우기, Tier 1 프롬프트), 새 물체 분할, 실측 정물 검출, PPE 무학습 기준선 | MVP(설치 시)·확장 | AGPL-3.0 |
| ViTPose | 오프라인 자동 주석(학습 데이터 준비) | 보조 | 확인 필요 |
| Sapiens / Sapiens2 | 오프라인 자동 주석(학습 데이터 준비) | 보조 | Sapiens(1) CC BY-NC 4.0 / Sapiens2 는 ‘Sapiens2 License’(조문 확인 못 함) |
