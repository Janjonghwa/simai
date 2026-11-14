package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.ssafy.simai.domain.gpu.GpuCacheService.CacheSign;
import com.ssafy.simai.domain.gpu.GpuCacheService.SignRequest;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.properties.StorageProperties;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * 16차 GPU 세션(S15P31A101-710): 서버 키에 ListBucket 이 없으면 없는 객체의 HEAD 가 404 대신 403 이다. 가짜 S3(HTTP)가 HEAD 에 403 을 줄 때
 * GPU 캐시는 GET=miss, PUT=조건부(If-None-Match: *) 서명을 주고, 다른 경로의 head 는 그대로 오류(STORAGE_UNAVAILABLE)다.
 * 실제 ObjectStorage(AWS SDK)를 쓴다. 서명은 로컬 계산이라 가짜 S3 에는 HEAD 만 온다.
 */
class GpuCacheForbiddenHeadTest {

    private static final String BUCKET = "simai-test";
    private static final String SHA = "ab".repeat(32);

    private HttpServer s3;
    private ObjectStorage storage;
    private GpuCacheService cache;
    /** 키(버킷 뒤 경로) → HEAD 응답 코드. 없으면 403 */
    private final Map<String, Integer> headStatus = new ConcurrentHashMap<>();
    private final List<String> heads = new CopyOnWriteArrayList<>();

    @BeforeEach
    void setUp() throws Exception {
        s3 = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        s3.createContext("/", exchange -> {
            String path = exchange.getRequestURI().getPath();
            String key = path.startsWith("/" + BUCKET + "/") ? path.substring(BUCKET.length() + 2) : path;
            if ("HEAD".equals(exchange.getRequestMethod())) {
                heads.add(key);
            }
            exchange.sendResponseHeaders(headStatus.getOrDefault(key, 403), -1);
            exchange.close();
        });
        s3.start();
        storage = new ObjectStorage(new StorageProperties(BUCKET, "ap-northeast-2",
                "http://127.0.0.1:" + s3.getAddress().getPort(), true, "test-access", "test-secret", null, null));
        GpuSessionService sessions = mock(GpuSessionService.class);
        when(sessions.openSessionForWorker("worker-1")).thenReturn(true);
        cache = new GpuCacheService(storage, sessions);
    }

    @AfterEach
    void tearDown() {
        storage.close();
        s3.stop(0);
    }

    @Test
    void HEAD_403_이면_GET_은_miss() {
        CacheSign sign = cache.sign("worker-1", new SignRequest("trt-engine", "nvidia-h100/sm_90/0123456789abcdef", "GET", null, null));

        assertThat(sign.hit()).isFalse();
        assertThat(sign.url()).isNull();
        assertThat(sign.objectKey()).isEqualTo("job-objects/gpu_cache/trt-engine/nvidia-h100/sm_90/0123456789abcdef.tar");
        assertThat(heads).containsExactly("job-objects/gpu_cache/trt-engine/nvidia-h100/sm_90/0123456789abcdef.tar");
    }

    @Test
    void HEAD_403_이면_PUT_은_덮어쓰지_않는_조건부_서명() {
        CacheSign sign = cache.sign("worker-1", new SignRequest("reid", "cope-1", "PUT", 10_000L, SHA));

        assertThat(sign.hit()).isFalse();
        assertThat(sign.method()).isEqualTo("PUT");
        assertThat(sign.objectKey()).isEqualTo("job-objects/gpu_cache/reid/cope-1.tar");
        assertThat(sign.url()).contains("/" + BUCKET + "/job-objects/gpu_cache/reid/cope-1.tar");
        assertThat(sign.headers()).anySatisfy((name, value) -> {
            assertThat(name).isEqualToIgnoringCase("if-none-match");
            assertThat(value).isEqualTo("*");
        });
    }

    @Test
    void HEAD_404_도_없음이고_다른_경로의_head_는_403_을_오류로_본다() {
        headStatus.put("job-objects/gpu_cache/reid/cope-2.tar", 404);
        assertThat(cache.sign("worker-1", new SignRequest("reid", "cope-2", "GET", null, null)).hit()).isFalse();

        assertThat(storage.headOrAbsentIfForbidden("recordings/x.mp4")).isEmpty();
        assertThatThrownBy(() -> storage.head("recordings/x.mp4")).isInstanceOf(BusinessException.class);
    }
}
