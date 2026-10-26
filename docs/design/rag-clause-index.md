# RAG 조항 색인 (S15P31A101-27)

TBM·일일 일지 보고서가 인용할 안전 수칙 조항을 EC2 PostgreSQL pgvector(`rag_clause`)에 넣는다. 산업안전보건기준에 관한 규칙의
조항을 국가법령정보 Open API 에서 받아 조 단위로 나누고, BAAI/bge-m3 dense 로 임베딩해 Flyway 반복 마이그레이션으로 적재한다.
TBM 질문은 템플릿으로 미리 임베딩해 `rag_query_template` 에 둔다. 검색은 S15P31A101-223(EC2), 인용 검사는 S15P31A101-225(분석 워커 `tbm_report`)다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 3.8, 11장 첫머리 | RAG 색인은 같은 PostgreSQL 의 pgvector. bge-m3 dense 1024차원, float32, L2 정규화, cosine. 문서·질문 같은 revision·설정, 운영 색인 전에 revision 고정 | revision 고정, `R__rag_clauses.sql` 의 섞임 확인 |
| 5.4 | 인용은 RAG 로 검색된 조항 ID 만. RAG 문서는 산업안전보건기준에 관한 규칙 관련 조항 수십 개로 시작. 임베딩 실행 위치는 추후 | 31개 조, 노트북 CPU |
| 7.1, 7.2 | 규칙의 "근거 조항"은 보고서 인용과 연결. R3 2 m 의 정확한 조항은 RAG 문서 구축 때 확인 | R3 근거 조항(아래). 규칙 연결은 별도 스토리(S15P31A101-252 뒤) |
| 19.1 | bge-m3 revision·실행 위치·청크·배치 크기, R3 근거 조항 미결 | 아래 "명세에 없어 정한 것". 명세 반영은 명세 정리 MR |

## 결정 (2026-10-23, 이연주 승인, `.handoff/27-decision.md`)

- 원문: Open API JSON, 법령일련번호(MST) 273603 고정. 고른 조문만 정규화해 커밋하고 원 응답 전체는 커밋하지 않는다.
- 분할: 조 단위, 1,000자 넘는 조만 항·호 경계로 묶어 나눈다. ID `산업안전보건기준규칙 제N조[#k]`. 임베딩 입력에서 개정 꼬리표를 뺀다. 별표 제외.
- 임베딩: revision `5617a9f61b028005a4858fdac845db406aefb181`, transformers `AutoModel` CLS + L2 정규화, CPU fp32, 배치 1. 결과 파일 커밋.
- 질문: M3 는 질문 템플릿 사전 임베딩(`rag_query_template`). 자유 문장 질의(확장 3)는 나중에 EC2 별도 임베딩 컨테이너.
- DB: `V3__rag_clause.sql` + 생성 SQL `R__rag_clauses.sql`.
- R3 근거: 제32조(①2호) + 제44조(①). `rule_version.legal_basis` 연결(비어 있을 때만 채움, 다르면 멈춤, R3 v1 만, v1001 은 NULL)은 이 티켓에서 하지 않고 별도 스토리로 넘긴다. 같은 생성기를 고치는 S15P31A101-252 가 dev 에 들어간 뒤에 한다.

## 코드·데이터

| 위치 | 내용 |
|---|---|
| `ai/src/simai_ai/rag/law.py` | Open API 수집(`fetch`, `current_mst`)·정규화(`normalize`). 고정 `MST`, 조 목록 `ARTICLES` |
| `ai/src/simai_ai/rag/chunk.py` | 분할·조항 ID·임베딩 입력(`chunk_article`, `CLAUSE_ID_RE`, `strip_amendment_tags`) |
| `ai/src/simai_ai/rag/embed.py` | bge-m3 dense(`BgeM3`), 고정 `REVISION`·`SETTINGS`. torch·transformers 는 extra `rag` |
| `ai/src/simai_ai/rag/seed.py` | CLI `fetch`·`check-current`·`embed`·`sql --write/--check`, 시드 SQL 생성 |
| `ai/src/simai_ai/rag/data/clauses.json` | 정규화한 조항(판 정보·응답 sha256 포함, 약 49 KB) |
| `ai/src/simai_ai/rag/data/query_templates.json` | TBM 질문 템플릿 9개 |
| `ai/src/simai_ai/rag/data/embeddings.json` | 청크 38개·템플릿 9개의 float32 임베딩(base64 리틀엔디언)과 입력 sha256, 모델·revision·설정·실행 판(약 264 KB) |
| `server/src/main/resources/db/migration/V3__rag_clause.sql` | `CREATE EXTENSION vector`, 두 테이블, HNSW `vector_cosine_ops` 인덱스 |
| `server/src/main/resources/db/migration/R__rag_clauses.sql` | 생성물(약 0.66 MB). 손으로 고치지 않는다 |
| `ai/pyproject.toml` | extra `rag = ["torch==2.11.0", "transformers==5.9.0"]`(한 가상환경의 판과 같게). import-linter: risk·Tier 0 경로는 `simai_ai.rag` 금지 |
| `.env.example` | `SIMAI_LAW_API_OC` |

다시 만드는 순서(`ai/` 에서):

```sh
uv run --env-file ../.env python -m simai_ai.rag.seed fetch            # 망·OC 필요. --from-file <응답 JSON> 로 망 없이도
uv run --env-file ../.env python -m simai_ai.rag.seed check-current    # 개정 감시(자동 갱신 안 함)
uv run --extra rag python -m simai_ai.rag.seed embed                   # 노트북 CPU 약 1분
uv run python -m simai_ai.rag.seed sql --write ../server/src/main/resources/db/migration/R__rag_clauses.sql
```

## 원문 확보

- 국가법령정보 공동활용 Open API `lawService.do?target=law&MST=273603&type=JSON`. 법령ID `007363`, 공포 2025-09-01 고용노동부령 제450호,
  시행 2025-09-01. 정규화 원 응답 sha256 `d4b920afdee516a2ffc5604067cfeeb0be26ebf7fa98e36b4078763795226002`(`clauses.json`).
- 인증값 OC 는 `.env` 의 `SIMAI_LAW_API_OC`. 법령정보센터에 등록한 서버 IP 에서만 응답한다(등록하지 않은 IP 는 "사용자 정보 검증에 실패" 응답).
- 법령 본문은 저작권법 제7조 제1호로 보호받지 못하는 저작물이라 커밋해도 된다. 원 응답 전체(1.3 MB)는 커밋하지 않는다. 같은 MST 로 다시 받을 수 있다.
- 정규화: 조문마다 `head`(조문내용 머리 줄)와 `units`(항이 있으면 항과 그 호·목, 없으면 호와 그 목). 줄 앞뒤 공백만 걷고 원문(개정 꼬리표 포함)을 그대로 둔다.

## 조항 목록 (31개 조, 38개 청크)

결정 보고서 B 표 그대로다(보고서 본문의 "합계 30개"는 표의 행을 잘못 센 것이고 표에는 31개가 있다).

| 규칙 | 조 |
|---|---|
| R3 고소 | 제32조(보호구의 지급 등), 제44조(안전대의 부착설비 등), 제46조, 제56조, 제67조, 제68조, 제186조, 제187조 |
| R2 개구부·가장자리 | 제43조, 제13조, 제42조, 제45조, 제23조 |
| R1 위험 구역 | 제20조, 제14조, 제172조, 제171조, 제86조 |
| R4 낙상 | 제3조 |
| 통로 | 제22조, 제24조, 제30조, 제9조 |
| TBM 공통 | 제35조, 제36조, 제37조, 제38조, 제57조, 제63조, 제48조, 제624조 |

**R3 2 m 근거 조항.** 현행 제42조(추락의 방지)에는 높이 숫자가 없다. "높이 2미터 이상의 추락할 위험이 있는 장소"로 의무를 거는 곳은
제32조 제1항 제2호(안전대 지급·착용)와 제44조 제1항(안전대 부착설비)이다. 제46조(2 m **초과** 승강설비)·제56조(비계)·제42조 제4항 제6호(이동식 사다리)·
제67조(말비계)는 특정 설비나 다른 조치에 한정된다. 그래서 R3 근거는 제32조 + 제44조다. 7.2 의 "추락 방지 조치를 요구하는 높이"는 정확히는
"안전대 착용·부착설비를 요구하는 높이"이고, 7.2 문장과 19.1 항목은 명세 정리 MR 에서 고친다.

## 분할 규칙

- 조 본문 = 머리 줄 + 단위. 1,000자(`LIMIT_CHARS`) 이하면 청크 하나, ID `산업안전보건기준규칙 제N조`(가지 조문 `제66조의2`), `chunk_index 0`.
- 넘으면 단위 경계에서 앞에서부터 1,000자 이하로 묶고, 모든 청크 ID 에 `#k`(k = `chunk_index`, 0부터)를 붙인다. 접미사 없는 ID 는 그 조에 만들지 않는다.
- 모든 청크는 머리 줄로 시작한다. 항이 있는 조는 "제N조(제목)", 호만 있는 조(제13조·제20조 등)는 "제N조(제목) 사업주는 … 각 호 …" 라 청크 하나로도 뜻이 통한다.
- 단위 하나가 1,000자를 넘으면 그 단위만 한 청크다(항·호 중간에서 끊지 않음). 지금은 제63조#0(1,114자, 제1항과 그 호) 하나다.
- 나뉜 조: 제20조(3), 제38조(2), 제42조(2), 제63조(2), 제86조(2), 제186조(2). 토큰은 최대 1,232 로 bge-m3 입력 한도 8,192 안이다.
- `body` 는 원문 그대로, 임베딩 입력은 `"{law_name} {body}"` 에서 날짜가 든 꼬리표(`<개정 …>`, `<신설 …>`, `[본조신설 …]`)를 뺀 문자열이다.
  "[작업발판의 끝ㆍ개구부(開口部) 등을 제외한다]"처럼 날짜 없는 대괄호는 본문이라 남긴다.
- `law_name` 은 정식 이름(산업안전보건기준에 관한 규칙), ID 접두사는 `db-tables.md` 예의 `산업안전보건기준규칙`.
- `source_version` 은 조문 시행일(YYYY-MM-DD). 판(MST·공포)은 시드 머리 주석과 `clauses.json` 에 있다.
- 별표는 넣지 않는다(제35조가 별표 2·3 을 참조). 넣을 때는 `산업안전보건기준규칙 별표3` 처럼 별표 하나를 조처럼 다룬다.

## 임베딩

- `BAAI/bge-m3` revision `5617a9f…`(가중치 해시·라이선스: `docs/data/weights.md`). dense = 마지막 은닉 상태의 CLS 를 L2 정규화
  (FlagEmbedding `BGEM3FlagModel` dense 와 같은 계산). CPU fp32, 배치 1(패딩 없음), `max_length` 8192. 질문 템플릿도 같은 함수로 문장 그대로 넣는다.
- 결과는 `embeddings.json` 에 float32 그대로(base64) 커밋하고, 입력 문자열의 sha256 을 함께 둔다. 시드 생성은 이 파일만 읽어 모델 없이 돈다.
  조항·템플릿 문장이 바뀌었는데 다시 임베딩하지 않았으면 `sql` 이 멈춘다.
- SQL 의 벡터 값은 float32 로 다시 읽으면 같은 값이 되는 가장 짧은 십진 표기다(pgvector 는 float32 로 읽는다). DB 에 들어간 47개 벡터가 파일과 비트 단위로 같다(아래 시험).

**"같은 revision 이면 같은 결과".** 적재는 커밋된 임베딩을 넣으므로 다시 돌려도 비트 단위로 같다. 임베딩 재계산은 같은 기계·같은 판이면 비트 단위로 같고
(실측), 다른 기계(BLAS·CPU 명령어)는 보장하지 않아 재계산 시험은 최대 절댓값 차 1e-5 로 비교한다.

실측(노트북 i7-13700H, torch 2.11.0+cpu, transformers 5.9.0, 2026-10-23):

| 항목 | 값 |
|---|---|
| 31개 조(38 청크) + 템플릿 9개 임베딩(`seed embed`, 모델 적재 포함) | 약 62 s |
| 배치 1 / 4 / 8 (조 30개, 스크래치 측정) | 62.5 s / 117.0 s / 160.0 s. 배치 4·8 은 패딩 낭비로 느리고 배치 1 과 최대 차 3.9e-6 |
| 같은 설정 재실행 | 비트 단위로 같음 |
| 짧은 질문 1개 | 200~230 ms(스레드 4 로 266 ms), 프로세스 RSS 1.8 GB. 확장 3 별도 컨테이너의 크기 근거 |

## 질문 템플릿

EC2 Java 는 모델을 돌리지 않는다. TBM 질의는 PLANNED 결과(규칙 ID·대상)에서 나오므로 템플릿을 미리 임베딩한다. 키 형식 `규칙 ID.대상 종류`
(`^(R[1-5]|TBM)\.[A-Z_]+$`). EC2 가 `zone.zone_kind`(OPENING·출입 통제·장비 주차 등)와 규칙으로 키를 고른다(키 매핑은 223).

| 키 | 쓰는 경우 | 상위 결과(cosine, 로컬 DB) |
|---|---|---|
| `R1.OPENING` | R1, 개구부 구역 | 제20조#2 0.687, #0, #1, 제43조 0.651, 제32조 |
| `R1.RESTRICTED` | R1, 출입 통제 구역 | 제20조#0 0.704, #1, #2, 제43조, 제42조#0 |
| `R1.EQUIPMENT` | R1, 장비 주차 구역(R8 이전) | 제172조 0.785, 제171조 0.780, 제20조 |
| `R2.EDGE` | R2 | 제43조 0.793, 제45조, 제42조#0, 제13조, 제14조 |
| `R3.ELEVATED` | R3 | 제44조 0.820, 제46조, 제187조, 제42조#0, 제45조 |
| `R4.FALL` | R4 | 제3조 0.729, 제45조, 제32조, 제42조#0, #1 |
| `R5.ZONE` | R5, 구역 대상 | 제20조#0 0.636, #2, #1, 제35조, 제3조 |
| `R5.EDGE` | R5, 가장자리 대상 | 제45조 0.688, 제42조#0, 제43조, 제3조, 제42조#1 |
| `TBM.GENERAL` | 일일 일지·공통 | 제35조 0.736, 제38조#0, 제3조, 제186조#1, 제44조 |

- `R1.OPENING` 은 제43조(개구부)보다 제20조(출입 금지)가 앞선다. `R3.ELEVATED` 상위 5개에 R3 근거 제32조가 없다(제32조는 보호구 11종을 한 조에 담아 벡터가 흐려진다). 223 은 상위 k 를 넉넉히(예: 8) 넘기거나,
  규칙의 `legal_basis` 조항을 검색 결과와 합쳐 넣는다(별도 스토리에서 연결).
- 자유 문장 질의(확장 3 운영자 질의응답)는 템플릿으로 못 받는다. 그때 EC2 별도 임베딩 컨테이너(이미지 약 3~3.5 GB, RSS 약 1.9 GB)를 둔다.

## DB 반영

- `V3__rag_clause.sql`: `CREATE EXTENSION IF NOT EXISTS vector`(timescaledb-ha `pg17-all` 에 pgvector 0.8.7 포함, 로컬 확인), `rag_clause`, `rag_query_template`,
  `rag_clause_embedding_hnsw_idx`(HNSW `vector_cosine_ops`, 기본 m 16·ef_construction 64). 수십 행이면 순차 탐색이 더 빠르지만 설계대로 둔다.
- 번호 V3 는 merge 직전 dev 의 마이그레이션 번호를 다시 확인한다(S15P31A101-240 과 충돌 주의). Flyway `outOfOrder=false` 라 늦게 들어온 낮은 번호·같은 번호는 기동에서 멈춘다.
- `R__rag_clauses.sql`(생성물): 임시 시드 테이블 → 시드에 없는 행 삭제 → `INSERT … ON CONFLICT DO UPDATE … WHERE (…) IS DISTINCT FROM (…)`(바뀐 행만 고치고 `loaded_at` 도 그 행만 갱신)
  → 두 테이블 전체가 한 모델·revision 인지 확인(아니면 `RAISE EXCEPTION`).
- rag_clause 는 11.4-1 의 버전 추가 대상(구역·규칙·보정·Scene)이 아니고 PK 가 `clause_id` 하나라 revision·판을 바꾸면 행을 교체한다.
- 223 의 JPA 매핑: `ddl-auto: validate` 라 엔티티를 만들면 `vector` 타입 매핑(hibernate-vector 또는 네이티브 쿼리)이 필요하다.

## 명세에 없어 정한 것

| 값 | 근거 |
|---|---|
| 분할 기준 1,000자 | 후보 31개 중 6개만 나뉘고, 18개 호가 서로 다른 장소인 제20조가 3개로 나뉜다. bge-m3 한도(8,192 토큰)와 무관한 검색 정밀도 기준 |
| 배치 1 | 실측에서 가장 빠르고 패딩에 따른 값 차이가 없다 |
| 재계산 허용오차 1e-5 | 배치 차이(3.9e-6)보다 크고 cosine 순위를 바꾸지 않는 크기 |
| 템플릿 9개와 문장 | R1~R5 × 대상 종류 초안. 223 과 맞춰 고친다 |

## 버린 대안

- **Risk Engine 컨테이너에 임베딩 API**: 이미지 226 MB → 약 3.5 GB, risk 의 torch 금지(import-linter·`check_image.sh`)를 깨고 D-G3-04 대조 이미지가 무거워진다.
- **분석 워커(GPU Pod)에서 질문 임베딩**: Pod 는 검증할 때만 켜므로 TBM 이 Pod 가동에 묶이고, 임베딩 → 검색 → `tbm_report` 두 단계가 된다.
- **Spring 안 ONNX Runtime Java**: 문서(PyTorch)와 질문(ONNX)의 실행기가 달라 "같은 설정" 근거가 약하다.
- **psycopg 로 DB 에 직접 적재**: EC2 DB 는 internal 망이라 터널·비밀번호가 필요하고, 배포와 적재가 따로 놀아 빠뜨리기 쉽다.
- **항 단위 ID(`제42조제2항`)**: 청크 수가 몇 배가 되고 짧은 항만 떨어져 문맥이 약하다. `db-tables.md` 의 `#chunk_index` 정의와 다르다.
- **배치 4·8**: 패딩 때문에 느리고 값이 미세하게 달라진다.

## 시험

| 시험 | 보는 것 |
|---|---|
| `ai/tests/rag/test_chunk.py` | 분할(짧은 조 한 청크·접미사 없음, 긴 조 단위 경계로 묶음·모든 청크 접미사, 넘는 단위 통째, 호만 있는 조의 머리 줄 반복), 가지 조문 ID, 꼬리표 제거, 커밋 조항의 ID 형식·PK 중복 없음·chunk_index 연속, R3 근거 문구 |
| `ai/tests/rag/test_law.py` | API 응답 정규화(항·호·목, 항 없는 조문, 가지 조문, 편장 머리 건너뜀, 없는 조·다른 법령 거부), 커밋 조항 파일이 고정 MST·조 목록과 같음 |
| `ai/tests/rag/test_seed.py` | float32 최단 표기 왕복, 임베딩 파일이 현재 입력과 맞음·1024차원·노름 1(±1e-6), 입력이 바뀌면 거부, 템플릿 키, 생성 SQL 안정성·최신성(`sql --check`) |
| `ai/tests/rag/test_embed_model.py` | (extra `rag` + 가중치 캐시가 있을 때만) 재계산이 커밋 값과 1e-5 안, 재실행 비트 단위 같음. CI 는 건너뜀 |

로컬 DB 확인(2026-10-23, Docker `timescale/timescaledb-ha:pg17-all@sha256:26e355e…`, compose 와 같은 digest):

- Spring Boot 기동으로 Flyway 가 V1 → V2 → V3 → `R__rag_clauses` → `R__rule_versions` 를 적용했다(5개 성공, 서버 기동 완료).
- `rag_clause` 38행·`rag_query_template` 9행, revision 1종, 벡터 노름 0.99999989~1.00000011, 1024차원. DB 벡터 47개가 `embeddings.json` 과 비트 단위로 같다.
- `SET enable_seqscan=off` 에서 cosine 정렬이 `rag_clause_embedding_hnsw_idx` 를 쓴다.
- 시드 SQL 재실행: 같은 시드면 행·`loaded_at` 이 그대로(md5 같음). 한 행 본문을 바꾸고 시드 밖 행을 넣은 뒤 재실행하면 바뀐 행만 복구·`loaded_at` 갱신, 시드 밖 행 삭제. revision 을 섞으면 확인이 멈춘다.
- `server/src/test/resources/db/v1-smoke.sql`: 종료 코드 0, OK 15건, 실패 0건(같은 DB 에서 `rule_version` 9행·`site` 1·`camera` 4).

## 잔여 위험·넘긴 일

- 팀 OC 는 법령정보센터에 등록한 서버 IP 에서만 응답한다. 다른 PC 에서 `fetch`·`check-current` 를 돌리려면 그 IP 를 더 등록한다.
  `clauses.json` 은 같은 MST 를 공개 예시 OC 로 받은 응답에서 만들었고, IP 등록 뒤(2026-10-23) 팀 OC 로 다시 받은 응답의 sha256 이 같았다(`d4b920af…`). 현행 MST 도 273603 이다.
- 법령 개정은 자동으로 따라가지 않는다. `check-current` 가 알리면 판(MST)을 올리고 fetch → embed → sql 을 다시 돌린다.
- 템플릿 키 매핑(`zone_kind` 값 → 키)은 223 이 정한다. `zone_kind` 는 값 목록이 정해지지 않은 텍스트다.
- `R__rag_clauses.sql` 이 0.66 MB 라 조항이 늘면 커진다(수백 조면 수 MB). 그때 행 단위 파일이나 적재 명령을 다시 본다.
- 넘긴 일: R3 `legal_basis` 연결(별도 스토리, S15P31A101-252 merge 뒤), 계약 예시 `KOSHA-R-43`·`KOSHA-G-123-4.2` 교체(S15P31A101-225), 7.2 문장·19.1 정리(명세 정리 MR),
  `db-erd.md`·`db-verification.md` 의 테이블 수(69) 갱신(DB 설계 문서 정리 때).
