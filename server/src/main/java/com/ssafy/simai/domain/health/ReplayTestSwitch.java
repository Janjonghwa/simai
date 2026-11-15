package com.ssafy.simai.domain.health;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 녹화 재생 시험 스위치(S15P31A101-736, docs/design/S15P31A101-736-replay-test-switch.md). AGENTS 규칙 5(LIVE 만 실제 알림)의 시험용 예외다.
 * 본 시연은 라이브 카메라이고 녹화 재생은 리허설·시험용이다. 화면 버튼은 없고 EC2 .env(compose)로만 켠다. 기본은 둘 다 꺼짐.
 * <ul>
 *   <li>{@code SIMAI_REPLAY_TEST_ALERTS}: 켜면 노트북 하트비트가 녹화 파일·합성 영상 입력(-724 시험 입력)을 알리는 동안에도 워치 진동을
 *       라이브처럼 보낸다(-724 의 EC2 차단을 풀기만 한다). 관제 화면은 viewer_health 1.7.0 {@code laptop.replay_test_alerts} 로 「재생 시험 ·
 *       실제 알림 켜짐」을 보인다. 현장 경보 단말 소리는 단말 .env 의 같은 이름 스위치가 따로 정한다.
 *   <li>{@code SIMAI_REPLAY_TEST_SKIP_CAMERA_CHECK}: 켜고 노트북 카메라 입력이 모두 녹화 파일(FILE)일 때만 세팅 카메라 확인 걷기를 건너뛰게
 *       한다(job_lease_status 2.4.0 {@code camera_check_skip}, 완료 관문이 SKIPPED_REPLAY 를 통과로 본다). 실제 카메라(RTSP)가 하나라도
 *       있으면 켜져 있어도 걷기 확인 그대로다.
 * </ul>
 * HealthState 는 웹 앱에서만 있다. 없으면(웹이 아닌 시험 문맥) 입력을 모르는 것이라 건너뛰지 않는다.
 */
@Component
public class ReplayTestSwitch {

    private static final Logger log = LoggerFactory.getLogger(ReplayTestSwitch.class);

    private final boolean alerts;
    private final boolean skipCameraCheck;
    private final ObjectProvider<HealthState> health;

    public ReplayTestSwitch(@Value("${simai.replay-test.alerts:false}") boolean alerts,
            @Value("${simai.replay-test.skip-camera-check:false}") boolean skipCameraCheck, ObjectProvider<HealthState> health) {
        this.alerts = alerts;
        this.skipCameraCheck = skipCameraCheck;
        this.health = health;
        if (alerts) {
            log.warn("재생 시험 알림 스위치가 켜져 있다(SIMAI_REPLAY_TEST_ALERTS): 녹화 재생 입력의 위험 이벤트도 워치로 실제 진동을 보낸다. "
                    + "본 시연(라이브) 전에 끈다");
        }
        if (skipCameraCheck) {
            log.warn("재생 시험 걷기 확인 생략 스위치가 켜져 있다(SIMAI_REPLAY_TEST_SKIP_CAMERA_CHECK): 노트북 입력이 녹화 파일뿐이면 세팅 카메라 확인을 "
                    + "건너뛴다. 본 시연(라이브) 전에 끈다");
        }
    }

    /** 재생 시험 알림 스위치(켜면 시험 입력 중에도 EC2 워치 진동을 보낸다) */
    public boolean alerts() {
        return alerts;
    }

    /** 걷기 확인 생략 스위치 자체(입력 종류와 무관) */
    public boolean skipCameraCheck() {
        return skipCameraCheck;
    }

    /** 지금 세팅 카메라 확인 걷기를 건너뛰어도 되는가: 스위치가 켜져 있고 노트북 카메라 입력이 모두 녹화 파일이다 */
    public boolean cameraCheckSkipAllowed() {
        if (!skipCameraCheck) {
            return false;
        }
        HealthState state = health.getIfAvailable();
        return state != null && state.replayFileOnly();
    }
}
