# SIMAI 워치 앱 (Wear OS)

위험에 처한 작업자 본인에게 진동 알림을 주는 갤럭시워치 앱이다(기획서 부록 C.6, 확장). EC2 주소·기기 ID·기기 토큰을 넣으면
포그라운드 서비스가 EC2 와의 I4 연결(wss, 계약 `contracts/messages/watch_*`)을 화면이 꺼져도 유지하고, 위험 알림(`watch_alert`)이
오면 등급별로 울리고 보이고 확인(`watch_ack`)을 보낸다(S15P31A101-390·-337). 걸음 특징(`gait_features`)은 -339 에서 붙인다.
설계와 정한 값은 `docs/design/watch-app.md`, 계약은 `docs/design/watch-i4-contract.md`.

- 대상: Wear OS 3 이상(minSdk 30). 확인한 기기는 갤럭시워치5·6
- Play 스토어에 올리지 않는다. 개발 PC 에서 ADB 로 바로 설치한다

## 준비 (PC 한 번)

1. Android Studio 를 설치한다. SDK 관리자에서 **Android SDK Platform 37** 과 **Platform-Tools** 를 받는다
   (Wear Compose 1.7 이 compileSdk 37 을 요구한다).
2. JDK 17 이상(서버와 같은 21 이면 된다). Android Studio 로 열면 내장 JDK 를 쓴다.
3. SDK 경로를 알린다. 둘 중 하나:
   - 환경 변수 `ANDROID_HOME=C:\Users\<사용자>\AppData\Local\Android\Sdk`
   - `watch/local.properties` 에 `sdk.dir=C\:\Users\<사용자>\AppData\Local\Android\Sdk` (저장소에 올리지 않는다)

## 빌드와 시험

```bash
cd watch
./gradlew assembleDebug testDebugUnitTest
# APK: app/build/outputs/apk/debug/app-debug.apk
```

단위 시험은 저장소의 계약 예시(`contracts/examples/watch_*.json`)를 읽으므로 저장소 안에서 돌린다.
Android Studio 에서는 `watch/` 폴더를 프로젝트로 연다(저장소 최상위가 아니다).

## 워치 연결 (기기마다 한 번)

PC 와 워치를 같은 Wi-Fi 에 둔다.

1. 워치 설정 → 워치 정보 → 소프트웨어 정보 → **소프트웨어 버전**을 "개발자 모드 켜짐" 이 뜰 때까지 여러 번 누른다
2. 설정 → 개발자 옵션 → **ADB 디버깅**, **무선 디버깅** 켜기
3. 무선 디버깅 → **새 기기 페어링** 에 나온 IP:포트·6자리 코드로 페어링한다

   ```bash
   adb pair <IP:페어링포트> <코드>
   adb connect <IP:연결포트>   # 무선 디버깅 첫 화면의 IP:포트(페어링 포트와 다르다)
   adb devices                  # 워치가 device 로 보이면 된다
   ```

   워치가 다시 켜지거나 Wi-Fi 가 바뀌면 연결 포트가 바뀐다. `adb connect` 만 다시 한다.

## 설치와 실행

```bash
adb devices                              # 여러 대면 -s <IP:포트> 로 고른다
adb -s <IP:포트> install -r app/build/outputs/apk/debug/app-debug.apk
adb -s <IP:포트> shell am start -n com.ssafy.simai.watch/.MainActivity
```

처음 열면 알림 권한을 묻는다. 허용해야 연결 상태 알림과 화면이 꺼진 채 오는 알림 화면이 보인다(거부해도 연결·진동은 된다).
adb 로 주려면 `adb -s <IP:포트> shell pm grant com.ssafy.simai.watch android.permission.POST_NOTIFICATIONS`.
디버그 서명 키는 PC 마다 다르다. 다른 PC 에서 빌드한 앱이 깔려 있으면 `adb uninstall com.ssafy.simai.watch` 뒤에 설치한다
(설정도 지워지므로 아래를 다시 넣는다).

## EC2 주소·기기 ID·기기 토큰 넣기

워치 키보드로 긴 토큰을 칠 수 없어 adb 로 넣는다. 세 값은 **함께** 넣어야 바뀐다. 저장하면 서비스가 바로 그 주소로 잇는다.

```bash
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.ConfigActivity \
  --es ec2_base_url 'https://<EC2 주소>' --es device_id watch-01 --es device_token '<기기 토큰>'"
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.ConfigActivity --ez clear true"   # 지우기(연결도 멈춘다)
```

- `adb shell` 은 인자를 워치의 셸이 다시 읽는다. 값은 위처럼 **작은따옴표**로 감싼다(`&`·`;`·`$` 가 든 토큰이 잘리지 않게).
  Windows PowerShell 5.1 은 `"` 를 adb 에 넘기기 전에 지우므로 Git Bash 에서 부른다
- 이 입구는 adb(shell)만 부를 수 있다(DUMP 권한). 워치의 서드파티 앱은 부르지 못한다
- `clear` 와 주소·ID·토큰을 같이 주면 아무것도 바꾸지 않는다
- 주소는 `https://호스트` 만 받는다(계정·query·fragment·토큰이 든 주소는 거부, 노트북 게이트웨이와 같은 규칙).
  debug 빌드는 아래 가짜 EC2 용으로 `http://` 도 받는다
- 기기 ID 는 EC2 에 등록한 `device_id`(소문자·숫자·`-`, 예 `watch-01`)로, I4 메시지의 `source_id` 가 된다. `ec2` 는 쓸 수 없다
- 결과는 워치 토스트와 `adb logcat -s SimaiWatchConfig` 로 본다. 토큰은 어디에도 찍히지 않는다
- 기기 토큰은 워치마다 따로 발급한다(NFR-12). 발급 API 는 S15P31A101-336 에서 만든다
- 토큰을 셸 기록에 남기지 않으려면 명령을 스크립트 파일에 두지 말고 매번 입력한다
- 0.1.0 에서 넣은 설정(기기 ID 없음)은 0.2.0 이 지우므로 다시 넣는다

## 연결 보기

상태 화면 첫 줄이 "연결됨 / 연결 중 / 연결 끊김 / 설정 안 됨" 이다. 아래로 스크롤하면 EC2 호스트·기기 ID, 마지막 EC2 하트비트 시각과
시계 차이, 배터리·전송 경로(WIFI·PHONE_BLUETOOTH·CELLULAR), 마지막 실패 이유가 나온다. 자세한 기록은 `adb logcat -s SimaiWatchI4`.

- 하트비트는 EC2 가 정한 주기(기본 10초)로 보내고, 3회 답이 없으면 끊고 1·2·4…30초 간격으로 다시 잇는다
- 워치가 다시 켜져도 설정이 있으면 스스로 잇는다

## 가짜 EC2 로 시험하기 (PC, debug 빌드)

EC2 쪽(-336·-338)이 없는 동안 PC 에서 I4 를 흉내 내는 서버로 연결·하트비트·알림·확인을 시험한다. 워치와 PC 는 같은 Wi-Fi 에 둔다.

```bash
# 저장소 루트에서(uv 가 의존성을 받는다). 토큰은 아무 값이나, 워치에 넣는 값과 같게
uv run watch/tools/fake_ec2.py --token dev-token
# 워치(debug 빌드)에 PC 주소를 넣는다
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.ConfigActivity \
  --es ec2_base_url 'http://<PC IP>:8765' --es device_id watch-01 --es device_token 'dev-token'"
```

서버 표준 입력에 `alert CRITICAL 추락 FRONT` / `alert HIGH 지게차` / `alert CAUTION 접근 - 10000` 을 치면 워치가 울린다.
`resend` 는 같은 알림을 다시 보낸다(워치는 다시 울리지 않고 확인만 다시 보낸다). 서버는 워치가 보내는 메시지를 계약으로 검사해
어긋나면 찍고, 확인(`watch_ack`)이 오면 전달에 걸린 시간을 찍는다. PC 방화벽이 8765 포트를 막으면 열어 준다.

## 걸음 특징 보기

연결돼 있는 동안 워치가 가속도를 50 Hz 로 받아 4초 창을 2초마다 `gait_features` 로 보낸다(S15P31A101-339). 가짜 EC2 는 창마다
`gait watch-01 [창 시작~끝) 걸음 7 rms 1.34 peak 1.85 offset -1386 지연 3549ms START@…` 로 찍고, `--gait-log <파일>` 을 주면 CSV 로도 남긴다
(열: 수신 시각, 기기, 창 시작·끝, 시계 차이, 걸음 수, RMS, 최대 주파수, 전환). 상태 화면 아래쪽에도 `걸음 7/4초 · 1.8Hz · 걷는 중` 이 보인다.

```bash
uv run watch/tools/fake_ec2.py --token dev-token --gait-log gait_log.csv
```

- 걷기는 창마다 걸음 6~8개·최대 주파수 1.7~2.0 Hz·RMS 1 안팎, 가만히 있으면 걸음 0·최대 주파수 없음·RMS 0.01 안팎이 정상이다
- 책상에 둔 채 가만히 있으면 워치가 약 20초 주기로 잠들어 창이 10여 초씩 비고 하트비트도 늦어진다. 착용하고 움직이면 2초마다 온다
- 센서 수집은 연결돼 있을 때만 돈다. 끊기면 센서도 멈춘다

## 가짜 알림으로 알림 화면 시험하기 (debug 빌드)

서버 없이 알림 화면·진동·확인 단추만 adb 로 시험한다. 알림은 연결 서비스로 들어가 실제 알림과 같은 길(보관·진동·확인·화면)을 지난다.

```bash
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.DebugAlertActivity --es severity CRITICAL --es word stop --es direction LEFT"
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.DebugAlertActivity --es severity HIGH --es needs_check true"
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.DebugAlertActivity --es severity CAUTION --es ack_within_ms 5000"
```

- `severity`(CRITICAL·HIGH·CAUTION)만 필요하다. 나머지는 `word`(기본 "시험", 8자 이하), `direction`(FRONT·BACK·LEFT·RIGHT),
  `needs_check`(true·false), `ack_within_ms`(기본 30000), `vibration_pattern`(DANGER_LONG·CAUTION_DOUBLE·CAUTION_WEAK, 없으면 등급으로 정함),
  `event_id`·`alert_msg_id`(기본 시각으로 만든 ID)다. 같은 `alert_msg_id` 로 다시 부르면 같은 알림이 한 번 더 온 것이다(울리지 않음)
- 한글 값(`--es word 멈춤`)은 Windows 의 adb 가 깨뜨릴 수 있다. 깨지면 영문 단어를 쓴다
- 위험은 확인하거나 기한이 지날 때까지 길게 되풀이해 울리고, 경고는 짧게 두 번, 주의는 약하게 한 번 울린다. 기한이 지나도 알림 화면과 확인 단추는 남는다
- 확인하면 "확인함 시각" 을 잠깐 보이고 닫는다. 확인 기록은 `adb logcat -s SimaiWatchI4`, 낸 진동 패턴은 `adb logcat -s SimaiWatchVibe` 로 본다
- 잘못된 값은 토스트와 `adb logcat -s SimaiWatchDebugAlert` 로 이유가 나온다
- 화면이 꺼져 있어도 진동과 확인(VIBRATED)은 바로 된다. 알림 화면은 앱이 앞에 있으면 바로 뜨고, 꺼져 있으면 전체 화면 인텐트 알림으로
  올라가 손목을 들거나 알림을 누르면 뜬다(갤럭시워치6 확인). 전체 화면 인텐트는 사이드로드 앱이라 기본 거부라 한 번 켜 준다:
  `adb -s <IP:포트> shell appops set com.ssafy.simai.watch USE_FULL_SCREEN_INTENT allow`
  (또는 설정 → 앱 → SIMAI 워치 → 전체 화면 알림). 에뮬레이터는 `adb shell input keyevent 223` 로 화면을 끈 뒤 시험한다

## 기본 화면의 이름·구역 넣기 (debug 빌드)

기본 화면 첫 줄은 큰 시계이고, 그 아래 이름·구역은 값이 있을 때만 나온다. 값을 줄 서버 계약이 없는 동안 adb 로 넣는다.

```bash
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.DebugWorkerActivity --es name 김반장 --es zone '3층 A구역'"
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.DebugWorkerActivity --ez clear true"   # 지우기
```

- 이름과 구역은 함께 줄 때만 바꾼다. 각각 12자 이하이고 비워 둘 수 없다
- 결과는 워치 토스트와 `adb logcat -s SimaiWatchDebugWorker` 로 본다
- 연결 상태·마지막 알림·개발용 값은 기본 화면을 아래로 스크롤하면 나온다
- 손목을 내리면 어둡게 켜져 있는 화면(상시표시)이 되어 시계와 이름·구역만 남는다. 워치 설정의 "항상 켜짐"이 켜져 있어야 한다.
  에뮬레이터는 `adb shell input keyevent 223` 로 화면을 끄고(진입) `224` 로 켜서(복귀) 시험하고, 진입·복귀는 `adb logcat -s SimaiWatchAmbient` 로 본다
