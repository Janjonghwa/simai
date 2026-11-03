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
| MapAnything / Open3D / mvs-texturing(`texrecon`) | 초기 보정 추정, 정적 3D 배경 복원·텍스처 메시 굽기(통합 이미지의 같은 가상환경 torch 2.11.0, `simai-bootstrap scene` 일회성 모드와 분석 워커 I5 `static_reconstruct` 자식 프로세스, 3.12·5.3. 2.7.1 `mapanything` 이미지는 동등성 비교까지만 유지) | MVP | MapAnything 코드 Apache 2.0(가중치 `facebook/map-anything`은 CC-BY-NC 4.0, `facebook/map-anything-apache`는 Apache 2.0), Open3D MIT, mvs-texturing BSD-3-Clause |
| Depth Anything 3 | 정적 3D 비교군 두 경로(MapAnything 자세 고정 + 공식 `inference` 자세 조건 깊이, 자세 없이 DA3NESTED 단독 미터 추정, 5.3). 실험 단계에서는 통합 잠금에 넣지 않고 별도 가상환경에서 깊이만 낸다(`docs/design/exp01-hybrid-depth.md`) | 비교 | 코드 Apache-2.0. DA3NESTED-GIANT-LARGE-1.1 가중치는 CC BY-NC-4.0 이고 MapAnything 기본 가중치와 같은 판단(교육 시연 사용, 공개 산출물 출처 표기). Apache-2.0 대안은 DA3-BASE·DA3-SMALL·DA3METRIC-LARGE |
| MoGe-3 | 정적 3D 단안 대체였으나 EXP-01 비교 큐와 G4 미달 대체에서 모두 뺌(Lead 결정 2026-10-26, 5.3) | 제외 | 코드 MIT, 체크포인트 확인 필요 |
| CCVS | 카메라별 높이 검산 | 보조 진단·재보정 후보 거름(자체 구현, 4.3) | 방법만 참고(코드 미사용) |
| DMMR | 사람 기반 내부 파라미터 추정 | 참고 | 확인 필요 |
| ExtrCamCalib_PersonKeypoints | 운영 중 보정 감시 | 참고 | 확인 필요 |
| NVIDIA Cosmos3-Nano(16B) Reasoner | VLM(vLLM 0.21.0 + `vllm-cosmos3` 커밋 고정, Tier 0 와 같은 가상환경의 별도 프로세스, 3.12) | MVP | OpenMDW 1.1 |
| NVIDIA Cosmos3-Super(64B) | VLM 확장 후보(80GB 에 Tier 0 와 함께 안 올라감) | 확장 후보 | 확인 필요 |
| Qwen3-VL | VLM 참고(1순위 아님) | 참고 | 확인 필요 |
| Qwen/Qwen3-Embedding-0.6B | 법령 지식 그래프(RAG) 임베딩(법령 조문·지침·사례 노드 `rag_node` 및 질의, dense 1024차원, L2 정규화, cosine, 문서 입력 `"[법령명 조문(제목)] 본문"`, 5.4·11장, v1.3 수정 49). 문서·질의 동일 고정 revision 사용(이전 `BAAI/bge-m3` 대체) | M3 | Apache-2.0 |
| BAAI/bge-m3 | 초기 RAG 임베딩(`Qwen/Qwen3-Embedding-0.6B` 로 전환, v1.3 수정 49) | 교체됨 | MIT |
| graphify-legal-wiki / LegalGraphRAG (arXiv:2605.28120) | 한국 법령 조문 파싱·위계·교차 참조 그래프 및 하이브리드 검색 구조 설계 참고(`docs/design/tbm-legal-graph.md`) | 원리 참고(자체 구현, 5.4·11.3) | `graphify-legal-wiki` MIT, 논문 원리 참고(코드 미사용) |
| GPT 6.1 sol(외부 API, SSAFY GMS 를 거쳐 OpenAI, 모델 `gpt-6.1-sol`) | LLM 텍스트 생성(TBM 보고서 문장, RAG 답변, 운영자 질의응답) | MVP | OpenAI 이용 약관(SSAFY GMS 이용 안내 따름, 약관 확인 필요) |
| SAM 3D Body (Fast 변형) | MHR 인체 메시·포즈 스트림과 관측 기반 외형 갱신(EXP-02, 5.6·20.3) | 시연 범위(E-GATE-0\~3 검증) | Fast 구현 코드 MIT(yangtiming/Fast-SAM-3D-Body), 가중치는 원본 `facebook/sam-3d-body-*`의 SAM License(HF 승인 필요) |
| MHR | SAM 3D Body 바디 표현(5.6, 13.2) | 시연 범위 | Apache 2.0 |
| `torch` / `numpy` z-buffer 래스터화 (후보: PyTorch3D / nvdiffrast) | EXP-02 2단계 UV 투영·누적(5.6, 20.3). 구현은 새 외부 의존성 없이 `torch`/`numpy` 원근 보정 래스터화를 씀 | 시연 범위(2단계) | BSD-3-Clause(PyTorch, NumPy) |
| Multi-HMR 2 | HMR 비교 기준선 | 비교 | 확인 못 함. 바디 모델 Anny 는 Apache 2.0 |
| SOMA-X | 바디 표현 변환 후보 | 확장 후보 | Apache 2.0 |
| TrackTacular, MVTrajecter | 추적 연구 참고 | 참고 | 확인 필요 |
| 3DGS-U, GS-Pool, GaussianUpdate, CL-Splats | 정적 공간 갱신 | 확장 후보 | 확인 필요 |
| Spring Boot | EC2 백엔드(4.x, Java 21) | MVP | Apache 2.0 |
| FastAPI | EC2 Risk Engine·보고서 생성 컨테이너 내부 API(3.8, 5.4, 12.2) | MVP | MIT |
| PostgreSQL, TimescaleDB, PostGIS | Spatial DB(`timescale/timescaledb-ha` -all 이미지) | MVP | PostgreSQL License, Apache 2.0(일부 기능 Timescale License), GPL-2.0 |
| Three.js | 뷰어 | MVP | MIT |
| Unreal Engine / Blender | 배경 빌드 / 대안 | 확장 | Unreal EULA / GPL |
| Tapo C110 | 카메라 | MVP | 제조사 약관 |
| YOLO26x 검출(미세조정, 4클래스) | PPE 판정(5.5). 신호수 표식 클래스는 확장 | MVP(M3) | AGPL-3.0 |
| Ultralytics Construction-PPE | PPE 공개 데이터(비교군·보완 후보) | MVP(M3) | AGPL-3.0 |
| SH17 | PPE 공개 데이터 | 라이선스 확인 전 미사용 | 확인 필요 |
| YOLO26-seg / YOLOE-26 | 마스크(정적 3D 사람 지우기, 상시 아바타 루프 `services/avatar` 의 Tier 1 HMR 프롬프트·UV 텍스처 가림 마스크 `yolo26x-seg.pt`), 새 물체 분할, 실측 정물 검출, PPE 무학습 기준선 | MVP·시연 범위·확장 | AGPL-3.0 |
| ViTPose | 오프라인 자동 주석(학습 데이터 준비) | 보조 | 확인 필요 |
| Sapiens / Sapiens2 | 오프라인 자동 주석(학습 데이터 준비) | 보조 | Sapiens(1) CC BY-NC 4.0 / Sapiens2 는 ‘Sapiens2 License’(조문 확인 못 함) |
