# 노트북 gateway 앱의 WinError 10014 대응 (S15P31A101-199)

이 노트북(Windows 11, AhnLab V3 설치)의 Winsock 은 bind·listen·connect·socketpair 가 제품 코드와 상관없이 가끔
`OSError: [WinError 10014]`(WSAEFAULT)로 실패하고, 새로 하면 바로 된다(S15P31A101-198 측정: 유휴 116 s 중 21 s 에 1회, 몰릴 때
30~45%). 원인 후보는 V3 네트워크 필터(미확인)다. 관제 중(명세 3.2 노트북 하트비트 1 s·누락 3 s, 12.7 전송·재접속)에 이 오류로
연결이 끊기거나 앱이 죽지 않게 한다. 시험 쪽 보정(`ai/tests/winsock_retry/sitecustomize.py`, 전역 교체)과 달리 제품 코드는
호출 지점에서 명시적으로 감싼다.

## 공용 함수

`gateway/site_alert/src/simai_site_alert/winsock.py` 의 `retry_wsaefault(fn, what=...)`.

- Windows(`sys.platform == "win32"`) 에서 `winerror == 10014` 인 OSError 만 다시 부른다. 다른 오류·다른 OS 는 바로 올린다.
- 기다림 0.01·0.02·…·최대 0.05 s, 기다림 합계 예산 0.3 s(약 9번). 다 쓰면 마지막 10014 를 올리고 WARNING 로그를 남긴다.
  다시 시도해 성공하면 INFO 로그(`WinError 10014 를 n번 다시 시도해 성공했다`)로 빈도를 셀 수 있다.
- fn 은 매번 새 소켓으로 처음부터 한다(실패한 소켓의 connect 를 되풀이하지 않는다).
- 위치: relay 는 이미 `simai-site-alert` 를 경로 의존으로 쓴다(endpoint·pinning·relay_status). 그래서 site_alert 에 두면 관제 경로
  두 앱이 의존성을 늘리지 않고 같은 코드를 쓴다. recorder·rtsp_probe 는 Python 소켓을 직접 열지 않아(PyAV 내부) 필요 없다.
  공용 패키지를 새로 만드는 것은 함수 하나에 비해 과하다.

### 예산 근거

| 기준 | 값 | 0.3 s 와 비교 |
|---|---|---|
| 노트북 → Tier 0·EC2 하트비트, 중계 상태 보고 주기 | 1 s | 짧다: 한 주기도 빠지지 않는다 |
| 하트비트·중계 상태 누락 판정 | 3 s | 짧다 |
| 단말 표시 tick | 0.5 s | 짧다 |
| 영상 송신 큐 상한 | 25 프레임 = 25 fps 에서 1 s | 짧다: 다시 시도만으로는 큐가 넘치지 않는다 |
| 기존 재접속 첫 대기(relay·제어 채널) | 0.5 s | 짧다: 재접속보다 먼저 끝난다 |

-198 의 시험 보정은 20번·약 1.5 s 다. 시험은 끝까지 성공해야 하지만, 제품은 예산을 넘기면 기존 재접속이 다시 시도하므로 판정·
표시 주기보다 짧게 잡았다. 실패한 호출 하나가 오래 걸리면(connect 시간 제한) 합계는 예산 + 호출 한 번으로 묶인다.

이 노트북 측정(2026-10-19, 아래 빈도 재기 반복으로 몰린 상태): 감싸지 않은 bind·listen·socketpair 2000회 중 10014 605회(30%).
같은 묶음을 `retry_wsaefault` 로 1000회 감싸면 예산을 다 쓴 것 0회, 한 호출 최장 0.235 s.

## 경로 목록

| 앱 | 경로 | 소켓 호출 | 10014 때 이전 | 지금 |
|---|---|---|---|---|
| relay | 카메라별 Tier 0 영상 연결 `tls_sink._open` | socket·connect | connect_failures + 재접속(0.5 s~) | 새 소켓으로 다시 시도. 예산을 넘기면 이전과 같다(DISCONNECTED, 하트비트 video_cameras 에서 빠짐) |
| relay | 같은 연결의 TLS 핸드셰이크·HELLO | wrap_socket·send·recv | 재접속 | 그대로(핸드셰이크 중간 상태는 다시 쓸 수 없다) |
| relay | 연결 중 송수신 `_send_all`·`_check_incoming` | send·recv·select | 끊김 → 재접속 | 그대로 |
| relay | 중계 상태 보고 `status_report` | UDP sendto | 무시, 다음 1 s 주기 | 그대로(3 s 넘게 빠지면 단말이 관제 중단 표시) |
| relay | Tier 0 엔드포인트 조회 `EndpointWatcher` | urllib(http.client) connect | 이전 엔드포인트 유지, 다음 주기 | 그대로 |
| site_alert | 제어 채널 이벤트 루프 생성 `transport.start` | socketpair(Proactor 자체 파이프) | **루프 스레드가 죽고 start() 가 영원히 기다림(앱 멈춤)** | 다시 시도. 예산을 넘기면 start() 는 돌아오고 연결 안 됨(관제 중단 표시)으로 두며 재접속 간격마다 루프를 다시 만든다 |
| site_alert | 제어 채널 접속 `websockets.connect` | 루프의 connect·TLS | 재접속(0.5 s 부터 두 배) | 그대로(비동기 경로, 재접속에 맡김) |
| site_alert | 중계 상태 포트 `RelayStatusReceiver` | socket·bind | 실행 내내 영상 상태 모름(관제 중단 표시) | 다시 시도. 예산을 넘기면 이전과 같다 |
| site_alert | 중계 상태 수신 | recvfrom | 무시하고 계속 받음 | 그대로 |
| site_alert | EC2 하트비트·엔드포인트 조회 | urllib connect | 실패 결과, 다음 1 s 주기 | 그대로 |
| recorder·rtsp_probe | RTSP 수신 | PyAV(FFmpeg) 내부 | 열기 실패 → 기존 재접속 | 범위 밖, 기존 재접속에 맡김 |
| relay·site_alert | `bench` 측정 명령 | listen·asyncio.run | 측정 실패 | 범위 밖(관제 경로 아님) |

## V3 영향 확인 (설정은 바꾸지 않았다)

1. Winsock 공급자 확인: 관리자 명령 창에서 `netsh winsock show catalog > catalog.txt` 를 저장하고 `findstr /i ahnlab catalog.txt`
   로 AhnLab 계층 공급자(LSP)가 끼어 있는지 본다. 없으면 V3 는 WFP 필터 드라이버로 붙는 것이라 `fltmc` 나 V3 의 네트워크 보호
   켜기·끄기로만 가를 수 있다.
2. 예외 위치: V3 환경 설정의 네트워크 보호(웹 보호·네트워크 침입 차단) 항목의 예외(프로그램·포트) 목록. 대상은
   `gateway/*/.venv/Scripts/python.exe`. 제품·버전마다 메뉴 이름이 다르고, 기관 관리형이면 중앙 정책이라 관리자에게 요청한다.
3. 빈도 재기(예외 전후 같은 조건으로): 아래를 유휴 상태에서 3회 돌려 10014 수를 적는다. 관제 중에는 relay·site_alert 로그의
   `WinError 10014 를 … 다시 시도` 줄 수를 시간당으로 센다.

```powershell
uv run python -c "import socket,collections as c;n=c.Counter()
for _ in range(2000):
  s=socket.socket()
  try: s.bind(('127.0.0.1',0)); s.listen(); a,b=socket.socketpair(); a.close(); b.close()
  except OSError as e: n[getattr(e,'winerror',None)]+=1
  finally: s.close()
print(dict(n))"
```

## 남은 위험

- 비동기 제어 채널 접속(websockets)과 TLS 핸드셰이크는 다시 시도하지 않고 기존 재접속에 맡긴다. 접속이 끊긴 동안은 관제 중단으로
  보인다(조용히 정상으로 보이지 않음).
- 중계 상태 포트가 예산을 넘겨 열리지 않으면 실행 내내 영상 상태 모름이다(이전과 같음). 다시 여는 주기는 만들지 않았다.
- V3 가 원인인지는 확인하지 않았다(위 방법으로 예외 전후 빈도를 재야 한다).
