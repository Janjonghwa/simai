"""EC2 Risk Engine 컨테이너 조립: Spring 이 compose 내부망으로 보내는 PLANNED 판정 요청을 받는 FastAPI 내부 API (명세 3.2, 3.8, 12.2).

계약은 contracts/messages/risk_plan_request·risk_plan_result 1.0.0 이다. 판정·변환·위치 생성은 simai_ai.risk.plan(Tier 0 와 같은
라이브러리)이 하고, 여기는 본문 읽기·스키마 검증·오류 판정 순서·HTTP 상태만 둔다. 알림 채널·저널·DB·외부 네트워크는 쓰지 않는다
(pyproject import-linter `risk-engine-no-channels`). 이미지는 deploy/risk-engine, 설계는 docs/design/risk-engine-container.md.
"""
