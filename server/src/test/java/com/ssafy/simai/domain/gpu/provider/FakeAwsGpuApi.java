package com.ssafy.simai.domain.gpu.provider;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;

/** 가짜 AWS(S15P31A101-703). 실제 AWS 를 부르지 않고 요청을 남기고 정해 둔 답을 준다 */
public class FakeAwsGpuApi implements AwsGpuApi {

    public volatile boolean credentials = true;
    public final Map<String, Set<String>> zones = new LinkedHashMap<>();
    public final Map<String, GpuSpec> specs = new HashMap<>();
    public final Map<String, String> subnets = new LinkedHashMap<>();
    public final Map<String, BigDecimal> spot = new HashMap<>();
    public final Map<String, BigDecimal> onDemand = new HashMap<>();
    /** 이 종류의 Price List 조회는 실패한다(설정 표로 내려가는지) */
    public final Set<String> pricingFails = new java.util.HashSet<>();
    public final List<RunRequest> runs = new CopyOnWriteArrayList<>();
    public final List<String> terminated = new CopyOnWriteArrayList<>();
    public final List<String> calls = new CopyOnWriteArrayList<>();
    public volatile GpuProviderException runFailure;
    public volatile String nextInstanceId = "i-0123456789abcdef0";
    /** 인스턴스 ID → 상태 이름(없으면 DescribeInstances 에 없음) */
    public final Map<String, String> states = new HashMap<>();

    @Override
    public boolean credentialsAvailable() {
        return credentials;
    }

    @Override
    public Map<String, Set<String>> offeredZones(List<String> instanceTypes) {
        calls.add("offeredZones " + instanceTypes);
        Map<String, Set<String>> out = new LinkedHashMap<>();
        instanceTypes.forEach(t -> {
            if (zones.containsKey(t)) {
                out.put(t, zones.get(t));
            }
        });
        return out;
    }

    @Override
    public Map<String, GpuSpec> gpuSpecs(List<String> instanceTypes) {
        return new HashMap<>(specs);
    }

    @Override
    public Map<String, String> subnetZones(List<String> subnetIds) {
        calls.add("subnetZones " + subnetIds);
        Map<String, String> out = new LinkedHashMap<>();
        subnetIds.forEach(s -> {
            if (subnets.containsKey(s)) {
                out.put(s, subnets.get(s));
            }
        });
        return out;
    }

    @Override
    public Map<String, BigDecimal> spotPrices(List<String> instanceTypes) {
        calls.add("spotPrices " + instanceTypes);
        return new HashMap<>(spot);
    }

    @Override
    public Optional<BigDecimal> onDemandPrice(String instanceType) throws GpuProviderException {
        calls.add("onDemandPrice " + instanceType);
        if (pricingFails.contains(instanceType)) {
            throw new GpuProviderException("REJECTED", "AWS 온디맨드 가격 조회 거절(AccessDeniedException, HTTP 400)");
        }
        return Optional.ofNullable(onDemand.get(instanceType));
    }

    @Override
    public String run(RunRequest request) throws GpuProviderException {
        calls.add("run " + request.instanceType() + " " + request.availabilityZone());
        if (runFailure != null) {
            throw runFailure;
        }
        runs.add(request);
        states.put(nextInstanceId, "pending");
        return nextInstanceId;
    }

    @Override
    public void terminate(String instanceId) {
        calls.add("terminate " + instanceId);
        terminated.add(instanceId);
        states.put(instanceId, "shutting-down");
    }

    @Override
    public Optional<String> state(String instanceId) {
        calls.add("state " + instanceId);
        return Optional.ofNullable(states.get(instanceId));
    }

    public List<String> callsStartingWith(String prefix) {
        List<String> out = new ArrayList<>();
        calls.forEach(c -> {
            if (c.startsWith(prefix)) {
                out.add(c);
            }
        });
        return out;
    }
}
