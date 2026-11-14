package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.domain.gpu.GpuActivity.Busy;
import com.ssafy.simai.domain.gpu.GpuSessionService.EndReason;
import com.ssafy.simai.domain.gpu.GpuSettingsService.GpuSettings;
import com.ssafy.simai.domain.gpu.provider.GpuProvider;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.CreatedInstance;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.GpuOffer;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.InstanceState;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.LaunchSpec;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.Network;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.OfferQuery;
import com.ssafy.simai.domain.gpu.provider.GpuProviderException;
import com.ssafy.simai.domain.gpu.provider.GpuProviders;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.ConfigBundleProperties;
import com.ssafy.simai.global.properties.GpuProperties;
import java.math.BigDecimal;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 켜기·끄기·자동 정지(S15P31A101-702, 설계 docs/design/S15P31A101-702-gpu-provider-runpod.md, 명세 3.12).
 * <ul>
 * <li>한 번에 GPU 하나: 열린 G1 세션이나 끝나지 않은 켠 요청(gpu_launch)이 있으면 켜지 않는다. 확인과 요청 기록은 advisory 잠금 하나로 줄
 * 세우고, 공급자 호출은 잠금 밖에서 한다(요청 행이 다음 켜기를 막는다).</li>
 * <li>켠 요청은 공급자를 부르기 전에 한 번 쓰고 결과·끄기는 gpu_launch_event 에 덧붙인다(AGENTS 6). G1 세션과는 provider + instance_id 로 잇는다.</li>
 * <li>끄기: 관제(LIVE)·세팅 중이면 거부. 공급자 인스턴스를 지운 뒤 그 인스턴스의 열린 세션을 ADMIN_STOPPED 로 끝내 토큰을 바로 폐기한다.</li>
 * <li>자동 정지({@link #autoStop()}): LIVE·세팅·녹화 업로드·분석 작업 중에는 어떤 경우에도 끄지 않는다. 그 밖에 켠 뒤 등록 없음(bootTimeout),
 * 세션이 끝났는데 인스턴스가 남음, FAILED 지속(failedGrace, I1 없음), 유휴(설정의 분)면 끈다.</li>
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class GpuPowerService {

    /** 화면 점 하나의 상태 */
    public enum Power {
        OFF, STARTING, PREPARING, CONNECTED, ERROR
    }

    /** 켠 요청 상태(사건에서 만든다) */
    enum LaunchState {
        REQUESTED, RUNNING, FAILED, STOPPED
    }

    /** 켠 요청 한 건(가장 최근) */
    record Launch(UUID launchId, String provider, String gpuType, String region, String cloudType, boolean interruptible,
            BigDecimal pricePerHourUsd, boolean autoSelected, Long requestedBy, String requestedByLabel, Instant requestedAt,
            String instanceId, Instant createdAt, String endKind, String endReason, Instant endedAt, Instant lastStopFailedAt,
            String failureCode) {

        LaunchState state() {
            if ("CREATE_FAILED".equals(endKind)) {
                return LaunchState.FAILED;
            }
            if (endKind != null) {
                return LaunchState.STOPPED;
            }
            return instanceId == null ? LaunchState.REQUESTED : LaunchState.RUNNING;
        }

        boolean open() {
            return endKind == null;
        }
    }

    /** 열린 G1 세션 요약 */
    record OpenSession(UUID sessionId, String provider, String instanceId, String stage, Instant stageAt, Instant registeredAt) {
    }

    private static final Logger log = LoggerFactory.getLogger(GpuPowerService.class);
    private static final String LOCK = "SELECT pg_advisory_xact_lock(hashtext('simai.gpu_launch'))";
    private static final String SESSION_OPEN =
            "NOT EXISTS (SELECT 1 FROM gpu_session_event x WHERE x.session_id = s.session_id AND x.kind = 'ENDED')";
    private static final Pattern GPU_TYPE = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,99}$");
    private static final Pattern REGION = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9-]{0,39}$");
    /** 켠 요청을 기록했는데 만들기 결과가 이만큼 없으면(서버가 그 사이 죽음) 잃은 요청으로 닫는다 */
    private static final Duration REQUEST_LOST_AFTER = Duration.ofMinutes(5);
    /** 만들기 실패를 화면에 오류로 보이는 시간 */
    private static final Duration FAILURE_VISIBLE = Duration.ofMinutes(15);
    /** 표에 더 보일 선호 밖 지역 수(재고가 있는 곳만) */
    private static final int EXTRA_REGIONS = 4;

    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private volatile Clock clock;
    private final GpuProviders providers;
    private final GpuSettingsService settings;
    private final GpuSessionService sessions;
    private final GpuActivity activity;
    private final GpuSessionSweeper sweeper;
    private final GpuProperties properties;
    private final String ec2BaseUrl;

    public GpuPowerService(NamedParameterJdbcTemplate jdbc, TransactionTemplate transaction, ObjectMapper mapper, Clock clock,
            GpuProviders providers, GpuSettingsService settings, GpuSessionService sessions, GpuActivity activity,
            GpuSessionSweeper sweeper, GpuProperties properties, ConfigBundleProperties bundle) {
        this.jdbc = jdbc;
        this.transaction = transaction;
        this.mapper = mapper;
        this.clock = clock;
        this.providers = providers;
        this.settings = settings;
        this.sessions = sessions;
        this.activity = activity;
        this.sweeper = sweeper;
        this.properties = properties;
        this.ec2BaseUrl = properties.runpod().ec2BaseUrl().isEmpty() ? bundle.publicBaseUrl() : properties.runpod().ec2BaseUrl();
    }

    // ---------------------------------------------------------------- 재고

    /** 지금 설정의 공급자에서 선호 GPU × 지역 표와 기본 선택 */
    public ObjectNode offers() {
        GpuSettings s = settings.current();
        GpuProvider provider = configuredProvider(s.provider());
        List<GpuOffer> all = fetchOffers(provider, s);
        Table table = table(s, all);
        ObjectNode body = mapper.createObjectNode();
        body.put("provider", s.provider());
        body.put("settings_version_id", s.versionId());
        body.put("cloud_type", s.cloudType());
        body.put("interruptible", s.interruptible());
        body.put("max_price_per_hour_usd", s.maxPricePerHourUsd());
        ArrayNode gpus = body.putArray("gpu_types");
        for (String gpu : table.gpuTypes) {
            ObjectNode row = gpus.addObject();
            row.put("gpu_type", gpu);
            GpuOffer any = table.anyOffer.get(gpu);
            row.put("name", any == null || any.gpuName() == null ? gpu : any.gpuName());
            if (any == null || any.memoryGb() == null) {
                row.putNull("memory_gb");
            } else {
                row.put("memory_gb", any.memoryGb());
            }
        }
        ArrayNode regions = body.putArray("regions");
        for (String region : table.regions) {
            ObjectNode row = regions.addObject();
            row.put("region", region);
            row.put("name", table.regionNames.getOrDefault(region, region));
            row.put("preferred", s.regions().contains(region));
        }
        ArrayNode cells = body.putArray("cells");
        for (String gpu : table.gpuTypes) {
            for (String region : table.regions) {
                GpuOffer offer = table.cell(gpu, region);
                ObjectNode cell = cells.addObject();
                cell.put("gpu_type", gpu);
                cell.put("region", region);
                cell.put("stock", offer == null ? "NONE" : offer.stock());
                // 칸 가격(AWS 스팟은 가용 영역마다 다르다), 칸이 없으면 그 GPU 의 아무 가격
                BigDecimal price = offer != null && offer.pricePerHourUsd() != null ? offer.pricePerHourUsd() : table.price(gpu);
                cell.put("price_per_hour_usd", price);
                cell.put("within_cap", withinCap(price, s.maxPricePerHourUsd()));
            }
        }
        Optional<GpuOffer> pick = autoPick(s, all);
        if (pick.isPresent()) {
            ObjectNode def = body.putObject("default");
            def.put("gpu_type", pick.get().gpuType());
            def.put("region", pick.get().region());
        } else {
            body.putNull("default");
        }
        body.put("fetched_at_ms", clock.millis());
        return body;
    }

    // ---------------------------------------------------------------- 켜기

    /**
     * 켠다. gpuType·region 이 없으면 선호 순서 × 재고 × 가격 상한으로 고른다. 이미 켜져 있거나 켜는 중이면 GPU_ALREADY_ON.
     * @return 켠 요청 보기(launch)
     */
    public ObjectNode launch(Long userId, String gpuType, String region) {
        GpuSettings s = settings.current();
        GpuProvider provider = configuredProvider(s.provider());
        if (templateFor(provider.id(), s) == null) {
            throw new BusinessException(ErrorCode.Gpu.GPU_PROVIDER_NOT_CONFIGURED); // RunPod 템플릿·AWS 시작 템플릿이 없다
        }
        if (ec2BaseUrl.isEmpty()) {
            throw new BusinessException(ErrorCode.Gpu.EC2_BASE_URL_MISSING);
        }
        // 이미 켜져 있으면 재고를 묻기 전에 거부한다
        ensureNothingOn();
        List<GpuOffer> all = fetchOffers(provider, s);
        GpuOffer chosen;
        boolean auto = gpuType == null && region == null;
        if (auto) {
            chosen = autoPick(s, all).orElseThrow(() -> new BusinessException(
                    anyInStock(s, all) ? ErrorCode.Gpu.GPU_PRICE_OVER_CAP : ErrorCode.Gpu.GPU_OUT_OF_STOCK));
        } else {
            if (gpuType == null || region == null || !GPU_TYPE.matcher(gpuType).matches() || !REGION.matcher(region).matches()) {
                throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_SETTINGS);
            }
            chosen = all.stream().filter(o -> o.gpuType().equals(gpuType) && o.region().equals(region) && o.inStock()).findFirst()
                    .orElseThrow(() -> new BusinessException(ErrorCode.Gpu.GPU_OUT_OF_STOCK));
            if (!withinCap(chosen.pricePerHourUsd(), s.maxPricePerHourUsd())) {
                throw new BusinessException(ErrorCode.Gpu.GPU_PRICE_OVER_CAP);
            }
        }
        UUID launchId = UUID.randomUUID();
        Instant now = now();
        transaction.executeWithoutResult(status -> {
            jdbc.queryForList(LOCK, Map.of());
            ensureNothingOn();
            jdbc.update("""
                    INSERT INTO gpu_launch (launch_id, provider, settings_version_id, gpu_type, region, cloud_type, interruptible,
                        price_per_hour_usd, auto_selected, requested_by, requested_at)
                    VALUES (:id, :provider, :version, :gpu, :region, :cloud, :interruptible, :price, :auto, :user, :now)
                    """, new MapSqlParameterSource("id", launchId).addValue("provider", provider.id())
                    .addValue("version", s.versionId()).addValue("gpu", chosen.gpuType()).addValue("region", chosen.region())
                    .addValue("cloud", s.cloudType()).addValue("interruptible", s.interruptible())
                    .addValue("price", chosen.pricePerHourUsd()).addValue("auto", auto).addValue("user", userId)
                    .addValue("now", utc(now)));
        });
        log.info("GPU 켜기 요청 {}: 공급자 {}, {} / {}, 시간당 {} USD, 자동 선택 {}, 요청자 {}", launchId, provider.id(), chosen.gpuType(),
                chosen.region(), chosen.pricePerHourUsd(), auto, userId);
        try {
            CreatedInstance created = provider.create(spec(launchId, provider.id(), s, chosen));
            event(launchId, "CREATED", created.instanceId(), null, null, userId);
            log.info("GPU 켜기 요청 {}: 인스턴스 {} 만듦", launchId, created.instanceId());
        } catch (GpuProviderException e) {
            event(launchId, "CREATE_FAILED", null, e.code(), e.getMessage(), userId);
            log.warn("GPU 켜기 요청 {} 실패: {}", launchId, e.code());
            throw new BusinessException("OUT_OF_STOCK".equals(e.code()) ? ErrorCode.Gpu.GPU_OUT_OF_STOCK
                    : "NOT_CONFIGURED".equals(e.code()) ? ErrorCode.Gpu.GPU_PROVIDER_NOT_CONFIGURED : ErrorCode.Gpu.GPU_PROVIDER_ERROR);
        }
        return launchView(latestLaunch().orElseThrow());
    }

    // ---------------------------------------------------------------- 끄기

    /** 관리자 끄기. LIVE·세팅 중이면 거부, 손으로 켠 GPU 는 GPU_STOP_UNSUPPORTED */
    public void stop(Long userId) {
        Target target = target().orElseThrow(() -> new BusinessException(ErrorCode.Gpu.GPU_NOT_ON));
        Optional<Busy> block = activity.stopBlock();
        if (block.isPresent()) {
            throw new BusinessException(block.get() == Busy.LIVE ? ErrorCode.Gpu.GPU_STOP_BLOCKED_LIVE : ErrorCode.Gpu.GPU_STOP_BLOCKED_SETUP);
        }
        if (target.instanceId() == null) {
            // 켠 요청은 있는데 공급자가 아직 인스턴스 ID 를 주지 않았다(만드는 중, 수 초). 잠시 뒤 다시 누르게 한다
            throw new BusinessException(ErrorCode.Gpu.GPU_PROVIDER_ERROR);
        }
        GpuProvider provider = providers.find(target.provider())
                .orElseThrow(() -> new BusinessException(ErrorCode.Gpu.GPU_STOP_UNSUPPORTED));
        if (!terminate(target, provider, userId, "ADMIN")) {
            throw new BusinessException(ErrorCode.Gpu.GPU_PROVIDER_ERROR);
        }
    }

    // ---------------------------------------------------------------- 자동 정지

    /**
     * 자동 정지 점검 한 번. @return 끈 까닭(없으면 비어 있다). 주기 작업(GpuAutoStopper)이 부른다.
     * 막는 일(LIVE·세팅·녹화 업로드·분석 작업)이 있으면 어떤 까닭이어도 끄지 않는다.
     */
    public Optional<String> autoStop() {
        Instant now = now();
        closeLostRequests(now);
        Optional<Target> found = target();
        if (found.isEmpty()) {
            return Optional.empty();
        }
        Target target = found.get();
        if (target.instanceId() == null) {
            return Optional.empty();
        }
        Optional<GpuProvider> provider = providers.find(target.provider());
        if (provider.isEmpty() || !provider.get().configured()) {
            return Optional.empty(); // 손으로 켠 GPU(MANUAL)·키 없음: 끌 수 없다
        }
        // 공급자에서 이미 사라졌으면 기록만 한다(과금 없음). 세션은 G1 무응답 정리가 끝낸다
        if (target.launch() != null) {
            try {
                if (provider.get().state(target.instanceId()) == InstanceState.GONE) {
                    event(target.launch().launchId(), "GONE", target.instanceId(), "PROVIDER_GONE", null, null);
                    log.info("GPU 인스턴스 {} 가 공급자에 없다(켠 요청 {} 닫음)", target.instanceId(), target.launch().launchId());
                    return Optional.empty();
                }
            } catch (GpuProviderException e) {
                log.warn("GPU 인스턴스 {} 상태 조회 실패: {}", target.instanceId(), e.code());
            }
        }
        Optional<Busy> block = activity.autoStopBlock(now, properties.uploadHold());
        if (block.isPresent()) {
            return Optional.empty();
        }
        Optional<String> reason = autoStopReason(target, now);
        if (reason.isEmpty()) {
            return Optional.empty();
        }
        log.warn("GPU 자동 정지({}): 인스턴스 {}", reason.get(), target.instanceId());
        return terminate(target, provider.get(), null, reason.get()) ? reason : Optional.empty();
    }

    private Optional<String> autoStopReason(Target target, Instant now) {
        Launch launch = target.launch();
        OpenSession session = target.session();
        Optional<ObjectNode> latest = latestSessionFor(target);
        if (session == null) {
            // 켰는데 세션이 아직(또는 이미) 없다
            if (latest.isPresent() && "ENDED".equals(latest.get().path("state").stringValue())) {
                return Optional.of("SESSION_ENDED");
            }
            if (launch != null && launch.createdAt() != null && !launch.createdAt().plus(properties.bootTimeout()).isAfter(now)) {
                return Optional.of("BOOT_TIMEOUT");
            }
            if (launch != null && launch.createdAt() != null) {
                return idle(target, now);
            }
            return Optional.empty();
        }
        boolean tier0Linked = sweeper.tier0Connected(session.sessionId());
        if ("FAILED".equals(session.stage()) && !tier0Linked && session.stageAt() != null
                && !session.stageAt().plus(properties.failedGrace()).isAfter(now)) {
            return Optional.of("FAILED");
        }
        return idle(target, now);
    }

    private Optional<String> idle(Target target, Instant now) {
        int minutes = settings.current().autoStopIdleMinutes();
        if (minutes <= 0) {
            return Optional.empty();
        }
        Instant since = target.startedAt();
        Optional<Instant> lastUse = activity.lastUse();
        if (lastUse.isPresent() && lastUse.get().isAfter(since)) {
            since = lastUse.get();
        }
        return since.plus(Duration.ofMinutes(minutes)).isAfter(now) ? Optional.empty() : Optional.of("IDLE");
    }

    /** 공급자 인스턴스를 지우고 기록한다. 지운 뒤 그 인스턴스의 열린 세션을 ADMIN_STOPPED 로 끝낸다. 실패하면 false */
    private boolean terminate(Target target, GpuProvider provider, Long userId, String reason) {
        Launch launch = target.launch();
        if (launch != null) {
            event(launch.launchId(), "STOP_REQUESTED", target.instanceId(), reason, null, userId);
        }
        try {
            provider.terminate(target.instanceId());
        } catch (GpuProviderException e) {
            if (launch != null) {
                event(launch.launchId(), "STOP_FAILED", target.instanceId(), e.code(), e.getMessage(), userId);
            }
            log.warn("GPU 인스턴스 {} 끄기 실패({}): {}", target.instanceId(), reason, e.code());
            return false;
        }
        if (launch != null) {
            event(launch.launchId(), "STOPPED", target.instanceId(), reason, null, userId);
        }
        for (UUID sessionId : openSessionsFor(target.provider(), target.instanceId())) {
            sessions.end(sessionId, EndReason.ADMIN_STOPPED);
        }
        log.info("GPU 인스턴스 {} 끔({}, 요청자 {})", target.instanceId(), reason, userId == null ? "자동" : userId);
        return true;
    }

    // ---------------------------------------------------------------- 상태

    /**
     * GET /api/gpu/sessions/current 응답에 켜기 상태를 더한다(필드 추가만): launch(가장 최근 켠 요청), power(점 상태),
     * stop_block(끄기를 막는 까닭 LIVE·SETUP, 없으면 null). body.session 은 G1 이 채운 가장 최근 세션이다.
     */
    public void describe(ObjectNode body) {
        Optional<Launch> launch = latestLaunch();
        body.set("launch", launch.map(this::launchView).orElse(null));
        JsonNode session = body.get("session");
        Power power = power(launch.orElse(null), session == null || session.isNull() ? null : session);
        body.put("power", power.name());
        Optional<Busy> block = power == Power.OFF ? Optional.empty() : activity.stopBlock();
        body.put("stop_block", block.map(Enum::name).orElse(null));
    }

    Power power(Launch launch, JsonNode session) {
        Instant now = now();
        boolean sessionActive = session != null && "ACTIVE".equals(session.path("state").stringValue());
        if (launch != null && launch.open()) {
            boolean sameInstance = session != null && launch.instanceId() != null
                    && launch.provider().equals(session.path("provider").stringValue())
                    && launch.instanceId().equals(session.path("instance_id").stringValue());
            if (launch.lastStopFailedAt() != null) {
                return Power.ERROR;
            }
            if (!sameInstance) {
                return Power.STARTING; // 만드는 중이거나 만들었지만 아직 G1 등록 전
            }
            if (!sessionActive) {
                return Power.ERROR; // 인스턴스는 남았는데 세션이 끝났다(무응답 등). 자동 정지가 끈다
            }
        }
        if (sessionActive) {
            String stage = session.path("stage").stringValue();
            if ("FAILED".equals(stage)) {
                return Power.ERROR;
            }
            if ("READY".equals(stage) || "CONNECTED".equals(session.path("tier0_link").stringValue())) {
                return Power.CONNECTED;
            }
            return Power.PREPARING;
        }
        if (launch != null && launch.state() == LaunchState.FAILED && launch.endedAt() != null
                && launch.endedAt().plus(FAILURE_VISIBLE).isAfter(now)) {
            return Power.ERROR;
        }
        return Power.OFF;
    }

    ObjectNode launchView(Launch l) {
        ObjectNode node = mapper.createObjectNode();
        node.put("launch_id", l.launchId().toString());
        node.put("state", l.state().name());
        node.put("provider", l.provider());
        node.put("gpu_type", l.gpuType());
        node.put("region", l.region());
        node.put("cloud_type", l.cloudType());
        node.put("interruptible", l.interruptible());
        node.put("price_per_hour_usd", l.pricePerHourUsd());
        node.put("auto_selected", l.autoSelected());
        node.put("requested_by", l.requestedByLabel());
        node.put("requested_at_ms", l.requestedAt().toEpochMilli());
        node.put("instance_id", l.instanceId());
        putMillis(node, "created_at_ms", l.createdAt());
        putMillis(node, "ended_at_ms", l.endedAt());
        node.put("end_reason", l.endReason());
        node.put("failure_code", l.failureCode());
        putMillis(node, "stop_failed_at_ms", l.lastStopFailedAt());
        return node;
    }

    // ---------------------------------------------------------------- 내부

    /** 지금 켜져 있는 것(끄기·자동 정지 대상): 끝나지 않은 켠 요청, 없으면 열린 세션 */
    private record Target(String provider, String instanceId, Launch launch, OpenSession session) {

        Instant startedAt() {
            if (launch != null) {
                return launch.requestedAt();
            }
            return session.registeredAt();
        }
    }

    private Optional<Target> target() {
        Optional<Launch> launch = latestLaunch().filter(Launch::open);
        List<OpenSession> open = openSessions();
        if (launch.isPresent()) {
            Launch l = launch.get();
            OpenSession session = l.instanceId() == null ? null : open.stream()
                    .filter(o -> o.provider().equals(l.provider()) && o.instanceId().equals(l.instanceId())).findFirst().orElse(null);
            return Optional.of(new Target(l.provider(), l.instanceId(), l, session));
        }
        if (open.isEmpty()) {
            return Optional.empty();
        }
        OpenSession session = open.getFirst();
        return Optional.of(new Target(session.provider(), session.instanceId(), null, session));
    }

    private void ensureNothingOn() {
        if (latestLaunch().filter(Launch::open).isPresent() || !openSessions().isEmpty()) {
            throw new BusinessException(ErrorCode.Gpu.GPU_ALREADY_ON);
        }
    }

    private void closeLostRequests(Instant now) {
        latestLaunch().filter(l -> l.state() == LaunchState.REQUESTED && !l.requestedAt().plus(REQUEST_LOST_AFTER).isAfter(now))
                .ifPresent(l -> {
                    event(l.launchId(), "CREATE_FAILED", null, "REQUEST_LOST", "만들기 결과를 받지 못했다(서버 재시작 등)", null);
                    log.warn("GPU 켜기 요청 {} 의 만들기 결과가 {} 동안 없어 닫았다", l.launchId(), REQUEST_LOST_AFTER);
                });
    }

    Optional<Launch> latestLaunch() {
        return jdbc.query("""
                SELECT l.launch_id::text AS id, l.provider, l.gpu_type, l.region, l.cloud_type, l.interruptible, l.price_per_hour_usd,
                    l.auto_selected, l.requested_by, u.login_id, l.requested_at,
                    c.instance_id, c.at AS created_at,
                    e.kind AS end_kind, e.reason_code AS end_reason, e.at AS ended_at,
                    (SELECT max(f.at) FROM gpu_launch_event f WHERE f.launch_id = l.launch_id AND f.kind = 'STOP_FAILED'
                        AND f.at > coalesce((SELECT max(r.at) FROM gpu_launch_event r WHERE r.launch_id = l.launch_id
                                             AND r.kind = 'STOP_REQUESTED' AND r.event_id > f.event_id), '-infinity')) AS stop_failed_at
                FROM gpu_launch l
                LEFT JOIN app_user u ON u.user_id = l.requested_by
                LEFT JOIN gpu_launch_event c ON c.launch_id = l.launch_id AND c.kind = 'CREATED'
                LEFT JOIN gpu_launch_event e ON e.launch_id = l.launch_id AND e.kind IN ('CREATE_FAILED', 'STOPPED', 'GONE')
                ORDER BY l.requested_at DESC, l.launch_id
                LIMIT 1
                """, Map.of(), this::launchRow).stream().findFirst();
    }

    private Launch launchRow(ResultSet rs, int i) throws SQLException {
        String endKind = rs.getString("end_kind");
        return new Launch(UUID.fromString(rs.getString("id")), rs.getString("provider"), rs.getString("gpu_type"), rs.getString("region"),
                rs.getString("cloud_type"), rs.getBoolean("interruptible"), rs.getBigDecimal("price_per_hour_usd"),
                rs.getBoolean("auto_selected"), (Long) rs.getObject("requested_by"), rs.getString("login_id"),
                instant(rs, "requested_at"), rs.getString("instance_id"), instant(rs, "created_at"), endKind,
                rs.getString("end_reason"), instant(rs, "ended_at"), endKind == null ? instant(rs, "stop_failed_at") : null,
                "CREATE_FAILED".equals(endKind) ? rs.getString("end_reason") : null);
    }

    private List<OpenSession> openSessions() {
        return jdbc.query("""
                SELECT s.session_id::text AS id, s.provider, s.instance_id, s.registered_at, prog.stage, prog.received_at AS stage_at
                FROM gpu_session s
                LEFT JOIN LATERAL (
                    SELECT p.stage, p.received_at FROM gpu_session_event p
                    WHERE p.session_id = s.session_id AND p.kind = 'PROGRESS' ORDER BY p.event_id DESC LIMIT 1) prog ON true
                """ + " WHERE " + SESSION_OPEN + " ORDER BY s.registered_at DESC", Map.of(), (rs, i) -> new OpenSession(UUID.fromString(rs.getString("id")), rs.getString("provider"),
                rs.getString("instance_id"), rs.getString("stage"), instant(rs, "stage_at"), instant(rs, "registered_at")));
    }

    private List<UUID> openSessionsFor(String provider, String instanceId) {
        return jdbc.query("SELECT s.session_id::text AS id FROM gpu_session s WHERE s.provider = :provider AND s.instance_id = :instance AND "
                        + SESSION_OPEN, new MapSqlParameterSource("provider", provider).addValue("instance", instanceId),
                (rs, i) -> UUID.fromString(rs.getString("id")));
    }

    /** 이 대상 인스턴스의 가장 최근 세션 보기(G1 latest 와 같은 꼴). 대상 인스턴스의 세션이 아니면 비어 있다 */
    private Optional<ObjectNode> latestSessionFor(Target target) {
        Optional<ObjectNode> latest = sessions.latest(sweeper::tier0Connected);
        if (latest.isEmpty() || target.instanceId() == null) {
            return Optional.empty();
        }
        ObjectNode node = latest.get();
        if (!target.provider().equals(node.path("provider").stringValue())
                || !target.instanceId().equals(node.path("instance_id").stringValue())) {
            return Optional.empty();
        }
        return Optional.of(node);
    }

    private void event(UUID launchId, String kind, String instanceId, String reason, String detail, Long userId) {
        jdbc.update("""
                INSERT INTO gpu_launch_event (launch_id, kind, instance_id, reason_code, detail, actor_user_id, at)
                VALUES (:id, :kind, :instance, :reason, :detail, :user, :now)
                ON CONFLICT DO NOTHING
                """, new MapSqlParameterSource("id", launchId).addValue("kind", kind).addValue("instance", instanceId)
                .addValue("reason", reason).addValue("detail", detail == null ? null : truncate(detail))
                .addValue("user", userId).addValue("now", utc(now())));
    }

    private LaunchSpec spec(UUID launchId, String provider, GpuSettings s, GpuOffer chosen) {
        Map<String, String> env = new LinkedHashMap<>();
        // simai-gpu-boot(S15P31A101-700)이 읽는 이름. 부팅 비밀값 원문은 secretEnv 의 RunPod Secret 참조로만 들어간다
        env.put("SIMAI_GPU_BOOT_ID", properties.runpod().bootId());
        env.put("SIMAI_EC2_BASE_URL", ec2BaseUrl);
        env.put("SIMAI_GPU_PROVIDER", provider);
        if (chosen.pricePerHourUsd() != null) {
            env.put("SIMAI_PRICE_PER_HOUR_USD", chosen.pricePerHourUsd().toPlainString());
        }
        env.put("SIMAI_GPU_LAUNCH_ID", launchId.toString());
        String name = properties.runpod().podName() + "-" + launchId.toString().substring(0, 8);
        // 비밀 환경 변수 이름 목록은 공급자 공통이다(RunPod: Secret 참조, AWS: 인스턴스가 Secrets Manager JSON 에서 같은 이름으로 받음)
        return new LaunchSpec(name, templateFor(provider, s), chosen.gpuType(), chosen.region(), s.cloudType(), s.interruptible(), 1,
                env, properties.runpod().secretEnv(), network(provider, s));
    }

    /** 공급자별 템플릿: RunPod 템플릿 ID, AWS 시작 템플릿 ID(S15P31A101-703). 없으면 null(켜기 503) */
    private static String templateFor(String provider, GpuSettings s) {
        return "AWS".equals(provider) ? s.awsLaunchTemplateId() : s.templateId();
    }

    /** 공급자별 망 설정: AWS 서브넷·보안 그룹, RunPod 은 없음 */
    private static Network network(String provider, GpuSettings s) {
        return "AWS".equals(provider) ? new Network(s.awsSubnetIds(), s.awsSecurityGroupIds()) : Network.NONE;
    }

    private GpuProvider configuredProvider(String id) {
        GpuProvider provider = providers.find(id).orElseThrow(() -> new BusinessException(ErrorCode.Gpu.GPU_PROVIDER_NOT_CONFIGURED));
        if (!provider.configured()) {
            throw new BusinessException(ErrorCode.Gpu.GPU_PROVIDER_NOT_CONFIGURED);
        }
        return provider;
    }

    private List<GpuOffer> fetchOffers(GpuProvider provider, GpuSettings s) {
        try {
            return provider.offers(new OfferQuery(s.cloudType(), 1, s.gpuTypes(), s.regions(), s.interruptible(), network(provider.id(), s)));
        } catch (GpuProviderException e) {
            log.warn("GPU 재고 조회 실패: {}", e.code());
            throw new BusinessException("NOT_CONFIGURED".equals(e.code()) ? ErrorCode.Gpu.GPU_PROVIDER_NOT_CONFIGURED
                    : ErrorCode.Gpu.GPU_PROVIDER_ERROR);
        }
    }

    /** 선호 GPU 순서가 먼저, 그 안에서 선호 지역 순서. 재고가 있고 가격 상한 안인 첫 칸 */
    static Optional<GpuOffer> autoPick(GpuSettings s, List<GpuOffer> offers) {
        for (String gpu : s.gpuTypes()) {
            for (String region : s.regions()) {
                Optional<GpuOffer> hit = offers.stream()
                        .filter(o -> o.gpuType().equals(gpu) && o.region().equals(region) && o.inStock()
                                && withinCap(o.pricePerHourUsd(), s.maxPricePerHourUsd()))
                        .findFirst();
                if (hit.isPresent()) {
                    return hit;
                }
            }
        }
        return Optional.empty();
    }

    private static boolean anyInStock(GpuSettings s, List<GpuOffer> offers) {
        return offers.stream().anyMatch(o -> o.inStock() && s.gpuTypes().contains(o.gpuType()) && s.regions().contains(o.region()));
    }

    static boolean withinCap(BigDecimal price, BigDecimal cap) {
        if (cap == null) {
            return true;
        }
        return price != null && price.compareTo(cap) <= 0;
    }

    /** 표: 선호 GPU(순서) × 선호 지역(순서) + 선호 GPU 재고가 있는 다른 지역 몇 곳 */
    private static Table table(GpuSettings s, List<GpuOffer> offers) {
        Table t = new Table();
        t.gpuTypes.addAll(s.gpuTypes());
        Set<String> regions = new LinkedHashSet<>(s.regions());
        offers.stream()
                .filter(o -> s.gpuTypes().contains(o.gpuType()) && o.inStock() && !s.regions().contains(o.region()))
                .map(GpuOffer::region).distinct().sorted(Comparator.naturalOrder()).limit(EXTRA_REGIONS).forEach(regions::add);
        t.regions.addAll(regions);
        for (GpuOffer o : offers) {
            if (o.regionName() != null) {
                t.regionNames.putIfAbsent(o.region(), o.regionName());
            }
            if (s.gpuTypes().contains(o.gpuType())) {
                t.anyOffer.putIfAbsent(o.gpuType(), o);
                t.cells.put(o.gpuType() + "\n" + o.region(), o);
            }
        }
        return t;
    }

    private static final class Table {
        final List<String> gpuTypes = new ArrayList<>();
        final List<String> regions = new ArrayList<>();
        final Map<String, String> regionNames = new HashMap<>();
        final Map<String, GpuOffer> anyOffer = new HashMap<>();
        final Map<String, GpuOffer> cells = new HashMap<>();

        GpuOffer cell(String gpu, String region) {
            return cells.get(gpu + "\n" + region);
        }

        BigDecimal price(String gpu) {
            GpuOffer any = anyOffer.get(gpu);
            return any == null ? null : any.pricePerHourUsd();
        }
    }

    private static String truncate(String value) {
        return value.length() <= 500 ? value : value.substring(0, 500);
    }

    private static Instant instant(ResultSet rs, String column) throws SQLException {
        OffsetDateTime value = rs.getObject(column, OffsetDateTime.class);
        return value == null ? null : value.toInstant();
    }

    private static void putMillis(ObjectNode node, String field, Instant value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value.toEpochMilli());
        }
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    private Instant now() {
        return clock.instant().truncatedTo(ChronoUnit.MILLIS);
    }

    /** 시험에서 시계를 옮길 때만 쓴다(유휴·등록 시간 초과 판정) */
    void useClock(Clock clock) {
        this.clock = clock;
    }
}
