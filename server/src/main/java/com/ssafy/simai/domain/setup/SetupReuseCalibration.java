package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.calibration.CalibrationService;
import com.ssafy.simai.domain.calibration.CalibrationViews;
import java.time.Clock;
import java.util.Optional;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;

/**
 * 이전 맵 재사용 세팅 완료가 구성 묶음에 실을 보정(S15P31A101-714, docs/design/S15P31A101-714-reuse-chosen-map.md).
 * <p>재사용 = 고른 맵을 만든 보정을 그대로 쓴다. Tier 0 가 지금 그 보정(또는 같은 내용의 보정)을 쓰고 있으면 그 버전을 그대로 싣는다.
 * 다른 보정을 쓰고 있으면, Tier 0 는 저장소에 없는 보정을 "이전 버전 = 지금 활성 보정" 계보로만 받으므로(-467) 그 맵의 보정을 계보 키만 바꾼 새 버전으로
 * 다시 낸다({@link CalibrationService#reissueForReuse}). 카메라 값·World 는 한 글자도 바꾸지 않는다(content_hash 가 같다). GPU 가 꺼져 있었으면
 * Tier 0 는 다음 기동 때 가장 최근 묶음의 보정(이 버전)으로 시작한다(GpuStartConfig).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupReuseCalibration {

    private final CalibrationService calibrations;
    private final CalibrationViews views;
    private final Clock clock;

    SetupReuseCalibration(CalibrationService calibrations, CalibrationViews views, Clock clock) {
        this.calibrations = calibrations;
        this.views = views;
        this.clock = clock;
    }

    /**
     * @param sourceCalibration 고른 맵을 만든 보정
     * @return 구성 묶음에 실을 보정 버전(그 보정, Tier 0 가 쓰는 같은 내용의 보정, 또는 새로 낸 같은 내용의 버전)
     */
    String forCompletion(String siteId, UUID setupSessionId, String sourceCalibration) {
        Optional<String> current = views.currentVersion();
        if (current.isPresent() && calibrations.sameContent(current.get(), sourceCalibration)) {
            return current.get();
        }
        String version = sourceCalibration + "-reuse-" + setupSessionId.toString().substring(0, 8);
        return calibrations.reissueForReuse(siteId, sourceCalibration, version, current.orElse(null),
                setupSessionId.toString(), clock.millis());
    }
}
