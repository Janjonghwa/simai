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
import com.ssafy.simai.domain.gpu.provider.RunPodProvider;
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
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.function.Predicate;
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
 * 세션이 끝났는데 인스턴스가 남음, FAILED 지속(failedGrace, I1 없음), 유휴(설정의 분)면 끈다. 유휴는 준비가 끝난 뒤부터 센다
 * (S15P31A101-708: 그 세션의 첫 READY 보고와 Tier 0 첫 I1 연결 중 이른 쪽, 부팅 시간은 세지 않는다). Tier 0 가 붙어 있으면 관리자 화면
 * 조작 신호(POST /api/gpu/presence)도 사용으로 세되, 조작만으로 미루는 시간은 마지막 실제 사용·준비 완료 뒤 presenceMaxExtension(기본 90분)까지다
 * (S15P31A101-774: 화면만 열어 둔 채 2시간 켜져 있었다).</li>
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

    /** 열린 G1 세션 요약. readyAt 은 그 세션의 첫 READY 보고 수신 시각(없으면 null) */
    record OpenSession(UUID sessionId, String provider, String instanceId, String stage, Instant stageAt, Instant registeredAt,
            Instant readyAt) {
    }

    /** 자동 정지 까닭. reason 은 gpu_launch_event.reason_code, detail 은 같은 사건의 detail(유휴의 기준·조작 상한 구분, S15P31A101-774) */
    private record AutoStopCause(String reason, String detail) {
    }

    /** 유휴 정지 detail 앞머리: 조작 신호만 이어지다 상한(presenceMaxExtension)에 걸렸다 */
    static final String PRESENCE_CAP = "PRESENCE_CAP";

    /** 이 세션의 Tier 0 가 I1 에 붙은 것을 처음 본 시각(메모리, 자동 정지 점검 때 본다) */
    private record FirstLink(UUID sessionId, Instant at) {
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
    /** 그 세션 Tier 0 의 I1 연결이 지금 열려 있는지. 시험만 바꾼다(useTier0Link) */
    private volatile Predicate<UUID> tier0Link;
    /** 마지막 관리자 화면 조작(메모리). 서버가 뜰 때는 뜬 시각으로 둔다(재시작 직후 바로 끄지 않게, 설계 -708) */
    private volatile Instant lastPresence;
    private volatile FirstLink firstLink;

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
        this.tier0Link = sweeper::tier0Connected;
        this.lastPresence = now();
    }

    // ---------------------------------------------------------------- 관리자 화면 조작

    /**
     * 관리자가 화면을 보며 조작했다(POST /api/gpu/presence, front 는 화면이 보이고 최근 5분 안에 입력이 있을 때 1분에 한 번까지 보낸다).
     * 마지막 시각만 메모리에 둔다. Tier 0 가 붙어 있을 때만 자동 정지의 사용으로 세고, 조작만으로 미루는 시간에는 상한이 있다(idle).
     */
    public void presence() {
        lastPresence = now();
    }

    // ---------------------------------------------------------------- 재고

    /**
     * 지금 설정의 공급자 재고 표와 기본 선택(S15P31A101-705: 행은 공급자 카탈로그의 모든 GPU, 선호 목록은 정렬·기본 선택용).
     * 행은 추천(H100·H200 계열) → VRAM 등급 → 선호 순서 → VRAM 큰 순 → 이름 순이고, 칸마다 켤 수 있음(available: true·false·모름 null)을 준다.
     * 기존 stock 칸은 그대로 둔다(추가만).
     */
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
        for (Row row : table.rows) {
            ObjectNode node = gpus.addObject();
            node.put("gpu_type", row.gpuType());
            node.put("name", row.name());
            if (row.memoryGb() == null) {
                node.putNull("memory_gb");
            } else {
                node.put("memory_gb", row.memoryGb());
            }
            node.put("preferred", row.preferredRank() != Integer.MAX_VALUE);
            node.put("recommended", row.recommended());
            node.put("tier", row.tier());
            node.put("available", rowAvailable(table, row.gpuType()));
        }
        ArrayNode regions = body.putArray("regions");
        for (String region : table.regions) {
            ObjectNode node = regions.addObject();
            node.put("region", region);
            node.put("name", table.regionNames.getOrDefault(region, region));
            node.put("preferred", s.regions().contains(region));
        }
        ArrayNode cells = body.putArray("cells");
        for (Row row : table.rows) {
            for (String region : table.regions) {
                GpuOffer offer = table.cell(row.gpuType(), region);
                ObjectNode cell = cells.addObject();
                cell.put("gpu_type", row.gpuType());
                cell.put("region", region);
                String stock = offer == null ? "NONE" : offer.stock();
                cell.put("stock", stock);
                cell.put("available", GpuTiers.available(stock));
                // 칸 가격(AWS 스팟은 가용 영역마다 다르다), 칸이 없으면 그 GPU 의 아무 가격
                BigDecimal price = offer != null && offer.pricePerHourUsd() != null ? offer.pricePerHourUsd() : table.price(row.gpuType());
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

    /** 행 하나가 보이는 지역 중 어디서든 켤 수 있는지: 한 칸이라도 true 면 true, 아니면 모름이 있으면 null, 아니면 false */
    private static Boolean rowAvailable(Table table, String gpuType) {
        boolean unknown = false;
        for (String region : table.regions) {
            GpuOffer offer = table.cell(gpuType, region);
            Boolean available = GpuTiers.available(offer == null ? null : offer.stock());
            if (Boolean.TRUE.equals(available)) {
                return true;
            }
            unknown |= available == null;
        }
        return unknown ? null : false;
    }

    // ---------------------------------------------------------------- 켜기

    /**
     * 켠다. gpuType·region 이 없으면 추천 → 선호 순서 × 선호 지역 × 재고 × 가격 상한으로 고른다(autoPick). 이미 켜져 있거나 켜는 중이면 GPU_ALREADY_ON.
     * @return 켠 요청 보기(launch)
     */
    public ObjectNode launch(Long userId, String gpuType, String region) {
        GpuSettings s = settings.current();
        GpuProvider provider = configuredProvider(s.provider());
        if (templateFor(provider.id(), s) == null) {
            throw new BusinessException(ErrorCode.Gpu.GPU_PROVIDER_NOT_CONFIGURED); // RunPod 템플릿·AWS 시작 템플릿이 없다
        }
        if (RunPodProvider.ID.equals(provider.id()) && s.interruptible()) {
            // RunPod v2 에는 스팟이 없다(S15P31A101-781). 기본값·옛 설정이 스팟이면 재고를 묻거나 기록을 남기기 전에 막는다
            throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_SETTINGS);
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
            chosen = all.stream().filter(o -> gpuType.equals(o.gpuType()) && region.equals(o.region()) && o.inStock()).findFirst()
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
        if (!terminate(target, provider, userId, "ADMIN", null)) {
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
        Optional<AutoStopCause> cause = autoStopReason(target, now);
        if (cause.isEmpty()) {
            return Optional.empty();
        }
        String reason = cause.get().reason();
        String detail = cause.get().detail();
        log.warn("GPU 자동 정지({}{}): 인스턴스 {}", reason, detail == null ? "" : ", " + detail, target.instanceId());
        return terminate(target, provider.get(), null, reason, detail) ? Optional.of(reason) : Optional.empty();
    }

    private Optional<AutoStopCause> autoStopReason(Target target, Instant now) {
        Launch launch = target.launch();
        OpenSession session = target.session();
        Optional<ObjectNode> latest = latestSessionFor(target);
        if (session == null) {
            // 켰는데 세션이 아직(또는 이미) 없다
            if (latest.isPresent() && "ENDED".equals(latest.get().path("state").stringValue())) {
                return Optional.of(new AutoStopCause("SESSION_ENDED", null));
            }
            if (launch != null && launch.createdAt() != null && !launch.createdAt().plus(properties.bootTimeout()).isAfter(now)) {
                return Optional.of(new AutoStopCause("BOOT_TIMEOUT", null));
            }
            return Optional.empty(); // 등록 전(부팅 중)은 유휴로 세지 않는다. 등록이 없으면 BOOT_TIMEOUT 이 끈다
        }
        boolean tier0Linked = tier0Link.test(session.sessionId());
        Instant linkedAt = firstLinkAt(session.sessionId(), tier0Linked, now);
        if ("FAILED".equals(session.stage()) && !tier0Linked && session.stageAt() != null
                && !session.stageAt().plus(properties.failedGrace()).isAfter(now)) {
            return Optional.of(new AutoStopCause("FAILED", null));
        }
        Instant readySince = earliest(session.readyAt(), linkedAt);
        if (readySince == null) {
            // 아직 준비 중(READY·I1 연결 전): 부팅 시간은 세지 않는다. 부팅이 멈추면 FAILED 지속·무응답(SESSION_ENDED)이 끈다
            return Optional.empty();
        }
        return idle(readySince, tier0Linked, now);
    }

    /**
     * 유휴 판정. 시작점 = max(준비 완료, 마지막 실행 종료·세팅 갱신·작업 대여·업로드 서명, Tier 0 가 붙어 있으면 마지막 관리자 조작).
     * 켠 시각은 넣지 않는다(S15P31A101-708: 부팅이 29분 걸린 세션이 READY 66초 뒤 꺼졌다).
     * 관리자 조작만으로 이어지는 연장은 max(준비 완료, 마지막 실제 사용) 뒤 presenceMaxExtension 까지다(S15P31A101-774). 넘으면 조작이 있어도
     * IDLE 로 끄고 detail 을 PRESENCE_CAP 으로 남긴다. LIVE·세팅·업로드·분석 작업 중이면 여기 오기 전에 autoStopBlock 이 막는다.
     */
    private Optional<AutoStopCause> idle(Instant readySince, boolean tier0Linked, Instant now) {
        int minutes = settings.current().autoStopIdleMinutes();
        if (minutes <= 0) {
            return Optional.empty();
        }
        Duration window = Duration.ofMinutes(minutes);
        Instant use = readySince;
        String basis = "READY";
        Optional<Instant> lastUse = activity.lastUse();
        if (lastUse.isPresent() && lastUse.get().isAfter(use)) {
            use = lastUse.get();
            basis = "LAST_USE";
        }
        if (use.plus(window).isAfter(now)) {
            return Optional.empty(); // 실제 사용(또는 준비 완료) 뒤 N분 안
        }
        Instant presence = lastPresence;
        boolean presenceCounts = tier0Linked && presence != null && presence.isAfter(use);
        if (!presenceCounts || !presence.plus(window).isAfter(now)) {
            Instant since = presenceCounts ? presence : use;
            return Optional.of(new AutoStopCause("IDLE", "basis=" + (presenceCounts ? "PRESENCE" : basis) + " since=" + since
                    + " idle_minutes=" + minutes));
        }
        // 조작 신호만 이어지고 있다: 실제 사용 뒤 상한까지만 미룬다
        Duration cap = properties.presenceMaxExtension();
        if (use.plus(cap).isAfter(now)) {
            return Optional.empty();
        }
        return Optional.of(new AutoStopCause("IDLE", PRESENCE_CAP + " basis=" + basis + " since=" + use + " presence_only_minutes="
                + Duration.between(use, now).toMinutes() + " cap_minutes=" + cap.toMinutes() + " last_presence=" + presence));
    }

    /** 이 세션 Tier 0 의 첫 I1 연결을 본 시각. 지금 붙어 있는데 처음 보면 지금으로 적는다. 본 적 없으면 null */
    private Instant firstLinkAt(UUID sessionId, boolean linkedNow, Instant now) {
        FirstLink seen = firstLink;
        if (seen != null && seen.sessionId().equals(sessionId)) {
            return seen.at();
        }
        if (!linkedNow) {
            return null;
        }
        firstLink = new FirstLink(sessionId, now);
        return now;
    }

    private static Instant earliest(Instant a, Instant b) {
        if (a == null) {
            return b;
        }
        return b == null || a.isBefore(b) ? a : b;
    }

    /** 공급자 인스턴스를 지우고 기록한다. 지운 뒤 그 인스턴스의 열린 세션을 ADMIN_STOPPED 로 끝낸다. 실패하면 false */
    private boolean terminate(Target target, GpuProvider provider, Long userId, String reason, String detail) {
        Launch launch = target.launch();
        if (launch != null) {
            event(launch.launchId(), "STOP_REQUESTED", target.instanceId(), reason, detail, userId);
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
            event(launch.launchId(), "STOPPED", target.instanceId(), reason, detail, userId);
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
                SELECT s.session_id::text AS id, s.provider, s.instance_id, s.registered_at, prog.stage, prog.received_at AS stage_at,
                    (SELECT min(r.received_at) FROM gpu_session_event r
                     WHERE r.session_id = s.session_id AND r.kind = 'PROGRESS' AND r.stage = 'READY') AS ready_at
                FROM gpu_session s
                LEFT JOIN LATERAL (
                    SELECT p.stage, p.received_at FROM gpu_session_event p
                    WHERE p.session_id = s.session_id AND p.kind = 'PROGRESS' ORDER BY p.event_id DESC LIMIT 1) prog ON true
                """ + " WHERE " + SESSION_OPEN + " ORDER BY s.registered_at DESC", Map.of(), (rs, i) -> new OpenSession(UUID.fromString(rs.getString("id")), rs.getString("provider"),
                rs.getString("instance_id"), rs.getString("stage"), instant(rs, "stage_at"), instant(rs, "registered_at"),
                instant(rs, "ready_at")));
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
        // 후보 = 선호 목록 + 공급자 카탈로그 후보(AWS 처럼 물어볼 종류를 알아야 하는 공급자, S15P31A101-705). RunPod 은 목록 전체를 준다
        Set<String> candidates = new LinkedHashSet<>(s.gpuTypes());
        candidates.addAll(provider.catalogGpuTypes());
        try {
            return listed(s, provider.offers(new OfferQuery(s.cloudType(), 1, List.copyOf(candidates), s.regions(), s.interruptible(),
                    network(provider.id(), s))));
        } catch (GpuProviderException e) {
            log.warn("GPU 재고 조회 실패: {}", e.code());
            throw new BusinessException("NOT_CONFIGURED".equals(e.code()) ? ErrorCode.Gpu.GPU_PROVIDER_NOT_CONFIGURED
                    : ErrorCode.Gpu.GPU_PROVIDER_ERROR);
        }
    }

    /**
     * 공급자 답에서 표에 올릴 항목만 남긴다(S15P31A101-713, AWS·RunPod 공통). VRAM 0 이하는 모름(null)으로 바꾸고, 선호 밖 항목은
     * 이름·VRAM 을 아는 것만 둔다(RunPod 카탈로그의 「unknown · 0 GB」 같은 빈 항목, AWS 에서 GPU 정보를 못 받은 종류는 행을 만들지 않는다).
     * 선호 GPU 는 사람이 적은 것이라 그대로 둔다(이름·VRAM 을 몰라도 행·칸·자동 선택 대상).
     */
    static List<GpuOffer> listed(GpuSettings s, List<GpuOffer> offers) {
        List<GpuOffer> kept = new ArrayList<>();
        for (GpuOffer o : offers) {
            Integer memory = GpuTiers.memory(o.memoryGb());
            if (!s.gpuTypes().contains(o.gpuType()) && !GpuTiers.listable(o.gpuType(), o.gpuName(), memory)) {
                continue;
            }
            kept.add(Objects.equals(memory, o.memoryGb()) ? o
                    : new GpuOffer(o.gpuType(), o.gpuName(), memory, o.region(), o.regionName(), o.stock(), o.pricePerHourUsd()));
        }
        return kept;
    }

    /**
     * 기본 선택(S15P31A101-705): ① 추천 GPU(표 순서라 선호 순서가 먼저) 중 켤 수 있다고 확인된(재고 HIGH·MEDIUM·LOW) 첫 칸 →
     * ② 없으면 선호 GPU 순서로 재고가 있거나 모르는(UNKNOWN, AWS) 첫 칸. 둘 다 선호 지역 순서 안, 가격 상한 안이다.
     * ①에서 모름을 빼는 까닭: AWS 는 모든 칸이 모름이라 선호(g6e 등)보다 비싼 추천(p5.48xlarge 시간당 약 76달러)이 기본이 되면 안 된다.
     * GPU 를 지역보다 먼저 보는 것은 -702 와 같다(엔진 캐시가 GPU 종류별). 선호 밖 지역은 자동으로 고르지 않는다(EC2·노트북과의
     * 지연이 커질 수 있어 사람이 표에서 고른다).
     */
    static Optional<GpuOffer> autoPick(GpuSettings s, List<GpuOffer> offers) {
        List<String> recommended = rows(s, offers).stream().filter(Row::recommended).map(Row::gpuType).toList();
        Optional<GpuOffer> hit = firstCell(s, offers, recommended, true);
        return hit.isPresent() ? hit : firstCell(s, offers, s.gpuTypes(), false);
    }

    private static Optional<GpuOffer> firstCell(GpuSettings s, List<GpuOffer> offers, List<String> gpus, boolean knownStock) {
        for (String gpu : gpus) {
            for (String region : s.regions()) {
                Optional<GpuOffer> hit = offers.stream()
                        .filter(o -> gpu.equals(o.gpuType()) && region.equals(o.region())
                                && (knownStock ? Boolean.TRUE.equals(GpuTiers.available(o.stock())) : o.inStock())
                                && withinCap(o.pricePerHourUsd(), s.maxPricePerHourUsd()))
                        .findFirst();
                if (hit.isPresent()) {
                    return hit;
                }
            }
        }
        return Optional.empty();
    }

    /** 추천 행(표 순서) → 선호 목록(순서). 재고 없음 오류 구분·표에 더 보일 지역 고르기에 쓴다 */
    private static List<String> candidates(GpuSettings s, List<GpuOffer> offers) {
        Set<String> order = new LinkedHashSet<>();
        rows(s, offers).stream().filter(Row::recommended).map(Row::gpuType).forEach(order::add);
        order.addAll(s.gpuTypes());
        return List.copyOf(order);
    }

    private static boolean anyInStock(GpuSettings s, List<GpuOffer> offers) {
        List<String> gpus = candidates(s, offers);
        return offers.stream().anyMatch(o -> o.inStock() && gpus.contains(o.gpuType()) && s.regions().contains(o.region()));
    }

    static boolean withinCap(BigDecimal price, BigDecimal cap) {
        if (cap == null) {
            return true;
        }
        return price != null && price.compareTo(cap) <= 0;
    }

    /** 표 한 행(GPU 종류). preferredRank 는 선호 목록 순서(아니면 Integer.MAX_VALUE) */
    record Row(String gpuType, String name, Integer memoryGb, int preferredRank, boolean recommended, String tier) {
    }

    /**
     * 행: 공급자 카탈로그의 모든 GPU + (카탈로그에 없어도) 선호 GPU. 정렬은 추천 → VRAM 등급 → 선호 순서 → VRAM 큰 순 → 이름 순.
     * 이름·VRAM 은 그 종류의 첫 값(모르면 이름은 ID, VRAM 은 null).
     */
    static List<Row> rows(GpuSettings s, List<GpuOffer> offers) {
        Map<String, String> names = new LinkedHashMap<>();
        Map<String, Integer> memory = new HashMap<>();
        for (GpuOffer o : offers) {
            if (o.gpuType() == null) {
                continue;
            }
            names.putIfAbsent(o.gpuType(), o.gpuName() == null ? o.gpuType() : o.gpuName());
            if (o.memoryGb() != null) {
                memory.putIfAbsent(o.gpuType(), o.memoryGb());
            }
        }
        for (String gpu : s.gpuTypes()) {
            names.putIfAbsent(gpu, gpu);
        }
        List<Row> rows = new ArrayList<>();
        for (Map.Entry<String, String> e : names.entrySet()) {
            Integer mem = memory.get(e.getKey());
            int rank = s.gpuTypes().indexOf(e.getKey());
            rows.add(new Row(e.getKey(), e.getValue(), mem, rank < 0 ? Integer.MAX_VALUE : rank,
                    GpuTiers.recommended(e.getKey(), e.getValue(), mem), GpuTiers.tier(mem)));
        }
        rows.sort(Comparator.comparing((Row r) -> !r.recommended())
                .thenComparingInt(r -> GpuTiers.rank(r.tier()))
                .thenComparingInt(Row::preferredRank)
                .thenComparing(Row::memoryGb, Comparator.nullsLast(Comparator.reverseOrder()))
                .thenComparing(Row::name));
        return rows;
    }

    /** 표: 모든 행 × (선호 지역(순서) + 추천·선호 GPU 재고가 있는 다른 지역 몇 곳) */
    private static Table table(GpuSettings s, List<GpuOffer> offers) {
        Table t = new Table();
        t.rows.addAll(rows(s, offers));
        Set<String> focus = new LinkedHashSet<>(candidates(s, offers));
        Set<String> regions = new LinkedHashSet<>(s.regions());
        offers.stream()
                .filter(o -> o.region() != null && focus.contains(o.gpuType()) && o.inStock() && !s.regions().contains(o.region()))
                .map(GpuOffer::region).distinct().sorted(Comparator.naturalOrder()).limit(EXTRA_REGIONS).forEach(regions::add);
        t.regions.addAll(regions);
        for (GpuOffer o : offers) {
            // 지역 없는 칸 = 카탈로그에만 있고 어느 지역에도 없는 GPU(행만 생기고 칸은 없음 = 없음). 가격은 행에 보인다
            t.anyOffer.putIfAbsent(o.gpuType(), o);
            if (o.region() == null) {
                continue;
            }
            if (o.regionName() != null) {
                t.regionNames.putIfAbsent(o.region(), o.regionName());
            }
            t.cells.put(o.gpuType() + "\n" + o.region(), o);
        }
        return t;
    }

    private static final class Table {
        final List<Row> rows = new ArrayList<>();
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

    /** 시험에서 Tier 0 I1 연결 여부를 바꿀 때만 쓴다. null 이면 실제 I1 연결을 본다 */
    void useTier0Link(Predicate<UUID> link) {
        this.tier0Link = link == null ? sweeper::tier0Connected : link;
        this.firstLink = null;
    }
}
