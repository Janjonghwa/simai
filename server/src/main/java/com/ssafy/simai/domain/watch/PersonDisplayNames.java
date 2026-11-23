package com.ssafy.simai.domain.watch;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.core.annotation.Order;
import org.springframework.jdbc.core.RowCallbackHandler;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.event.TransactionalEventListener;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;

/**
 * 현장·날짜(Asia/Seoul)별 사람 표시 이름 해석과 추가 전용 이력 관리(S15P31A101-658, 명세 5.2·11.4-5·12.4·13.2~13.3·부록 C.6).
 * <ul>
 *   <li>우선순위: 관리자 World ID 지정 이름(WORLD_MANUAL, 당일·병합 승계) &gt; 배정된 워치 기기 표시 이름(WATCH_DEVICE, 현장 지속)
 *       &gt; "미확인 N"(UNIDENTIFIED, 현장·날짜별 1부터 단조 증가, 같은 World ID 는 같은 번호)</li>
 *   <li>병합(aliases): 생존 World ID 가 관리자 지정 이름(더 최근 지정 우선)과 미확인 번호(더 앞선 작은 번호 우선)를 이어받는다</li>
 *   <li>분할(split_from): 부모는 자기 이름·번호를 유지하고, 자식은 새 미확인 번호를 받는다</li>
 *   <li>이력(AGENTS 규칙 6): 모든 변경·배정은 INSERT 만 수행하고 기존 행을 덮어쓰거나 지우지 않는다(최신 history_id 행이 유효)</li>
 * </ul>
 */
@Service
public class PersonDisplayNames {

    public static final ZoneId SITE_ZONE = ZoneId.of("Asia/Seoul");
    public static final int MAX_DISPLAY_NAME_LENGTH = 20;
    public static final String SOURCE_WORLD_MANUAL = "WORLD_MANUAL";
    public static final String SOURCE_WATCH_DEVICE = "WATCH_DEVICE";
    public static final String SOURCE_UNIDENTIFIED = "UNIDENTIFIED";

    public record WatchNameItem(String deviceId, String displayName) {
    }

    public record PersonNameItem(String worldId, String displayName, String nameSource) {
    }

    public record Snapshot(List<WatchNameItem> watchNames, List<PersonNameItem> personNames) {
    }

    /** 지난 기록 조회 응답의 시각 구간별 사람 표시 이름 한 칸([fromMs, toMs), S15P31A101-835) */
    public record HistoricalNameSpan(String worldId, String displayName, String nameSource, long fromMs, long toMs) {
    }

    public record HistoricalSnapshot(String siteId, LocalDate date, List<HistoricalNameSpan> items) {
    }

    public record WatchNameRecord(long historyId, String siteId, String deviceId, String displayName,
            long changedByUserId, Instant createdAt) {
    }

    public record WorldNameRecord(long historyId, String siteId, LocalDate siteDate, String worldId,
            String displayName, long changedByUserId, Instant createdAt) {
    }

    private record ManualNameState(long historyId, String displayName, long changedByUserId) {
    }

    private record ObservedTrack(String worldId, String splitFrom, List<String> aliases) {
    }

    private record HistoricalManualRow(long historyId, String worldId, String displayName, Instant createdAt) {
    }

    private record HistoricalMergeEdge(String oldWorldId, String survivorWorldId, Instant createdAt) {
    }

    private record HistoricalBindingRow(long bindingId, String deviceId, String worldId, Instant boundAt,
            Instant effectiveEnd, String unbindReason, String handoffFrom, Instant handoffConfirmedAt) {
    }

    private record HistoricalWatchNameRow(long historyId, String deviceId, String displayName, Instant createdAt) {
    }

    private record ResolvedHistoricalName(String displayName, String nameSource) {
    }

    /** 작업 실행 한 번(work_run_window) [startedAt, endedAt). 실행 중이면 endedAt 은 null(S15P31A101-857 이름 이어 쓰기 범위) */
    private record HistoricalRunWindow(Instant startedAt, Instant endedAt) {
        boolean contains(Instant at) {
            return !at.isBefore(startedAt) && (endedAt == null || at.isBefore(endedAt));
        }
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final SiteLookup sites;
    private final WatchBindings bindings;
    private final Clock clock;
    private final ApplicationEventPublisher events;
    private final TransactionTemplate requiresNewTx;

    public PersonDisplayNames(NamedParameterJdbcTemplate jdbc, SiteLookup sites, WatchBindings bindings, Clock clock,
            ApplicationEventPublisher events, PlatformTransactionManager txManager) {
        this.jdbc = jdbc;
        this.sites = sites;
        this.bindings = bindings;
        this.clock = clock;
        this.events = events;
        TransactionTemplate template = new TransactionTemplate(txManager);
        template.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.requiresNewTx = template;
    }

    /** 앞뒤 모든 유니코드 공백을 잘라내고 1~20자·제어문자 없음을 검증한다(계약 $defs/person_display_name 와 일치) */
    public static String normalizeDisplayName(String raw) {
        if (raw == null) {
            throw new BusinessException(ErrorCode.Watch.INVALID_DISPLAY_NAME);
        }
        String trimmed = trimUnicodeSpace(raw);
        int codePoints = trimmed.codePointCount(0, trimmed.length());
        if (trimmed.isEmpty() || codePoints < 1 || codePoints > MAX_DISPLAY_NAME_LENGTH
                || trimmed.length() > MAX_DISPLAY_NAME_LENGTH) {
            throw new BusinessException(ErrorCode.Watch.INVALID_DISPLAY_NAME);
        }
        for (int i = 0; i < trimmed.length();) {
            int cp = trimmed.codePointAt(i);
            int type = Character.getType(cp);
            if (Character.isISOControl(cp) || type == Character.CONTROL || type == Character.FORMAT
                    || type == Character.LINE_SEPARATOR || type == Character.PARAGRAPH_SEPARATOR
                    || type == Character.SURROGATE) {
                throw new BusinessException(ErrorCode.Watch.INVALID_DISPLAY_NAME);
            }
            i += Character.charCount(cp);
        }
        return trimmed;
    }

    /** World ID 검증: 1~64자, 공백·제어문자 불가 */
    public static String normalizeWorldId(String raw) {
        if (raw == null) {
            throw new BusinessException(ErrorCode.Watch.INVALID_WORLD_ID);
        }
        String trimmed = trimUnicodeSpace(raw);
        if (trimmed.isEmpty() || trimmed.length() > 64) {
            throw new BusinessException(ErrorCode.Watch.INVALID_WORLD_ID);
        }
        for (int i = 0; i < trimmed.length();) {
            int cp = trimmed.codePointAt(i);
            int type = Character.getType(cp);
            if (Character.isWhitespace(cp) || Character.isSpaceChar(cp) || Character.isISOControl(cp)
                    || type == Character.CONTROL || type == Character.FORMAT) {
                throw new BusinessException(ErrorCode.Watch.INVALID_WORLD_ID);
            }
            i += Character.charCount(cp);
        }
        return trimmed;
    }

    private static String trimUnicodeSpace(String s) {
        int start = 0;
        int end = s.length();
        while (start < end) {
            int cp = s.codePointAt(start);
            if (!isSpaceOrBom(cp)) {
                break;
            }
            start += Character.charCount(cp);
        }
        while (end > start) {
            int cp = Character.codePointBefore(s, end);
            if (!isSpaceOrBom(cp)) {
                break;
            }
            end -= Character.charCount(cp);
        }
        return s.substring(start, end);
    }

    private static boolean isSpaceOrBom(int cp) {
        return Character.isWhitespace(cp) || Character.isSpaceChar(cp) || cp == 0xFEFF;
    }

    /** (a) 워치 기기 표시 이름 지정(현장 단위로 지속, 추가 전용) */
    @Transactional
    public WatchNameRecord setWatchDisplayName(String deviceId, String rawDisplayName, long actorUserId) {
        requireActiveWatch(deviceId);
        String name = normalizeDisplayName(rawDisplayName);
        String siteId = sites.siteId();
        lockSite(siteId);
        Instant now = clock.instant();
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("device", deviceId)
                .addValue("name", name)
                .addValue("actor", actorUserId)
                .addValue("now", OffsetDateTime.ofInstant(now, ZoneOffset.UTC));
        Long id = jdbc.queryForObject("""
                INSERT INTO watch_display_name_history (site_id, device_id, display_name, action, changed_by_user_id, created_at)
                VALUES (:site, :device, :name, 'SET', :actor, :now)
                RETURNING history_id
                """, params, Long.class);
        events.publishEvent(new PersonDisplayNamesChanged());
        return new WatchNameRecord(id == null ? -1 : id, siteId, deviceId, name, actorUserId, now);
    }

    /** 워치 기기 표시 이름 비움(이력 행 CLEAR 추가) */
    @Transactional
    public void clearWatchDisplayName(String deviceId, long actorUserId) {
        requireActiveWatch(deviceId);
        String siteId = sites.siteId();
        lockSite(siteId);
        Instant now = clock.instant();
        jdbc.update("""
                INSERT INTO watch_display_name_history (site_id, device_id, display_name, action, changed_by_user_id, created_at)
                VALUES (:site, :device, NULL, 'CLEAR', :actor, :now)
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("device", deviceId)
                .addValue("actor", actorUserId)
                .addValue("now", OffsetDateTime.ofInstant(now, ZoneOffset.UTC)));
        events.publishEvent(new PersonDisplayNamesChanged());
    }

    /** (b) 워치 없는 사람의 현재 World ID 표시 이름 지정(당일 Asia/Seoul 한정, 병합 따라감, 추가 전용) */
    @Transactional
    public WorldNameRecord setPersonWorldDisplayName(String rawWorldId, String rawDisplayName, long actorUserId) {
        String worldId = normalizeWorldId(rawWorldId);
        String name = normalizeDisplayName(rawDisplayName);
        String siteId = sites.siteId();
        LocalDate today = today();
        lockSiteDate(siteId, today);
        Instant now = clock.instant();
        OffsetDateTime nowUtc = OffsetDateTime.ofInstant(now, ZoneOffset.UTC);
        ensureUnidentifiedNumberLocked(siteId, today, worldId, "INITIAL", null, nowUtc);
        Long id = jdbc.queryForObject("""
                INSERT INTO person_world_name_history
                    (site_id, site_date, world_id, display_name, action, source_world_id, changed_by_user_id, created_at)
                VALUES (:site, :date, :world, :name, 'SET', NULL, :actor, :now)
                RETURNING history_id
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", today)
                .addValue("world", worldId)
                .addValue("name", name)
                .addValue("actor", actorUserId)
                .addValue("now", nowUtc), Long.class);
        events.publishEvent(new PersonDisplayNamesChanged());
        return new WorldNameRecord(id == null ? -1 : id, siteId, today, worldId, name, actorUserId, now);
    }

    /** World ID 당일 지정 이름 비움(이력 행 CLEAR 추가) */
    @Transactional
    public void clearPersonWorldDisplayName(String rawWorldId, long actorUserId) {
        String worldId = normalizeWorldId(rawWorldId);
        String siteId = sites.siteId();
        LocalDate today = today();
        lockSiteDate(siteId, today);
        Instant now = clock.instant();
        jdbc.update("""
                INSERT INTO person_world_name_history
                    (site_id, site_date, world_id, display_name, action, source_world_id, changed_by_user_id, created_at)
                VALUES (:site, :date, :world, NULL, 'CLEAR', NULL, :actor, :now)
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", today)
                .addValue("world", worldId)
                .addValue("actor", actorUserId)
                .addValue("now", OffsetDateTime.ofInstant(now, ZoneOffset.UTC)));
        events.publishEvent(new PersonDisplayNamesChanged());
    }

    /**
     * world_state 커밋 직후(HealthState 보다 먼저) 새 트랙 미확인 번호 배정·병합 승계·분할 새 번호 배정을 반영한다.
     * 이미 배정·병합 처리된 평상시 10 Hz 프레임에서는 잠금·쓰기 없이 빠르게 통과한다.
     */
    @Order(0)
    @TransactionalEventListener(fallbackExecution = true)
    void onWorldState(WorldStateStored stored) {
        observeWorldPayload(stored.envelope().payload());
    }

    /** 워치 배정이 새로 생기면 그 World ID 가 아직 미확인 번호가 없을 때도 바로 번호를 잡아 둔다 */
    @Order(0)
    @TransactionalEventListener(fallbackExecution = true)
    void onWatchBindingChanged(WatchBindingChanged changed) {
        String siteId = findSingleSiteOrNull();
        if (siteId == null) {
            return;
        }
        var bindingOpt = bindings.activeForDevice(changed.deviceId());
        if (bindingOpt.isEmpty()) {
            events.publishEvent(new PersonDisplayNamesChanged());
            return;
        }
        String worldId = bindingOpt.get().worldId();
        LocalDate today = today();
        if (!hasAllocatedWorlds(siteId, today, List.of(worldId)).contains(worldId)) {
            requiresNewTx.executeWithoutResult(status -> {
                lockSiteDate(siteId, today);
                ensureUnidentifiedNumberLocked(siteId, today, worldId, "INITIAL", null,
                        OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC));
            });
        }
        events.publishEvent(new PersonDisplayNamesChanged());
    }

    /**
     * 워치 배정 자리 이어받기(S15P31A101-793, WatchMotionMatcher): 사라진 {@code vanishedWorldId} 의 이름·미확인 번호를 새
     * {@code newWorldId} 가 이어받는다. Tier 0 병합(aliases)과 같은 승계 규칙이다. 화면 이름표 번호가 바뀌지 않아야 관리자가
     * 같은 사람으로 알아본다. 같은 쌍을 다시 받으면(멱등 기록) 아무것도 하지 않는다
     */
    public void recordHandoff(String vanishedWorldId, String newWorldId) {
        if (vanishedWorldId == null || newWorldId == null || vanishedWorldId.equals(newWorldId) || vanishedWorldId.length() > 64
                || newWorldId.length() > 64) {
            return;
        }
        String siteId = findSingleSiteOrNull();
        if (siteId == null) {
            return;
        }
        LocalDate today = today();
        Boolean mutated = requiresNewTx.execute(status -> {
            lockSiteDate(siteId, today);
            return applySingleMergeLocked(siteId, today, vanishedWorldId, newWorldId,
                    OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC));
        });
        if (Boolean.TRUE.equals(mutated)) {
            events.publishEvent(new PersonDisplayNamesChanged());
        }
    }

    public void observeWorldPayload(JsonNode payload) {
        if (payload == null) {
            return;
        }
        JsonNode tracksNode = payload.path("tracks");
        if (!tracksNode.isArray() || tracksNode.isEmpty()) {
            return;
        }
        List<ObservedTrack> observed = new ArrayList<>();
        Set<String> allCandidateIds = new LinkedHashSet<>();
        Map<String, List<String>> mergesBySurvivor = new LinkedHashMap<>();
        for (JsonNode track : tracksNode) {
            String id = track.path("world_id").asString("");
            if (id.isEmpty() || id.length() > 64 || "LOST".equals(track.path("track_lifecycle").asString(""))) {
                continue;
            }
            String splitFrom = track.path("split_from").asString("");
            if (splitFrom.length() > 64 || splitFrom.equals(id)) {
                splitFrom = "";
            }
            List<String> aliases = new ArrayList<>();
            for (JsonNode aliasNode : track.path("aliases")) {
                String old = aliasNode.asString("");
                if (!old.isEmpty() && old.length() <= 64 && !old.equals(id)) {
                    aliases.add(old);
                    allCandidateIds.add(old);
                }
            }
            if (!aliases.isEmpty()) {
                mergesBySurvivor.computeIfAbsent(id, k -> new ArrayList<>()).addAll(aliases);
            }
            if (!splitFrom.isEmpty()) {
                allCandidateIds.add(splitFrom);
            }
            allCandidateIds.add(id);
            observed.add(new ObservedTrack(id, splitFrom, aliases));
        }
        if (observed.isEmpty()) {
            return;
        }
        String siteId = findSingleSiteOrNull();
        if (siteId == null) {
            return;
        }
        LocalDate today = today();
        Set<String> alreadyAllocated = hasAllocatedWorlds(siteId, today, new ArrayList<>(allCandidateIds));
        boolean needsAllocation = observed.stream().anyMatch(t -> !alreadyAllocated.contains(t.worldId())
                || (!t.splitFrom().isEmpty() && !alreadyAllocated.contains(t.splitFrom())));
        boolean needsMerge = !mergesBySurvivor.isEmpty() && hasUnprocessedMerge(siteId, today, mergesBySurvivor);
        if (!needsAllocation && !needsMerge) {
            return;
        }

        Boolean mutated = requiresNewTx.execute(status -> applyWorldChangesLocked(siteId, today, observed));
        if (Boolean.TRUE.equals(mutated)) {
            events.publishEvent(new PersonDisplayNamesChanged());
        }
    }

    private boolean applyWorldChangesLocked(String siteId, LocalDate today, List<ObservedTrack> observed) {
        lockSiteDate(siteId, today);
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        boolean changed = false;

        // 1) 분할 부모가 아직 번호가 없으면 자식보다 먼저 배정해 부모 번호가 앞서게 한다
        for (ObservedTrack t : observed) {
            if (!t.splitFrom().isEmpty()) {
                changed |= ensureUnidentifiedNumberLocked(siteId, today, t.splitFrom(), "INITIAL", null, now);
            }
        }

        // 2) 병합(aliases): 생존 ID 가 옛 ID 의 관리자 이름(더 최근 것 우선)과 미확인 번호(더 작은 것 우선)를 이어받는다
        for (ObservedTrack t : observed) {
            for (String old : t.aliases()) {
                changed |= applySingleMergeLocked(siteId, today, old, t.worldId(), now);
            }
        }

        // 3) 아직 번호가 없는 트랙에 새 미확인 번호를 배정한다(split_from 이 있으면 SPLIT 으로 새 번호)
        for (ObservedTrack t : observed) {
            String reason = t.splitFrom().isEmpty() ? "INITIAL" : "SPLIT";
            String sourceWorldId = t.splitFrom().isEmpty() ? null : t.splitFrom();
            changed |= ensureUnidentifiedNumberLocked(siteId, today, t.worldId(), reason, sourceWorldId, now);
        }
        return changed;
    }

    private boolean applySingleMergeLocked(String siteId, LocalDate today, String oldWorldId, String survivorId,
            OffsetDateTime now) {
        int insertedMerge = jdbc.update("""
                INSERT INTO person_world_merge_log (site_id, site_date, old_world_id, survivor_world_id, created_at)
                VALUES (:site, :date, :old, :survivor, :now)
                ON CONFLICT (site_id, site_date, old_world_id, survivor_world_id) DO NOTHING
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", today)
                .addValue("old", oldWorldId)
                .addValue("survivor", survivorId)
                .addValue("now", now));
        if (insertedMerge == 0) {
            return false;
        }

        // (a) 관리자 World ID 지정 이름 승계: 옛 ID 에 유효한 이름이 있고 생존 ID 에 없거나 옛 ID 의 지정이 더 최근이면 승계 행 추가
        ManualNameState oldManual = latestManualNameState(siteId, today, oldWorldId);
        ManualNameState survivorManual = latestManualNameState(siteId, today, survivorId);
        if (oldManual != null && oldManual.displayName() != null
                && (survivorManual == null || survivorManual.displayName() == null
                        || oldManual.historyId() > survivorManual.historyId())) {
            jdbc.update("""
                    INSERT INTO person_world_name_history
                        (site_id, site_date, world_id, display_name, action, source_world_id, changed_by_user_id, created_at)
                    VALUES (:site, :date, :world, :name, 'MERGE', :source, :actor, :now)
                    """, new MapSqlParameterSource()
                    .addValue("site", siteId)
                    .addValue("date", today)
                    .addValue("world", survivorId)
                    .addValue("name", oldManual.displayName())
                    .addValue("source", oldWorldId)
                    .addValue("actor", oldManual.changedByUserId())
                    .addValue("now", now));
        }

        // (b) 미확인 N 번호 승계: 둘 중 먼저 받은 작은 번호를 이어받는다(없으면 새 번호 배정)
        Integer oldSeq = latestUnidentifiedSeq(siteId, today, oldWorldId);
        Integer survivorSeq = latestUnidentifiedSeq(siteId, today, survivorId);
        if (oldSeq != null && (survivorSeq == null || oldSeq < survivorSeq)) {
            jdbc.update("""
                    INSERT INTO person_unidentified_number_history
                        (site_id, site_date, world_id, seq_no, reason, source_world_id, created_at)
                    VALUES (:site, :date, :world, :seq, 'MERGE', :source, :now)
                    """, new MapSqlParameterSource()
                    .addValue("site", siteId)
                    .addValue("date", today)
                    .addValue("world", survivorId)
                    .addValue("seq", oldSeq)
                    .addValue("source", oldWorldId)
                    .addValue("now", now));
        } else if (survivorSeq == null) {
            ensureUnidentifiedNumberLocked(siteId, today, survivorId, "INITIAL", null, now);
        }
        return true;
    }

    private boolean ensureUnidentifiedNumberLocked(String siteId, LocalDate today, String worldId, String reason,
            String sourceWorldId, OffsetDateTime now) {
        if (latestUnidentifiedSeq(siteId, today, worldId) != null) {
            return false;
        }
        Integer nextSeq = jdbc.queryForObject("""
                SELECT COALESCE(MAX(seq_no), 0) + 1
                FROM person_unidentified_number_history
                WHERE site_id = :site AND site_date = :date
                """, new MapSqlParameterSource().addValue("site", siteId).addValue("date", today), Integer.class);
        jdbc.update("""
                INSERT INTO person_unidentified_number_history
                    (site_id, site_date, world_id, seq_no, reason, source_world_id, created_at)
                VALUES (:site, :date, :world, :seq, :reason, :source, :now)
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", today)
                .addValue("world", worldId)
                .addValue("seq", nextSeq == null ? 1 : nextSeq)
                .addValue("reason", reason)
                .addValue("source", sourceWorldId)
                .addValue("now", now));
        return true;
    }

    /** viewer_health(GET /api/health · /topic/health)에 실을 현재 현장·오늘(Asia/Seoul) 기준 스냅샷 */
    public Snapshot snapshotForCurrentSite() {
        String siteId = findSingleSiteOrNull();
        if (siteId == null) {
            return new Snapshot(List.of(), List.of());
        }
        return snapshot(siteId, today());
    }

    public Snapshot snapshot(String siteId, LocalDate siteDate) {
        MapSqlParameterSource siteParam = new MapSqlParameterSource("site", siteId);
        Map<String, String> watchNameByDevice = new LinkedHashMap<>();
        List<WatchNameItem> watchItems = new ArrayList<>();
        jdbc.query("""
                SELECT h.device_id, h.display_name
                FROM (
                    SELECT DISTINCT ON (device_id) device_id, display_name
                    FROM watch_display_name_history
                    WHERE site_id = :site
                    ORDER BY device_id, history_id DESC
                ) h
                JOIN device d ON d.device_id = h.device_id
                WHERE d.kind = 'WATCH' AND d.revoked_at IS NULL AND h.display_name IS NOT NULL
                ORDER BY h.device_id
                """, siteParam, (RowCallbackHandler) rs -> {
            String deviceId = rs.getString("device_id");
            String displayName = rs.getString("display_name");
            watchNameByDevice.put(deviceId, displayName);
            watchItems.add(new WatchNameItem(deviceId, displayName));
        });

        Map<String, String> deviceByWorld = new HashMap<>();
        for (WatchBindings.Binding binding : bindings.active()) {
            deviceByWorld.put(binding.worldId(), binding.deviceId());
        }

        MapSqlParameterSource dayParams = new MapSqlParameterSource().addValue("site", siteId).addValue("date", siteDate);
        Map<String, String> manualByWorld = new HashMap<>();
        jdbc.query("""
                SELECT DISTINCT ON (world_id) world_id, display_name
                FROM person_world_name_history
                WHERE site_id = :site AND site_date = :date
                ORDER BY world_id, history_id DESC
                """, dayParams, (RowCallbackHandler) rs -> {
            String name = rs.getString("display_name");
            if (name != null) {
                manualByWorld.put(rs.getString("world_id"), name);
            }
        });

        Map<String, Integer> seqByWorld = new HashMap<>();
        jdbc.query("""
                SELECT DISTINCT ON (world_id) world_id, seq_no
                FROM person_unidentified_number_history
                WHERE site_id = :site AND site_date = :date
                ORDER BY world_id, history_id DESC
                """, dayParams, (RowCallbackHandler) rs -> {
            seqByWorld.put(rs.getString("world_id"), rs.getInt("seq_no"));
        });

        Map<String, String> mergeParent = new HashMap<>();
        jdbc.query("""
                SELECT old_world_id, survivor_world_id
                FROM person_world_merge_log
                WHERE site_id = :site AND site_date = :date
                ORDER BY merge_id ASC
                """, dayParams, (RowCallbackHandler) rs -> {
            mergeParent.put(rs.getString("old_world_id"), rs.getString("survivor_world_id"));
        });

        Set<String> allWorldIds = new TreeSet<>(Comparator.naturalOrder());
        allWorldIds.addAll(seqByWorld.keySet());
        allWorldIds.addAll(manualByWorld.keySet());
        allWorldIds.addAll(deviceByWorld.keySet());
        allWorldIds.addAll(mergeParent.keySet());

        List<PersonNameItem> personItems = new ArrayList<>(allWorldIds.size());
        for (String worldId : allWorldIds) {
            String canonical = canonicalWorldId(worldId, mergeParent);
            String manual = manualByWorld.get(canonical);
            if (manual == null && !canonical.equals(worldId)) {
                manual = manualByWorld.get(worldId);
            }
            if (manual != null) {
                personItems.add(new PersonNameItem(worldId, manual, SOURCE_WORLD_MANUAL));
                continue;
            }

            String deviceId = deviceByWorld.get(canonical);
            if (deviceId == null && !canonical.equals(worldId)) {
                deviceId = deviceByWorld.get(worldId);
            }
            String watchName = deviceId == null ? null : watchNameByDevice.get(deviceId);
            if (watchName != null) {
                personItems.add(new PersonNameItem(worldId, watchName, SOURCE_WATCH_DEVICE));
                continue;
            }

            Integer seq = seqByWorld.get(canonical);
            if (seq == null && !canonical.equals(worldId)) {
                seq = seqByWorld.get(worldId);
            }
            if (seq != null) {
                personItems.add(new PersonNameItem(worldId, "미확인 " + seq, SOURCE_UNIDENTIFIED));
            }
        }
        return new Snapshot(watchItems, personItems);
    }

    /** 지난 기록 조회 API(GET /api/people/display-names?date=YYYY-MM-DD, S15P31A101-835)용 현재 현장 시각별 표시 이름 */
    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public HistoricalSnapshot historicalForCurrentSite(LocalDate siteDate) {
        return historicalSnapshot(sites.siteId(), siteDate);
    }

    /**
     * 특정 현장·날짜(Asia/Seoul)의 [00:00, 다음날 00:00) 구간에 나온 World ID 별 시각별 표시 이름을 계산한다(읽기 전용).
     * <ul>
     *   <li>우선순위: World 지정 이름(그날, WORLD_MANUAL) &gt; 그 시각 그 World ID 에 배정된 워치의 그 시각 표시 이름(WATCH_DEVICE) &gt; 없음(생략)</li>
     *   <li>병합(person_world_merge_log)·이어받기(watch_binding.handoff_from) 승계 규칙을 반영한다</li>
     *   <li>대조가 잠깐 놓쳐(MATCH_LOST) 배정이 끊긴 World ID 는 같은 작업 실행 안에서 다음 배정까지 직전 워치 이름을 이어 쓴다.
     *       그 워치가 계보 밖 다른 World ID 에 배정되면 그때부터 이어 쓰지 않는다(S15P31A101-857)</li>
     *   <li>인접한 같은 (world_id, display_name, name_source) 구간은 하나로 합친 반열린 구간 [from_ms, to_ms) 으로 돌려준다</li>
     * </ul>
     */
    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public HistoricalSnapshot historicalSnapshot(String siteId, LocalDate siteDate) {
        Instant dayStart = siteDate.atStartOfDay(SITE_ZONE).toInstant();
        Instant dayEnd = siteDate.plusDays(1).atStartOfDay(SITE_ZONE).toInstant();
        OffsetDateTime dayStartUtc = OffsetDateTime.ofInstant(dayStart, ZoneOffset.UTC);
        OffsetDateTime dayEndUtc = OffsetDateTime.ofInstant(dayEnd, ZoneOffset.UTC);

        MapSqlParameterSource dayParams = new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", siteDate);

        // 1) 당일 World ID 수동 지정·병합 승계·해제 이력
        Map<String, List<HistoricalManualRow>> manualByWorld = new HashMap<>();
        List<HistoricalManualRow> allManualRows = new ArrayList<>();
        jdbc.query("""
                SELECT history_id, world_id, display_name, created_at
                FROM person_world_name_history
                WHERE site_id = :site AND site_date = :date
                ORDER BY created_at ASC, history_id ASC
                """, dayParams, (RowCallbackHandler) rs -> {
            HistoricalManualRow row = new HistoricalManualRow(
                    rs.getLong("history_id"),
                    rs.getString("world_id"),
                    rs.getString("display_name"),
                    rs.getTimestamp("created_at").toInstant());
            allManualRows.add(row);
            manualByWorld.computeIfAbsent(row.worldId(), k -> new ArrayList<>()).add(row);
        });

        // 2) 당일 병합(aliases) 및 recordHandoff 계보
        Map<String, HistoricalMergeEdge> edgeByPair = new LinkedHashMap<>();
        jdbc.query("""
                SELECT old_world_id, survivor_world_id, created_at
                FROM person_world_merge_log
                WHERE site_id = :site AND site_date = :date
                ORDER BY created_at ASC, merge_id ASC
                """, dayParams, (RowCallbackHandler) rs -> {
            String oldId = rs.getString("old_world_id");
            String survivorId = rs.getString("survivor_world_id");
            Instant createdAt = rs.getTimestamp("created_at").toInstant();
            registerEdge(edgeByPair, oldId, survivorId, createdAt);
        });

        // 3) 그 날짜 구간 [dayStart, dayEnd) 과 겹치는 워치 배정 이력(이후 폐기된 워치라도 그 시각에는 유효)
        Map<String, List<HistoricalBindingRow>> bindingsByWorld = new HashMap<>();
        List<HistoricalBindingRow> allBindings = new ArrayList<>();
        Set<String> boundDeviceIds = new LinkedHashSet<>();
        jdbc.query("""
                SELECT b.binding_id, b.device_id, b.world_id, b.bound_at, b.unbound_at,
                       b.unbind_reason, b.handoff_from, b.handoff_confirmed_at, d.revoked_at
                FROM watch_binding b
                JOIN device d ON d.device_id = b.device_id
                WHERE d.kind = 'WATCH'
                  AND b.bound_at < :dayEnd
                  AND (b.unbound_at IS NULL OR b.unbound_at > :dayStart)
                  AND (d.revoked_at IS NULL OR d.revoked_at > :dayStart)
                ORDER BY b.bound_at ASC, b.binding_id ASC
                """, new MapSqlParameterSource()
                .addValue("dayStart", dayStartUtc)
                .addValue("dayEnd", dayEndUtc), (RowCallbackHandler) rs -> {
            Instant boundAt = rs.getTimestamp("bound_at").toInstant();
            Timestamp unboundTs = rs.getTimestamp("unbound_at");
            Timestamp revokedTs = rs.getTimestamp("revoked_at");
            Timestamp confirmedTs = rs.getTimestamp("handoff_confirmed_at");
            Instant effectiveEnd = earliest(
                    unboundTs == null ? null : unboundTs.toInstant(),
                    revokedTs == null ? null : revokedTs.toInstant());
            if (effectiveEnd != null && !effectiveEnd.isAfter(boundAt)) {
                return;
            }
            HistoricalBindingRow row = new HistoricalBindingRow(
                    rs.getLong("binding_id"),
                    rs.getString("device_id"),
                    rs.getString("world_id"),
                    boundAt,
                    effectiveEnd,
                    rs.getString("unbind_reason"),
                    rs.getString("handoff_from"),
                    confirmedTs == null ? null : confirmedTs.toInstant());
            allBindings.add(row);
            bindingsByWorld.computeIfAbsent(row.worldId(), k -> new ArrayList<>()).add(row);
            boundDeviceIds.add(row.deviceId());

            // 당일 발생한 자리 이어받기(handoff_from)도 승계 계보에 반영한다(미확정 반증 해제 MATCH_LOST 는 제외)
            boolean rejectedTentative = row.handoffConfirmedAt() == null && "MATCH_LOST".equals(row.unbindReason());
            if (row.handoffFrom() != null && !rejectedTentative
                    && !row.boundAt().isBefore(dayStart) && row.boundAt().isBefore(dayEnd)) {
                registerEdge(edgeByPair, row.handoffFrom(), row.worldId(), row.boundAt());
            }
        });

        // 3-1) 그 날짜와 겹치는 작업 실행 구간. 배정이 잠깐 끊긴 동안 이름을 이어 쓰는 범위다(S15P31A101-857)
        List<HistoricalRunWindow> runs = new ArrayList<>();
        jdbc.query("""
                SELECT started_at, ended_at
                FROM work_run_window
                WHERE site_id = :site AND started_at < :dayEnd AND (ended_at IS NULL OR ended_at > :dayStart)
                ORDER BY started_at ASC
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("dayStart", dayStartUtc)
                .addValue("dayEnd", dayEndUtc), (RowCallbackHandler) rs -> {
            Timestamp endedTs = rs.getTimestamp("ended_at");
            runs.add(new HistoricalRunWindow(rs.getTimestamp("started_at").toInstant(),
                    endedTs == null ? null : endedTs.toInstant()));
        });
        Map<String, List<HistoricalBindingRow>> bindingsByDevice = new HashMap<>();
        for (HistoricalBindingRow row : allBindings) {
            bindingsByDevice.computeIfAbsent(row.deviceId(), k -> new ArrayList<>()).add(row);
        }

        // 4) 배정된 워치 기기들의 dayEnd 이전 표시 이름 이력(이전 날 지정분 포함)
        Map<String, List<HistoricalWatchNameRow>> watchNamesByDevice = new HashMap<>();
        List<HistoricalWatchNameRow> allWatchNameRows = new ArrayList<>();
        if (!boundDeviceIds.isEmpty()) {
            jdbc.query("""
                    SELECT history_id, device_id, display_name, created_at
                    FROM watch_display_name_history
                    WHERE site_id = :site AND device_id IN (:devices) AND created_at < :dayEnd
                    ORDER BY created_at ASC, history_id ASC
                    """, new MapSqlParameterSource()
                    .addValue("site", siteId)
                    .addValue("devices", boundDeviceIds)
                    .addValue("dayEnd", dayEndUtc), (RowCallbackHandler) rs -> {
                HistoricalWatchNameRow row = new HistoricalWatchNameRow(
                        rs.getLong("history_id"),
                        rs.getString("device_id"),
                        rs.getString("display_name"),
                        rs.getTimestamp("created_at").toInstant());
                allWatchNameRows.add(row);
                watchNamesByDevice.computeIfAbsent(row.deviceId(), k -> new ArrayList<>()).add(row);
            });
        }

        List<HistoricalMergeEdge> allEdges = new ArrayList<>(edgeByPair.values());
        Map<String, String> mergeParent = new HashMap<>();
        for (HistoricalMergeEdge edge : allEdges) {
            mergeParent.put(edge.oldWorldId(), edge.survivorWorldId());
        }

        Set<String> candidateWorlds = new TreeSet<>(Comparator.naturalOrder());
        candidateWorlds.addAll(manualByWorld.keySet());
        candidateWorlds.addAll(bindingsByWorld.keySet());
        for (HistoricalMergeEdge edge : allEdges) {
            candidateWorlds.add(edge.oldWorldId());
            candidateWorlds.add(edge.survivorWorldId());
        }
        if (candidateWorlds.isEmpty()) {
            return new HistoricalSnapshot(siteId, siteDate, List.of());
        }

        // 모든 상태 변화 시각을 [dayStart, dayEnd] 안으로 모아 구간 분할점(sweep-line)을 만든다
        TreeSet<Instant> changePoints = new TreeSet<>();
        changePoints.add(dayStart);
        changePoints.add(dayEnd);
        for (HistoricalManualRow row : allManualRows) {
            changePoints.add(clamp(row.createdAt(), dayStart, dayEnd));
        }
        for (HistoricalMergeEdge edge : allEdges) {
            changePoints.add(clamp(edge.createdAt(), dayStart, dayEnd));
        }
        for (HistoricalBindingRow row : allBindings) {
            changePoints.add(clamp(row.boundAt(), dayStart, dayEnd));
            if (row.effectiveEnd() != null) {
                changePoints.add(clamp(row.effectiveEnd(), dayStart, dayEnd));
            }
        }
        for (HistoricalWatchNameRow row : allWatchNameRows) {
            if (row.createdAt().isAfter(dayStart) && row.createdAt().isBefore(dayEnd)) {
                changePoints.add(row.createdAt());
            }
        }
        for (HistoricalRunWindow run : runs) {
            if (run.endedAt() != null) {
                changePoints.add(clamp(run.endedAt(), dayStart, dayEnd)); // 이어 쓰기는 실행 끝에서 멈춘다
            }
        }
        List<Instant> points = new ArrayList<>(changePoints);

        Map<String, Set<String>> fullComponents = new HashMap<>();
        for (String worldId : candidateWorlds) {
            if (!fullComponents.containsKey(worldId)) {
                Set<String> component = connectedComponent(worldId, allEdges, null);
                for (String member : component) {
                    fullComponents.put(member, component);
                }
            }
        }

        List<HistoricalNameSpan> result = new ArrayList<>();
        for (String worldId : candidateWorlds) {
            String canonical = canonicalWorldId(worldId, mergeParent);
            Set<String> fullGroup = fullComponents.getOrDefault(worldId, Set.of(worldId));
            ResolvedHistoricalName openName = null;
            long openFromMs = 0L;
            long openToMs = 0L;

            for (int i = 0; i < points.size() - 1; i++) {
                Instant segStart = points.get(i);
                Instant segEnd = points.get(i + 1);
                if (!segStart.isBefore(segEnd)) {
                    continue;
                }
                ResolvedHistoricalName resolved = resolveHistoricalAt(
                        worldId, canonical, fullGroup, segStart, allEdges,
                        manualByWorld, bindingsByWorld, watchNamesByDevice);
                if (resolved == null) {
                    resolved = carriedHistoricalName(worldId, fullGroup, segStart, allEdges, runs,
                            bindingsByWorld, bindingsByDevice, watchNamesByDevice);
                }
                long segStartMs = segStart.toEpochMilli();
                long segEndMs = segEnd.toEpochMilli();
                if (resolved == null) {
                    if (openName != null) {
                        result.add(new HistoricalNameSpan(
                                worldId, openName.displayName(), openName.nameSource(), openFromMs, openToMs));
                        openName = null;
                    }
                } else if (openName != null && openName.equals(resolved) && openToMs == segStartMs) {
                    openToMs = segEndMs;
                } else {
                    if (openName != null) {
                        result.add(new HistoricalNameSpan(
                                worldId, openName.displayName(), openName.nameSource(), openFromMs, openToMs));
                    }
                    openName = resolved;
                    openFromMs = segStartMs;
                    openToMs = segEndMs;
                }
            }
            if (openName != null) {
                result.add(new HistoricalNameSpan(
                        worldId, openName.displayName(), openName.nameSource(), openFromMs, openToMs));
            }
        }
        return new HistoricalSnapshot(siteId, siteDate, result);
    }

    private static void registerEdge(Map<String, HistoricalMergeEdge> edgeByPair, String oldId, String survivorId,
            Instant createdAt) {
        if (oldId == null || survivorId == null || oldId.equals(survivorId)) {
            return;
        }
        String key = oldId + ">" + survivorId;
        HistoricalMergeEdge existing = edgeByPair.get(key);
        if (existing == null || createdAt.isBefore(existing.createdAt())) {
            edgeByPair.put(key, new HistoricalMergeEdge(oldId, survivorId, createdAt));
        }
    }

    private static ResolvedHistoricalName resolveHistoricalAt(
            String worldId,
            String canonical,
            Set<String> fullGroup,
            Instant at,
            List<HistoricalMergeEdge> allEdges,
            Map<String, List<HistoricalManualRow>> manualByWorld,
            Map<String, List<HistoricalBindingRow>> bindingsByWorld,
            Map<String, List<HistoricalWatchNameRow>> watchNamesByDevice) {
        Set<String> mergedNow = fullGroup.size() == 1
                ? fullGroup
                : connectedComponent(worldId, allEdges, at);

        // 1순위: 당일 World ID 수동 지정 이름(병합·이어받기 승계 포함)
        HistoricalManualRow manual = latestManual(mergedNow, at, manualByWorld);
        if (manual == null && fullGroup.size() > mergedNow.size()) {
            // 병합·이어받기 직전 시각의 옛 ID·새 ID 도 같은 사람의 당일 지정 이름을 이어받는다
            HistoricalManualRow inherited = latestManual(fullGroup, at, manualByWorld);
            if (inherited != null && inherited.displayName() != null) {
                manual = inherited;
            }
        }
        if (manual != null && manual.displayName() != null) {
            return new ResolvedHistoricalName(manual.displayName(), SOURCE_WORLD_MANUAL);
        }

        // 2순위: 그 시각 그 World ID(또는 병합·이어받기 계보)에 배정된 워치의 그 시각 표시 이름
        String deviceId = activeDeviceFor(worldId, at, bindingsByWorld);
        if (deviceId == null && mergedNow.size() > 1) {
            deviceId = activeDeviceAmong(mergedNow, canonical, at, bindingsByWorld);
        }
        if (deviceId == null && fullGroup.size() > mergedNow.size()) {
            deviceId = activeDeviceAmong(fullGroup, canonical, at, bindingsByWorld);
        }
        if (deviceId != null) {
            HistoricalWatchNameRow watchName = latestWatchName(deviceId, at, watchNamesByDevice);
            if (watchName != null && watchName.displayName() != null) {
                return new ResolvedHistoricalName(watchName.displayName(), SOURCE_WATCH_DEVICE);
            }
        }

        // 3순위: 없음(지난 기록에서는 호출부가 「작업자 #NNNN」으로 표기한다)
        return null;
    }

    /**
     * 배정이 잠깐 끊긴 동안의 이름 이어 쓰기(S15P31A101-857). 시각 {@code at} 에 그 World ID(그 시각까지의 병합·이어받기 계보 포함)에
     * 지금 배정이 없고, 가장 최근에 끝난 배정이 대조 놓침(MATCH_LOST)으로 끝났으며, 그 끝과 {@code at} 이 같은 작업 실행 안이고,
     * 그 사이 그 워치가 계보 밖 다른 World ID 에 배정되지 않았으면, 끝나기 직전 그 워치 이름을 돌려준다(관제 경보 카드의
     * 「그 ID 의 마지막 실제 이름」(-851)과 같은 결과). 관리자 해제(RELEASED)·ID 교정·교체로 끝난 배정, 실행 밖, 지금 다른(이름 없는)
     * 배정이 있으면 이어 쓰지 않는다(모르면 비운다).
     */
    private static ResolvedHistoricalName carriedHistoricalName(
            String worldId,
            Set<String> fullGroup,
            Instant at,
            List<HistoricalMergeEdge> allEdges,
            List<HistoricalRunWindow> runs,
            Map<String, List<HistoricalBindingRow>> bindingsByWorld,
            Map<String, List<HistoricalBindingRow>> bindingsByDevice,
            Map<String, List<HistoricalWatchNameRow>> watchNamesByDevice) {
        Set<String> group = fullGroup.size() == 1 ? fullGroup : connectedComponent(worldId, allEdges, at);
        HistoricalBindingRow last = null;
        for (String member : group) {
            for (HistoricalBindingRow row : bindingsByWorld.getOrDefault(member, List.of())) {
                if (row.boundAt().isAfter(at)) {
                    break;
                }
                if (row.effectiveEnd() == null || at.isBefore(row.effectiveEnd())) {
                    return null; // 지금 배정이 있다(이름 없는 워치 등): 다음 배정까지만 이어 쓴다
                }
                if (last == null || row.effectiveEnd().isAfter(last.effectiveEnd())
                        || (row.effectiveEnd().equals(last.effectiveEnd()) && row.bindingId() > last.bindingId())) {
                    last = row;
                }
            }
        }
        if (last == null || !"MATCH_LOST".equals(last.unbindReason())) {
            return null;
        }
        Instant endedAt = last.effectiveEnd();
        boolean sameRun = false;
        for (HistoricalRunWindow run : runs) {
            if (run.contains(endedAt) && run.contains(at)) {
                sameRun = true;
                break;
            }
        }
        if (!sameRun) {
            return null;
        }
        for (HistoricalBindingRow row : bindingsByDevice.getOrDefault(last.deviceId(), List.of())) {
            if (!fullGroup.contains(row.worldId()) && !row.boundAt().isBefore(endedAt) && !row.boundAt().isAfter(at)) {
                return null; // 그 워치가 다른 사람에게 옮겨 갔다
            }
        }
        HistoricalWatchNameRow watchName = latestWatchName(last.deviceId(), endedAt.minusNanos(1), watchNamesByDevice);
        if (watchName == null || watchName.displayName() == null) {
            return null;
        }
        return new ResolvedHistoricalName(watchName.displayName(), SOURCE_WATCH_DEVICE);
    }

    private static Set<String> connectedComponent(String start, List<HistoricalMergeEdge> edges, Instant upToInclusive) {
        Set<String> visited = new LinkedHashSet<>();
        Deque<String> queue = new ArrayDeque<>();
        visited.add(start);
        queue.add(start);
        while (!queue.isEmpty()) {
            String cur = queue.poll();
            for (HistoricalMergeEdge edge : edges) {
                if (upToInclusive != null && edge.createdAt().isAfter(upToInclusive)) {
                    continue;
                }
                if (cur.equals(edge.oldWorldId()) && visited.add(edge.survivorWorldId())) {
                    queue.add(edge.survivorWorldId());
                } else if (cur.equals(edge.survivorWorldId()) && visited.add(edge.oldWorldId())) {
                    queue.add(edge.oldWorldId());
                }
            }
        }
        return visited;
    }

    private static HistoricalManualRow latestManual(Set<String> worldIds, Instant at,
            Map<String, List<HistoricalManualRow>> manualByWorld) {
        HistoricalManualRow best = null;
        for (String worldId : worldIds) {
            List<HistoricalManualRow> rows = manualByWorld.get(worldId);
            if (rows == null) {
                continue;
            }
            for (HistoricalManualRow row : rows) {
                if (row.createdAt().isAfter(at)) {
                    break;
                }
                if (best == null || isAfterManual(row, best)) {
                    best = row;
                }
            }
        }
        return best;
    }

    private static boolean isAfterManual(HistoricalManualRow candidate, HistoricalManualRow current) {
        int cmp = candidate.createdAt().compareTo(current.createdAt());
        return cmp > 0 || (cmp == 0 && candidate.historyId() > current.historyId());
    }

    private static String activeDeviceFor(String worldId, Instant at,
            Map<String, List<HistoricalBindingRow>> bindingsByWorld) {
        HistoricalBindingRow best = activeBindingFor(worldId, at, bindingsByWorld);
        return best == null ? null : best.deviceId();
    }

    private static HistoricalBindingRow activeBindingFor(String worldId, Instant at,
            Map<String, List<HistoricalBindingRow>> bindingsByWorld) {
        List<HistoricalBindingRow> rows = bindingsByWorld.get(worldId);
        if (rows == null) {
            return null;
        }
        HistoricalBindingRow best = null;
        for (HistoricalBindingRow row : rows) {
            if (row.boundAt().isAfter(at)) {
                break;
            }
            if (row.effectiveEnd() == null || at.isBefore(row.effectiveEnd())) {
                if (best == null || isAfterBinding(row, best)) {
                    best = row;
                }
            }
        }
        return best;
    }

    private static String activeDeviceAmong(Set<String> worldIds, String canonical, Instant at,
            Map<String, List<HistoricalBindingRow>> bindingsByWorld) {
        if (canonical != null && worldIds.contains(canonical)) {
            String canonicalDevice = activeDeviceFor(canonical, at, bindingsByWorld);
            if (canonicalDevice != null) {
                return canonicalDevice;
            }
        }
        HistoricalBindingRow best = null;
        for (String worldId : worldIds) {
            HistoricalBindingRow candidate = activeBindingFor(worldId, at, bindingsByWorld);
            if (candidate != null && (best == null || isAfterBinding(candidate, best))) {
                best = candidate;
            }
        }
        return best == null ? null : best.deviceId();
    }

    private static boolean isAfterBinding(HistoricalBindingRow candidate, HistoricalBindingRow current) {
        int cmp = candidate.boundAt().compareTo(current.boundAt());
        return cmp > 0 || (cmp == 0 && candidate.bindingId() > current.bindingId());
    }

    private static HistoricalWatchNameRow latestWatchName(String deviceId, Instant at,
            Map<String, List<HistoricalWatchNameRow>> watchNamesByDevice) {
        List<HistoricalWatchNameRow> rows = watchNamesByDevice.get(deviceId);
        if (rows == null) {
            return null;
        }
        HistoricalWatchNameRow best = null;
        for (HistoricalWatchNameRow row : rows) {
            if (row.createdAt().isAfter(at)) {
                break;
            }
            best = row;
        }
        return best;
    }

    private static Instant earliest(Instant a, Instant b) {
        if (a == null) {
            return b;
        }
        if (b == null) {
            return a;
        }
        return a.isBefore(b) ? a : b;
    }

    private static Instant clamp(Instant value, Instant min, Instant max) {
        if (value.isBefore(min)) {
            return min;
        }
        if (value.isAfter(max)) {
            return max;
        }
        return value;
    }

    private static String canonicalWorldId(String worldId, Map<String, String> mergeParent) {
        String current = worldId;
        Set<String> visited = new HashSet<>();
        while (visited.add(current)) {
            String next = mergeParent.get(current);
            if (next == null) {
                return current;
            }
            current = next;
        }
        return current;
    }

    private ManualNameState latestManualNameState(String siteId, LocalDate siteDate, String worldId) {
        List<ManualNameState> rows = jdbc.query("""
                SELECT history_id, display_name, changed_by_user_id
                FROM person_world_name_history
                WHERE site_id = :site AND site_date = :date AND world_id = :world
                ORDER BY history_id DESC
                LIMIT 1
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", siteDate)
                .addValue("world", worldId),
                (rs, i) -> new ManualNameState(rs.getLong("history_id"), rs.getString("display_name"),
                        rs.getLong("changed_by_user_id")));
        return rows.isEmpty() ? null : rows.getFirst();
    }

    private Integer latestUnidentifiedSeq(String siteId, LocalDate siteDate, String worldId) {
        List<Integer> rows = jdbc.queryForList("""
                SELECT seq_no
                FROM person_unidentified_number_history
                WHERE site_id = :site AND site_date = :date AND world_id = :world
                ORDER BY history_id DESC
                LIMIT 1
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", siteDate)
                .addValue("world", worldId), Integer.class);
        return rows.isEmpty() ? null : rows.getFirst();
    }

    private Set<String> hasAllocatedWorlds(String siteId, LocalDate siteDate, List<String> worldIds) {
        if (worldIds.isEmpty()) {
            return Set.of();
        }
        return new HashSet<>(jdbc.queryForList("""
                SELECT DISTINCT world_id
                FROM person_unidentified_number_history
                WHERE site_id = :site AND site_date = :date AND world_id IN (:worlds)
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", siteDate)
                .addValue("worlds", worldIds), String.class));
    }

    private boolean hasUnprocessedMerge(String siteId, LocalDate siteDate, Map<String, List<String>> mergesBySurvivor) {
        Set<String> oldIds = new HashSet<>();
        mergesBySurvivor.values().forEach(oldIds::addAll);
        if (oldIds.isEmpty()) {
            return false;
        }
        Set<String> existing = new HashSet<>();
        jdbc.query("""
                SELECT old_world_id, survivor_world_id
                FROM person_world_merge_log
                WHERE site_id = :site AND site_date = :date AND old_world_id IN (:olds)
                """, new MapSqlParameterSource()
                .addValue("site", siteId)
                .addValue("date", siteDate)
                .addValue("olds", oldIds), (RowCallbackHandler) rs -> {
            existing.add(rs.getString("old_world_id") + ">" + rs.getString("survivor_world_id"));
        });
        for (Map.Entry<String, List<String>> entry : mergesBySurvivor.entrySet()) {
            for (String old : entry.getValue()) {
                if (!existing.contains(old + ">" + entry.getKey())) {
                    return true;
                }
            }
        }
        return false;
    }

    private void requireActiveWatch(String deviceId) {
        if (deviceId == null || deviceId.isBlank()) {
            throw new BusinessException(ErrorCode.Watch.WATCH_NOT_FOUND);
        }
        Integer count = jdbc.queryForObject("""
                SELECT count(*) FROM device WHERE device_id = :id AND kind = 'WATCH' AND revoked_at IS NULL
                """, new MapSqlParameterSource("id", deviceId), Integer.class);
        if (count == null || count == 0) {
            throw new BusinessException(ErrorCode.Watch.WATCH_NOT_FOUND);
        }
    }

    private void lockSite(String siteId) {
        jdbc.queryForObject("SELECT pg_advisory_xact_lock(hashtext(:key))",
                new MapSqlParameterSource("key", "watch_display_name:" + siteId), Object.class);
    }

    private void lockSiteDate(String siteId, LocalDate siteDate) {
        jdbc.queryForObject("SELECT pg_advisory_xact_lock(hashtext(:key))",
                new MapSqlParameterSource("key", "person_display_name:" + siteId + ":" + siteDate), Object.class);
    }

    private String findSingleSiteOrNull() {
        List<String> rows = jdbc.queryForList("SELECT site_id FROM site ORDER BY site_id LIMIT 2", Map.of(), String.class);
        return rows.size() == 1 ? rows.getFirst() : null;
    }

    public LocalDate today() {
        return LocalDate.now(clock.withZone(SITE_ZONE));
    }
}
