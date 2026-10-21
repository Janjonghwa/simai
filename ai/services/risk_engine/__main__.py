"""Risk Engine 컨테이너 진입점: `python -m services.risk_engine [--host H] [--port P]`.

컨테이너(deploy/risk-engine)는 compose 내부망에서 0.0.0.0:8080 으로 듣고 포트를 공개하지 않는다. 노트북 기본값은 127.0.0.1:8080.
환경 변수 SIMAI_RISK_ENGINE_HOST·SIMAI_RISK_ENGINE_PORT 로도 정한다. 비밀 값은 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.risk_engine",
        description="Risk Engine 컨테이너 내부 API: POST /v1/plan/evaluate (PLANNED 판정), GET /healthz",
    )
    parser.add_argument("--host", default=os.environ.get("SIMAI_RISK_ENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIMAI_RISK_ENGINE_PORT", "8080")))
    args = parser.parse_args(argv)

    import uvicorn  # --help 는 uvicorn·fastapi 없이도 된다

    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, server_header=False, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
