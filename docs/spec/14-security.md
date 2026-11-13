# 14. 보안

외부로 열리는 모든 지점에 인증을 걸고, 모든 외부 통신을 TLS로 암호화한다. 특히 GPU 실행 사이트(RunPod 등, 3.12)의 포트는 공개 IP로 열리므로 인증 없이 두지 않는다.

| 대상 | 정책 |
|---|---|
| EC2 공개 주소 | HTTPS 인증서, 필요한 포트만 개방. Spring Boot Actuator 와 DB 포트는 외부에 열지 않는다. 관리 포트는 IP 제한 |
| 뷰어 | 계정 로그인, 역할 3종(관리자, 반장, 열람). 실시간 관제 화면 내 구역·작업면 편집 모드 진입·발행(`POST /api/zones/commit`), 규칙·현장 규칙 프로파일·레이아웃 수정, 작업 시작·종료(`POST /api/work-units/{id}/start`·`/end`), 사람·워치 운영 표시 이름 설정·초기화(`PUT·DELETE /api/watches/{deviceId}/display-name`, `/api/people/{worldId}/display-name`, 12.6·13.2), TBM 보고서 참석자 전자서명 등록·조회(`/api/reports/tbm/{reportId}/signatures`), 이벤트 클립 및 작업 구간 녹화 조각 열람(`/api/clips/...`, `/api/videos/...`), 관제 화면 CCTV 원격 시청(`POST /api/cctv/remote/tickets` 로 유효 60 s 일회용 입장권 발급 후 `wss://<ec2>/ws/cctv/view?ticket=…` 접속, v1.3 수정 46)은 관리자(`ADMIN`)만, 아바타 자산(`/api/avatars/...`, 같은 출처 경로)·`viewer_health`(`person_names`·`watch_names` 포함)와 STOMP 토픽(`/topic/avatar`·`/topic/health` 포함) 조회는 관제 앱(열람 이상) 인증. MVP 는 공사장 1개라 계정을 공사에 묶지 않는다(기업·공사 계층은 부록 C.9) |
| 노트북 게이트웨이 | 기기 토큰. 영상은 카메라별 TLS over TCP(Tier 0 공개키 고정), 제어는 wss, EC2 하트비트·엔드포인트 조회·세팅 1단계 각도 헬퍼 제출·관제 종료 뒤 완료 작업 구간 조회 및 녹화 조각 업로드(`GET /api/devices/recordings/windows`, `POST /api/uploads/sign` `RECORDING_SEGMENT`, `POST /api/devices/recordings/segments`, 5.1·12.6)는 HTTPS, EC2 원격 CCTV 송출(`wss://<ec2>/ws/devices/cctv`, 보는 관리자가 있을 때만 프레임 전송, v1.3 수정 46)은 wss + 기기 토큰 헤더 |
| TLS 서버 확인 | Tier 0 TLS 키는 한 번 만들어 Tier 0 저장 위치의 비공개 폴더(3.12)에 둔다. 노트북은 그 공개키 지문만 신뢰한다(또는 팀 사설 CA). 인증서 검증을 끄는 구현은 금지한다(12.7) |
| 기기 토큰 | 기기마다 따로 발급한다. 주기적으로 회전하고(주기 확인 필요), 분실·교체한 기기의 토큰은 EC2 에서 폐기한다 |
| GPU 실행 사이트(RunPod·Vast·시연 사이트) | 템플릿의 기본 공개 포트(Jupyter 등)는 닫는다. SSH 는 키 인증만(VM 처럼 호스트 SSH 를 쓰면 컨테이너 SSH 는 끈다). 영상 수신은 인스턴스 공개 TCP 포트(Direct TCP)에서 Tier 0 서비스가 TLS를 직접 종료하고 노트북 기기 토큰으로 인증(12.7). 분석 워커는 EC2 로 먼저 접속해 작업을 가져가고(I5, 12.5) 아바타 스트림(I6 `wss://<ec2>/ws/worker/avatar`, 12.2)과 자산 업로드 주소 발급을 열므로 워커로 들어오는 포트가 없다. 워커 연결은 모두 TLS + 워커 토큰 |
| 외부 LLM API | EC2 의 Python 컨테이너(`deploy/risk-engine`) 안에서만 호출하며(API 키 `SIMAI_LLM_API_KEY` 는 EC2 `.env`, v1.3 수정 49), 텍스트(규칙 계산 결과·법령 지식 그래프 조항·이벤트 요약·비개인정보 공종)만 보낸다. 영상·이미지·얼굴·실명(TBM 보고서 참석자 전자서명 `signer_name` 및 서명 이미지, 관제 화면 운영 표시 이름 `person_names`·`watch_names` 포함)은 절대 보내지 않는다(5.4, 8.3, 11.5) |
| 카메라 RTSP 계정 | 카메라별 별도 계정, 교실망 안에서만 접근. Tapo 클라우드 외부 접속은 차단 |
| 비밀 값 | API 키(LLM 포함)·토큰·계정·Tier 0 TLS 개인키는 저장소에 넣지 않고 `.env`·비밀 저장소로 관리 |
| 객체 저장소 | 비공개, 임시 서명 주소로만 업로드·조회 |
| 개인 정보 | 이벤트 클립 및 관제 종료 뒤 올린 작업 구간 녹화 조각(`video_segment`, 첫 프레임 + 14일 자동 삭제, 보존 표시 없음, 5.1·11.5) 열람은 관리자(`ADMIN`)로 제한하고 조회 기록(`clip_access_log`·`video_access_log`)을 남긴다. 관제 화면 원격 CCTV 실시간 영상은 관리자만 열람하며 EC2 에 저장하지 않는다(3.3, v1.3 수정 46). 위치(`world_track_state`)·이벤트(`risk_event`)·작업명(`work_unit`)·담당 조 정본에는 사람 이름을 넣지 않되(NFR-14), ① TBM 보고서 참석자 전자서명(`report_signature.signer_name` 및 손글씨 서명 이미지)은 내부 안전 교육 참석 기록용으로 서버에만 저장하고 관리자(`ADMIN`)만 조회하며(`"공인 전자서명 아님, 내부 기록용 전자 서명"` 고지, 8.3·v1.3 수정 52), ② 관제 화면 식별 보조용 사람·워치 운영 표시 이름은 별도 추가 전용 이력 표(`watch_display_name_history`·`person_world_name_history` 등 `V65`)에만 두어 관리자(`ADMIN`)만 변경·초기화하고 로그인한 관제 화면(`VIEWER` 이상)에서만 열람하며(11.4-5·13.2, `docs/design/S15P31A101-658-person-name-label.md`), 둘 다 외부 LLM 에 절대 보내지 않고 프로젝트 발표 후 30일 안에 파기한다(실제 현장 운영 시 서명 보존 기간은 Lead 확인 필요) |
| Unreal MCP(확장) | 에디터 루프백만 허용, 빌드 워커 툴 범위 제한 |
