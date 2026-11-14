package com.ssafy.simai.domain.gpu.provider;

import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Component;

/**
 * 공급자 목록(S15P31A101-702). 빈으로 등록된 GpuProvider 를 id 로 찾는다. 지금은 RUNPOD 하나이고 AWS(-703)는 구현 빈을 더하면
 * 같은 화면·같은 경로(설정의 provider)로 쓰인다. 구현이 없는 공급자는 비어 있다(화면은 "설정되지 않음").
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
