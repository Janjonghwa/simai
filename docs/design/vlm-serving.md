# VLM 서빙과 클라이언트 (S15P31A101-22)

관련 명세: 3.2(GPU 예산·워커 보호), 3.12, 5.4(VLM 빠른 경로·VLM 입력 프레임), 15장 G7, 16장 "VLM 모델"·"VLM 서빙 환경".
코드 `ai/src/simai_ai/vlm/client.py`, 시험 `ai/tests/vlm/test_client.py`(가짜 서버, GPU 없음).

## 서빙 구성

- 모델·판: Cosmos3-Nano(16B) Reasoner, vLLM 0.21.0 + `vllm-cosmos3`, CUDA 13.0, torch 2.11.0. Tier 0 와 같은 가상환경, 프로세스 분리(명세 16장).
- 감시 프로세스가 띄운다(`deploy/README.md` "감시 프로세스" 3번): `--served-model-name cosmos3-nano`, `--host 127.0.0.1 --port $SIMAI_VLLM_PORT(8000)`,
  `--gpu-memory-utilization 0.55`, `--max-num-seqs 4`, `--max-model-len 32768`, `VLLM_USE_FLASHINFER_SAMPLER=0`.
- 반복 억제·형식 강제는 서버 인자가 아니라 요청 인자로 준다(아래).

## 3차 GPU 세션 결과 (2026-10-26)

숫자 원본은 `unified-env.md` "3차 GPU 세션 결과(2026-10-26, S15P31A101-278)". 핵심만:

| 항목 | 값 |
|---|---|
| 기동(`/health` 200) | 70.1 s (시한 1800 s) |
| VRAM | 가중치 16.79 GiB + KV 23.99 GiB(174,688 토큰), GPU 최대 약 54.2 GB(Tier 0 선확보 8 GB 포함) |
| 지연(그림 1280 px + 질문, 답 ≤ 256 토큰) | 동시 1 p95 2.04 s, 동시 4 p95 2.10 s, 10분 동시 4 p95 2.12 s, 오류 0 → G7(p95 ≤ 5 s) 통과 |
| Tier 0 영향(동시 4 지속) | `frame_age` p95 60 → 76 ms, `pipeline_lag` p95 148 → 154 ms, ready 4/4 |
| 답 품질 | 같은 문장 14회 반복 1건 |

같은 그림을 되풀이했으므로 prefix 캐시 덕에 지연이 낙관적일 수 있다(4차 v4 가 다시 잰다).

## 클라이언트

```python
from simai_ai.vlm.client import VlmClient, VlmSettings
from simai_ai.vlm.face_blur import prepare_vlm_frames

gate = prepare_vlm_frames(items, "BLURRED", provider)          # 270 관문(워커는 services.worker.vlm_frames.gate_input_frames)
client = VlmClient(VlmSettings.from_env(os.environ))
r = client.complete(question, gate.frames, response_format=None)  # → VlmResult(content, model, latency_ms, input_tokens, output_tokens, finish_reason)
```

- 이미지는 `VlmFrame` 만 받는다(배열·바이트·URL·경로는 `VLM_INPUT`). 픽셀은 클라이언트 안에서 가로 1280 px 로 줄여 JPEG 품질 90 data URL 로 만든다(OpenCV, `vlm_int.py` 와 같음).
- 보내기 전에 본문의 이미지 파트가 모두 이 호출에서 만든 data URL 인지 본다. 아니면 보내지 않는다.
- 요청 모양은 `deploy/scripts/vlm_int.py` 의 `chat_once` 와 같다(`POST /v1/chat/completions`, user 메시지에 text → image_url 파트, `max_tokens` 256). 다른 점: 측정용 `stream`·`temperature 0`·질문 앞 표식을 싣지 않고 `repetition_penalty` 를 싣는다. 시험이 둘을 비교한다.
- 주소는 `http://127.0.0.1:{SIMAI_VLLM_PORT}` 고정이다. 호스트를 설정으로 받지 않는다: vLLM 은 루프백에서만 듣고, 얼굴을 처리한 프레임이라도 이미지를 Pod 밖으로 보내지 않는다(외부 LLM 에는 텍스트만, AGENTS 8). 그래서 원격 https 경로를 만들지 않았다.
- 스트리밍(첫 토큰 지연)은 넣지 않았다. 첫 토큰 지연은 `vlm_int.py` 가 잰다. 처리기가 필요하면 그때 넣는다.

### 설정 변수

| 변수 | 기본 | 범위 | 비고 |
|---|---|---|---|
| `SIMAI_VLLM_PORT` | 8000 | 1~65535 | 감시 프로세스와 같은 변수 |
| `SIMAI_VLLM_MODEL` | `cosmos3-nano` | | `--served-model-name` |
| `SIMAI_VLM_TIMEOUT_S` | 30 | 0 초과 ~600 | G7 p95 2.1 s 의 10배 이상. 동시 4 가 찬 대기열에서도 넉넉하게 |
| `SIMAI_VLM_MAX_TOKENS` | 256 | 1~4096 | 3차 세션·`vlm_int` 와 같음(G7 측정 범위) |
| `SIMAI_VLM_REPETITION_PENALTY` | 1.05 | 0 초과 ~2 | vLLM 확장. 1 이면 끔 |
| `SIMAI_VLM_FREQUENCY_PENALTY` | 없음(미전송) | -2~2 | |
| `SIMAI_VLM_TEMPERATURE` | 없음(미전송) | 0~2 | 미전송이면 모델 generation_config 기본값 |

### 오류 코드

| 코드 | retryable | 경우 |
|---|---|---|
| `VLM_CONFIG` | 아니오 | 설정 값이 숫자가 아니거나 범위 밖 |
| `VLM_INPUT` | 아니오 | VlmFrame 아닌 이미지, 빈 질문, data URL 아닌 이미지 파트, 모르는 `response_format.type` |
| `VLM_REQUEST_REJECTED` | 아니오 | HTTP 4xx(429 제외). 메시지에 vLLM 오류 문구(예: 문맥 길이 초과) |
| `VLM_UNAVAILABLE` | 예 | 연결 실패(기동·재기동 중), HTTP 429·5xx, 응답 중 끊김 |
| `VLM_TIMEOUT` | 예 | `SIMAI_VLM_TIMEOUT_S` 안에 응답 없음 |
| `VLM_BAD_RESPONSE` | 예 | 비JSON, `choices` 없음·빈 목록, 내용 비었음 |

모두 `VlmError` 하위이고 `code`·`retryable` 을 가진다. 처리기(25)는 `JobFailed(e.code, str(e), retryable=e.retryable)` 로 옮긴다.
`finish_reason == "length"` 는 오류로 보지 않고 결과에 둔다(형식 강제 JSON 이 잘렸는지 처리기가 판단).

## 답 반복 대응

3차 세션의 반복 1건은 `temperature 0`(탐욕)·억제 인자 없음 조건이었다. 대응은 요청 인자다.

- `repetition_penalty` 기본 1.05: Qwen2-VL·Qwen2.5-VL 이 generation_config 에 두는 약한 값이다(Cosmos3-Nano 자체 generation_config 값은 노트북에 가중치가 없어 확인하지 못했다, 4차 세션에서 본다). 근거가 반복 1건뿐이라 더 세게 두지 않았다(답 문장이 짧고 한국어 조사 반복을 깎을 위험).
- `frequency_penalty` 는 기본으로 싣지 않는다. 4차 세션에서 반복이 남으면 켠다.
- temperature 는 싣지 않는다(지시대로). 결정적 답이 필요하면 `SIMAI_VLM_TEMPERATURE=0` 과 억제 인자를 함께 쓴다.
- 형식 강제: `complete(..., response_format={"type": "json_schema", ...})` 를 그대로 싣는다(vLLM 이 structured outputs 로 처리). 스키마는 25 가 정한다.

## 4차 세션에서 이 클라이언트로 한 번 부르기

v4(`vlm_int.py`) 뒤 vLLM 이 떠 있는 Pod 에서(통합 이미지 venv, 그림은 `vlm_int` 입력 폴더의 `cam_*.jpg`):

```bash
python - <<'EOF'
import os, cv2, glob
from simai_ai.vlm.client import VlmClient, VlmSettings
from simai_ai.vlm.face_blur import prepare_vlm_frames
# provider 는 워커의 pose 검출기. 검출기 없이 확인만 하려면 사람 없음을 돌려주는 가짜 provider 를 쓴다(얼굴 처리 없이 지나간다 — 확인용만)
class NoPeople:
    def people(self, image): return []
items = [(p, cv2.imread(p)) for p in sorted(glob.glob(os.environ["VLM_IMAGES"] + "/cam_*.jpg"))[:1]]
gate = prepare_vlm_frames(items, "BLURRED", NoPeople())
r = VlmClient(VlmSettings.from_env(os.environ)).complete("이 교실 그림에서 사람이 몇 명 보이고, 넘어지거나 위험해 보이는 사람이 있는지 한 문장으로 답하라.", gate.frames)
print(r)
EOF
```

`VLM_IMAGES` 는 그림 폴더. 결과(지연·토큰·finish_reason·답 반복 여부)는 4차 세션 기록에 남긴다.

## 남은 일

- 4차 세션 v4 재측정(S15P31A101-291): 새 CPU 묶기·질문 앞 표식으로 지연 재확인, 위 명령으로 이 클라이언트 한 번 호출.
- `repetition_penalty` 1.05 는 항상 보낸다(`SIMAI_VLM_REPETITION_PENALTY=1` 이면 끔). 3차 세션 지연 측정은 이 인자 없이 쟀으므로 4차 세션의 이 클라이언트 호출로 지연·답을 확인한다.
- 실제 카메라 그림(매번 다름)으로 지연 재측정.
- vlm_rationale 응답 JSON 스키마와 `response_format`(S15P31A101-25), 결과 파싱·상향 의견 검증(25·273).
- 35 G7 평가.
- `encode_jpeg`(OpenCV) 시험은 노트북 개발 환경에 cv2 가 없어 건너뛴다(워커 이미지·Linux CI 에서 돈다).
