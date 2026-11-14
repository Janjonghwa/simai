package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.domain.gpu.GpuCacheService.CacheSign;
import com.ssafy.simai.domain.gpu.GpuCacheService.SignRequest;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.CacheControl;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/** GPU 캐시 받기·올리기 서명(워커 토큰, S15P31A101-700). 응답의 서명 주소는 캐시하지 않는다 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class GpuCacheController {

    private final GpuCacheService cache;

    GpuCacheController(GpuCacheService cache) {
        this.cache = cache;
    }

    @PostMapping(GpuPaths.CACHE_SIGN)
    ResponseEntity<CacheSign> sign(@RequestBody SignRequest request, @AuthenticationPrincipal String workerId) {
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).body(cache.sign(workerId, request));
    }
}
