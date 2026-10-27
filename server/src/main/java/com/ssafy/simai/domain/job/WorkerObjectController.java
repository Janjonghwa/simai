package com.ssafy.simai.domain.job;

import com.ssafy.simai.domain.job.JobObjectSignService.SignRequest;
import com.ssafy.simai.domain.job.JobObjectSignService.SignedObject;
import java.time.Clock;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 분석 워커 객체 서명(워커 토큰, 명세 12.5·14, S15P31A101-248). 대여 중인 작업의 input 객체를 받고(GET) 결과를 올릴(PUT)
 * 임시 서명 주소를 낸다. 본문은 I5 봉투가 아니라 이 API 전용 JSON 이다(계약 메시지가 아니라 서버 OpenAPI 가 정본).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class WorkerObjectController {

    private final JobObjectSignService objects;
    private final Clock clock;

    WorkerObjectController(JobObjectSignService objects, Clock clock) {
        this.objects = objects;
        this.clock = clock;
    }

    @PostMapping("/api/worker/jobs/{jobId}/objects/sign")
    SignedObject sign(@PathVariable String jobId, @RequestBody SignRequest request, @AuthenticationPrincipal String workerId) {
        return objects.sign(workerId, jobId, request, clock.instant());
    }
}
