package com.ssafy.simai.domain.upload;

import com.ssafy.simai.domain.upload.UploadSignService.SignRequest;
import com.ssafy.simai.domain.upload.UploadSignService.SignedUpload;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 사진·프레임·클립 임시 업로드 주소(명세 12.6 "기기·반장", S15P31A101-69). 기기 토큰·Tier 0 토큰·반장 JWT 를 같은 경로에서 받는다
 * (SecurityConfig 업로드 체인). 목적별로 받을 수 있는 주체가 다르다(UploadPurpose).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class UploadController {

    private final UploadSignService uploads;

    UploadController(UploadSignService uploads) {
        this.uploads = uploads;
    }

    @PostMapping("/api/uploads/sign")
    SignedUpload sign(@RequestBody SignRequest request, Authentication auth) {
        Requester who = Requester.of(auth).orElseThrow(() -> new BusinessException(ErrorCode.Auth.FORBIDDEN));
        return uploads.sign(who, request);
    }
}
