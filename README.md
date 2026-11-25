# SimAI

> **S**mart **I**ntegrated **M**onitoring **AI**
> 일반 CCTV 4대로 만드는 3D 건설 안전 관제 플랫폼

건설업 사망사고의 52.6%는 떨어짐 사고입니다. 2D 영상만 보는 AI CCTV는 사람이 "어디쯤" 있는지는 알아도,
바닥에서 몇 m 위에 있는지, 개구부까지 얼마나 남았는지는 모릅니다.

SimAI는 추가 장비 없이 **일반 CCTV 4대만으로 현장을 미터 단위 3D 공간으로 재현**하고, 작업자를 3D 골격으로 추적해
위험 구역 진입·진입 예측·고소 작업·낙상 의심을 판정합니다. 판정 결과는 3D 관제 화면, 현장 경보음, 작업자 워치로
동시에 전달되고, 작업 전에는 3D 맵 위에 AI가 위험 구역을 추천하고 법령 그래프로 TBM(작업 전 안전 대화) 보고서를 채웁니다.

| | |
| --- | --- |
| 시연 영상 | [YouTube](https://youtu.be/DcXtVS5CMq4) |
| 발표 자료 | [슬라이드 보기](https://janjonghwa.github.io/simai-deck.html) · [Canva 원본](https://canva.link/tob8j0584boin80) |
| 포트폴리오 | [janjonghwa.github.io](https://janjonghwa.github.io/) |
| 기간 · 인원 | 2026.10.01 – 2026.11.24 · 5인 (SSAFY 15기 자율 프로젝트) |

[![SimAI 시연](https://janjonghwa.github.io/images/simai-cover.jpg)](https://youtu.be/DcXtVS5CMq4)

---

## 결과

- 추가 장비 없이 CCTV 4대만으로 현장 3D 맵(카메라 높이 잔차 최대 **1.78 cm**)과 다시점 3D 위치 추적 구현
- 세팅부터 TBM 보고서, 실시간 관제, 경보, 다시 보기까지 전 과정을 실제 로비에서 라이브로 시연
- 위험 판정부터 작업자 워치 알림까지 약 **0.3초**
- 관제 화면에서 GPU 서버를 켜면 약 **11분** 만에 자동 준비·연결되는 운영 구조 (AWS 서울 g7e 기준)

## 주요 기능

### CCTV 4대로 만든 3D 공간

같은 시점에 찍힌 CCTV 4장을 왜곡 보정한 뒤 카메라 위치와 깊이를 추정해 하나의 3D 맵으로 융합합니다.
점구름 바닥이 1° 남짓 기울어 카메라 높이가 최대 8 cm 어긋나던 문제는, 실측 카메라 높이로 바닥 기울기를 보정해 최대 1.78 cm 안으로 맞췄습니다.

![3D 맵](https://janjonghwa.github.io/images/video/simai-map.jpg)

### 다시점 삼각측량으로 3D 위치 추적

카메라마다 따로 붙인 2D 관절 ID를 카메라 쌍별로 검사해 같은 사람끼리 묶고, 다시점 삼각측량과 헝가리안 배정으로 하나의 3D ID를 유지합니다.
화면에서 머리 높이가 같아 보이는 두 사람도, 한 명은 바닥·한 명은 단상 위라는 것을 3D 높이로 구분합니다.

![삼각측량](https://janjonghwa.github.io/images/simai-track.jpg)

### 위험 판정과 경보

위험 구역 진입, 2초 뒤 진입 예측, 고소 작업, 낙상 의심, 보호구 미착용 등을 판정하고 경보의 발생·확인·해소를 관리합니다.
들어가기 전에 「위험구역 접근」 주의를 먼저 보내고, 실제 진입하면 관제 화면·워치·현장 경보음이 같은 경보를 동시에 받습니다.
낙상 의심은 진행 중인 다른 경보와 상관없이 즉시 다시 울립니다.

### 세팅 — 카메라 4대에서 3D 맵까지

카메라 높이만 입력하면 배경 촬영 → 카메라 보정(마커 없음) → 3D 맵 생성 → AI 위험 구역 초안까지 한 흐름으로 진행됩니다.
같은 현장이면 이전에 만든 맵을 GPU 없이 1~2분 만에 다시 씁니다.

![세팅](https://janjonghwa.github.io/images/video/simai-setup.jpg)

### 법령 그래프 RAG로 채우는 TBM 보고서

산업안전 법령 10종의 참조·위임·준용 관계를 노드 7,963개·엣지 14,265개 그래프로 만들고 임베딩 검색과 결합해,
작업 공종에 맞는 위험 요인과 방호 조치를 찾아 TBM 보고서를 채웁니다. 외부 LLM에는 문장 작성만 맡기고 텍스트만 보내며, 인용은 코드로 검사합니다.

![법령 그래프](https://janjonghwa.github.io/images/simai-graph.jpg)

### 127관절 메시 아바타

카메라 영상 속 사람에 127관절 3D 인체 모델(SAM 3D Body)을 맞추고, 카메라에서 보인 옷 색을 입혀 3D 화면에서도 누가 누구인지 알아볼 수 있게 했습니다.
얼굴은 생성하지 않고, 위치와 경보는 아바타가 아니라 삼각측량 결과로만 정합니다.

![아바타](https://janjonghwa.github.io/images/video/simai-avatar.jpg)

## 설계 원칙

- **Tier 0가 위치의 정본** — 3D 위치·검증된 관절·위험 이벤트는 실시간 판정 계층만 만듭니다. 아바타·3D 맵·VLM 결과로 덮어쓰지 않습니다.
- **모르면 비운다** — 2대 미만에서 보인 관절은 지어내지 않고, 바닥을 모르면 높이를 0으로 채우지 않습니다.
- **AI는 제안만** — VLM은 경보를 낮추거나 끄지 못하고 상향 의견만 냅니다.
- **계약 우선** — 파트 사이 데이터 형식은 `contracts/`의 JSON Schema를 먼저 정하고 코드가 따릅니다.

## 시스템 구조

![시스템 구조](https://janjonghwa.github.io/images/simai-arch.jpg)

현장 노트북이 카메라 4대의 영상을 GPU 서버로 중계하고, GPU 서버의 Tier 0가 2D 관절 → 3D 위치 → 위험 판정을 실시간으로 처리합니다.
판정 결과는 EC2의 Spring Boot 서버를 거쳐 관제 웹, 갤럭시 워치, 현장 경보음으로 전달됩니다.

## 기술 스택

| 구분 | 사용 기술 |
| --- | --- |
| AI · 3D | Python 3.11, PyTorch 2.11 (CUDA 13), YOLO26-pose, TensorRT, MapAnything · WorldMirror, SAM 3D Body, vLLM (Cosmos3-Nano) |
| RAG | PostgreSQL + pgvector · pg_trgm, Qwen3-Embedding, FastAPI |
| Backend | Java 21, Spring Boot 4.1, JPA, Flyway, WebSocket(STOMP), PostgreSQL 17 + TimescaleDB · PostGIS |
| Frontend | React 19, TypeScript, Vite, Three.js · react-three-fiber, TanStack Router/Query, Tailwind CSS |
| Watch | Android (Kotlin), Galaxy Watch |
| Infra | Docker (bake), AWS EC2 · S3 · g7e GPU, RunPod, Nginx, Jenkins |

## 저장소 구성

```
contracts/   파트 사이 JSON Schema 정본 (메시지·공급자·실험·데이터)
ai/          실시간 추적·위험 판정(Tier 0), 3D 맵·아바타, VLM, 법령 RAG
gateway/     현장 노트북: 카메라 중계·녹화·현장 경보음·원격 제어
server/      EC2 Spring Boot API 서버
front/       관제 웹 (3D 뷰어, 세팅, 작업 관리, 다시 보기)
watch/       갤럭시 워치 앱
deploy/      Docker 이미지, Compose, GPU(RunPod·AWS) 배포 스크립트
docs/        기획서(spec/), 요구사항, 구현 설계(design/), 실험, 팀 규칙
exec/        포팅 매뉴얼, 외부 서비스, 시연 시나리오
```

처음 보신다면 [`docs/spec/README.md`](docs/spec/README.md)(기획서 장 목록), [`exec/01_포팅매뉴얼.md`](exec/01_포팅매뉴얼.md)(빌드·배포), [`exec/04_시연시나리오.md`](exec/04_시연시나리오.md)(시연 흐름) 순서를 권합니다.
`docs/design/`에는 티켓별로 정한 수치와 근거, 버린 대안, 남은 위험이 남아 있습니다.

## 팀과 역할

5인 팀에서 **기술팀장**을 맡아 시스템 설계와 AI·3D·인프라 구현을 담당했습니다.

- 기획서·요구사항·파트 간 JSON Schema 계약 관리, Jira 에픽·스토리 설계, AI 코딩 에이전트를 티켓 단위로 운영하며 결과 검증과 통합
- 다중 카메라 3D 추적과 위험 판정 엔진
- CCTV 사진으로 현장 3D 맵을 만드는 파이프라인과 127관절 아바타, 3D 뷰어
- 마커 없는 카메라 보정과 세팅 흐름, 이전 맵 재사용
- 법령 그래프 RAG와 VLM 상황 해석
- 현장 노트북 게이트웨이, 단일 GPU Docker 이미지, 관제 화면에서 GPU를 켜는 셀프서비스, Jenkins CI

서버·관제 웹·CI는 팀원과 함께 만들었고, 보호구 탐지 모델과 갤럭시 워치 앱은 팀원이 맡았습니다.

---

SSAFY GitLab에서 옮긴 저장소입니다. 커밋 이력은 그대로 보존했고, 운영 비밀값과 DB 덤프는 공개 저장소에서 제외했습니다.
