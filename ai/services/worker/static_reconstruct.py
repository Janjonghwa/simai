"""I5 static_reconstruct 조립: 객체 저장소·취소·진행률을 scene 파이프라인에 연결한다."""
from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from simai_ai.scene.reconstruct import ReconstructionError, Stages, StaticReconstructor
from simai_ai.scene.reconstruct_process import ProcessStages

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError


class StaticReconstructHandler:
    def __init__(self, store: ObjectStore, *, clock_ms: Callable[[], int], stages: Stages | None = None,
                 env: Mapping[str, str] | None = None, workspace: Path | None = None,
                 on_preview: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.store = store
        self.pipeline = StaticReconstructor(stages or ProcessStages(env), clock_ms=clock_ms)
        self.workspace = workspace
        self.on_preview = on_preview

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        try:
            # 대여별 새 폴더·Scene 버전. 재시도 결과로 이전 결과를 덮지 않는다.
            with tempfile.TemporaryDirectory(prefix="static-", dir=self.workspace) as tmp:
                try:
                    return self.pipeline.run(ctx.input, Path(tmp), run_id=f"{ctx.job_id}-{ctx.lease_id}",
                        cancelled=ctx.cancelled, progress=ctx.report_progress,
                        get=lambda uri: self.store.get(ctx, uri),
                        put=lambda name, raw, content_type: self.store.put(ctx, name, raw, content_type),
                        on_preview=self.on_preview)
                except ReconstructionError as e:
                    # 실패한 외부 도구 로그도 임시 폴더 정리 전에 남긴다. 취소·대여 상실 뒤에는 저장소를 쓰지 않는다.
                    refs = []
                    for log in sorted(Path(tmp).glob("*.log")):
                        if ctx.cancelled.is_set():
                            break
                        try:
                            refs.append(self.store.put(ctx, f"failed-{log.name}", log.read_bytes(), "text/plain"))
                        except (ObjectStoreError, OSError):
                            break  # 진단 보존 실패로 원래 실패 코드를 바꾸지 않는다
                    if refs:
                        raise ReconstructionError(e.code, f"{e}; logs={','.join(refs)}") from e
                    raise
        except ReconstructionError as e:
            raise JobFailed(e.code, str(e)) from e
        except ObjectStoreError as e:
            raise JobFailed("STATIC_OBJECT_FAILED", str(e), retryable=e.retryable) from e
        except (ValueError, KeyError, TypeError) as e:
            raise JobFailed("STATIC_OUTPUT_INVALID", type(e).__name__) from e
        except OSError as e:
            raise JobFailed("STATIC_IO_FAILED", type(e).__name__) from e
