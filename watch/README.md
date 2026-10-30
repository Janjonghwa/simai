# SIMAI 워치 앱 (Wear OS)

위험에 처한 작업자 본인에게 진동 알림을 주는 갤럭시워치 앱이다(기획서 부록 C.6, 확장). 지금은 뼈대만 있다:
EC2 주소·기기 토큰을 넣고 상태를 보는 화면. EC2 연결·진동·ACK 는 S15P31A101-337, 걸음 특징은 -339 에서 붙인다.
설계와 정한 값은 `docs/design/watch-app.md`.

- 대상: Wear OS 3 이상(minSdk 30). 확인한 기기는 갤럭시워치5·6
- Play 스토어에 올리지 않는다. 개발 PC 에서 ADB 로 바로 설치한다

## 준비 (PC 한 번)

1. Android Studio 를 설치한다. SDK 관리자에서 **Android SDK Platform 37** 과 **Platform-Tools** 를 받는다
   (Wear Compose 1.7 이 compileSdk 37 을 요구한다).
2. JDK 17 이상(서버와 같은 21 이면 된다). Android Studio 로 열면 내장 JDK 를 쓴다.
3. SDK 경로를 알린다. 둘 중 하나:
   - 환경 변수 `ANDROID_HOME=C:\Users\<사용자>\AppData\Local\Android\Sdk`
   - `watch/local.properties` 에 `sdk.dir=C\:\\Users\\<사용자>\\AppData\\Local\\Android\\Sdk` (저장소에 올리지 않는다)

## 빌드와 시험

```bash
cd watch
./gradlew assembleDebug testDebugUnitTest
# APK: app/build/outputs/apk/debug/app-debug.apk
```

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

디버그 서명 키는 PC 마다 다르다. 다른 PC 에서 빌드한 앱이 깔려 있으면 `adb uninstall com.ssafy.simai.watch` 뒤에 설치한다
(설정도 지워지므로 아래를 다시 넣는다).

## EC2 주소·기기 토큰 넣기

워치 키보드로 긴 토큰을 칠 수 없어 adb 로 넣는다. 주소와 토큰은 **함께** 넣어야 바뀐다.

```bash
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.ConfigActivity \
  --es ec2_base_url 'https://<EC2 주소>' --es device_token '<기기 토큰>'"
adb -s <IP:포트> shell "am start -n com.ssafy.simai.watch/.ConfigActivity --ez clear true"   # 지우기
```

- `adb shell` 은 인자를 워치의 셸이 다시 읽는다. 값은 위처럼 **작은따옴표**로 감싼다(`&`·`;`·`$` 가 든 토큰이 잘리지 않게).
  Windows PowerShell 5.1 은 `"` 를 adb 에 넘기기 전에 지우므로 Git Bash 에서 부른다
- 이 입구는 adb(shell)만 부를 수 있다(DUMP 권한). 워치의 다른 앱은 부르지 못한다
- `clear` 와 주소·토큰을 같이 주면 아무것도 바꾸지 않는다
- 주소는 `https://호스트` 만 받는다(계정·query·fragment·토큰이 든 주소는 거부, 노트북 게이트웨이와 같은 규칙)
- 결과는 워치 토스트와 `adb logcat -s SimaiWatchConfig` 로 본다. 토큰은 어디에도 찍히지 않는다
- 기기 토큰은 워치마다 따로 발급한다(NFR-12). 발급 API 는 S15P31A101-336 에서 만든다
- 토큰을 셸 기록에 남기지 않으려면 명령을 스크립트 파일에 두지 말고 매번 입력한다
