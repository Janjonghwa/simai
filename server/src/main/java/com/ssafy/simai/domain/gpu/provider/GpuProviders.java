package com.ssafy.simai.domain.gpu.provider;

import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Component;

/**
 * 공급자 목록(S15P31A101-702). 빈으로 등록된 GpuProvider 를 id 로 찾는다. RUNPOD(RunPodProvider)와 AWS(AwsGpuProvider, -703, 기본 꺼짐)가
 * 같은 화면·같은 경로(설정의 provider)로 쓰인다. 구현이 없거나 configured() 가 false 면 화면은 "설정되지 않음"·"준비 안 됨".
 */
@Component
public class GpuProviders {

    private final List<GpuProvider> providers;

    public GpuProviders(List<GpuProvider> providers) {
        this.providers = List.copyOf(providers);
    }

    public Optional<GpuProvider> find(String id) {
        return providers.stream().filter(p -> p.id().equals(id)).findFirst();
    }
}
