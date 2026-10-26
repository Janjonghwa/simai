"""I5 `tbm_report` 처리기: PLANNED 계산 결과(TBM)·이벤트 요약(DAILY)과 조항 → 외부 LLM 보고서 초안 (명세 5.4, 8.3, 12.5, FR-142).

초안 생성·프롬프트·인용 검사는 `simai_ai.report.tbm`, LLM 호출은 `simai_ai.report.llm` 에 있다. 입력·결과 형식은
`contracts/messages/tbm_report.schema.json`. 외부 LLM 호출은 이 작업 안에서만 한다(5.4).

- 입력은 계약으로 먼저 검사한다(형식 오류는 재시도 불가 TBM_INPUT_INVALID).
- LLM 설정(SIMAI_LLM_*)이 없거나 잘못되면 호출하지 않고 재시도 불가 LLM_CONFIG 로 끝낸다. 키 값은 메시지·로그에 남기지 않는다.
- LLM 오류는 LlmError 의 code·retryable 그대로(시간 초과·연결·429·5xx 는 재시도 가능, 인증·그 밖의 4xx 는 불가).
- 인용·형식 검사로 초안을 두 번 버리면 재시도 불가 REPORT_DRAFT_REJECTED(같은 입력으로 외부 API 비용만 더 쓰지 않게).
- 시간 제한·재시도는 작업 대여 그대로다(러너가 timeout_ms 를 넘기면 취소한다). 처리기는 LLM 호출 사이에 ctx.cancelled 를 본다.
- 결과도 계약으로 검사한 뒤 돌려준다. 보고서는 경보가 아니다(알림 채널 없음).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from simai_ai.providers.contract import schema_errors
from simai_ai.report.llm import ChatClient, LlmError, LlmSettings, make_client
from simai_ai.report.tbm import ReportRejected, generate_report

from .handlers import JobContext, JobFailed

INPUT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/tbm_report.schema.json#/$defs/input"}
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/tbm_report.schema.json#/$defs/result"}

log = logging.getLogger("services.worker")


def client_from_env(env: Mapping[str, str] = os.environ) -> ChatClient:
    settings = LlmSettings.from_env(env)
    log.info("외부 LLM 호출 방식 %s, 모델 %s", settings.api_style, settings.model)  # 결과에는 넣지 않는다
    return make_client(settings)


class TbmReportHandler:
    def __init__(self, *, clock_ms: Callable[[], int], client_factory: Callable[[], ChatClient] = client_from_env) -> None:
        self._clock_ms = clock_ms
        self._client_factory = client_factory

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        inp = json.loads(json.dumps(ctx.input))
        if errors := schema_errors(inp, INPUT_SCHEMA):
            raise JobFailed("TBM_INPUT_INVALID", "; ".join(errors))
        try:
            client = self._client_factory()
        except LlmError as e:
            raise JobFailed(e.code, str(e), retryable=e.retryable) from None
        ctx.report_progress(0.1)
        try:
            result = generate_report(inp, client, now_ms=self._clock_ms, cancelled=ctx.cancelled,
                                     progress=ctx.report_progress)
        except LlmError as e:
            raise JobFailed(e.code, str(e), retryable=e.retryable) from None
        except ReportRejected as e:
            raise JobFailed("REPORT_DRAFT_REJECTED", f"인용·형식 검사를 통과한 초안이 없다: {e}"[:1000]) from None
        except InterruptedError:
            raise JobFailed("CANCELLED", "작업이 취소됐다") from None
        except ValueError as e:  # 보내기 전 검사(텍스트만) 위반: 입력 조항 본문에 주소가 있는 등
            raise JobFailed("TBM_INPUT_INVALID", str(e)) from None
        if errors := schema_errors(result, RESULT_SCHEMA):
            raise JobFailed("TBM_RESULT_INVALID", "; ".join(errors))
        ctx.report_progress(1.0)
        return result
