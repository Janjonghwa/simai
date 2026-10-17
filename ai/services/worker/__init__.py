"""I5 분석 워커 조립: EC2 에서 작업을 대여해 가져와(HTTPS long-poll) 기능 패키지의 처리기에 넘기고 결과를 제출한다.

계약은 contracts/messages/job_lease_request·job_request·job_heartbeat·job_callback·job_lease_status 다(명세 12.5).
"""
