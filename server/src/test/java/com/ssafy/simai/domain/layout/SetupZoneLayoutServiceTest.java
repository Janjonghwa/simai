package com.ssafy.simai.domain.layout;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.geometry.StaticObjectRepository;
import com.ssafy.simai.domain.work.WorkUnitRepository;
import com.ssafy.simai.domain.zone.ZoneRepository;
import com.ssafy.simai.global.exception.BusinessException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.transaction.TransactionStatus;
import org.springframework.transaction.support.TransactionCallback;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

class SetupZoneLayoutServiceTest {
    private final SiteLookup sites = mock(SiteLookup.class);
    private final WorkUnitRepository works = mock(WorkUnitRepository.class);
    private final ZoneRepository zones = mock(ZoneRepository.class);
    private final StaticObjectRepository objects = mock(StaticObjectRepository.class);
    private final LayoutRepository layouts = mock(LayoutRepository.class);
    private final LayoutZoneService layoutZones = mock(LayoutZoneService.class);
    private final NamedParameterJdbcTemplate jdbc = mock(NamedParameterJdbcTemplate.class);
    private final TransactionTemplate transaction = mock(TransactionTemplate.class);
    private final JsonMapper mapper = JsonMapper.builder().build();
    private SetupZoneLayoutService service;

    @BeforeEach
    @SuppressWarnings("unchecked")
    void setUp() {
        when(transaction.execute(any())).thenAnswer(invocation ->
                ((TransactionCallback<Object>) invocation.getArgument(0)).doInTransaction(mock(TransactionStatus.class)));
        when(sites.siteId()).thenReturn("site-1");
        when(works.lockSession("site-1", 7)).thenReturn((ObjectNode) mapper.readTree("""
                {"status":"PLANNED","name":"작업","layout_id":null}
                """));
        when(jdbc.queryForList(anyString(), any(MapSqlParameterSource.class), eq(Integer.class))).thenReturn(List.of(1));
        when(zones.findCurrent("site-1")).thenReturn(List.of((ObjectNode) mapper.readTree("""
                {"zone_id":"z-1","zone_version":1,"zone_kind":"OPENING","name":"구역","z_min_m":0,"z_max_m":2}
                """)));
        when(objects.findCurrent("site-1")).thenReturn(List.of((ObjectNode) mapper.readTree("""
                {"object_id":"ws-1","object_version":1,"kind":"WORK_SURFACE","zone_id":"z-1",
                 "obb":{"center":[1,2,0.5],"size":[2,3,1],"yaw_rad":0}}
                """)));
        service = new SetupZoneLayoutService(sites, works, zones, objects, layouts, layoutZones, jdbc,
                transaction, mapper, Clock.fixed(Instant.parse("2026-11-09T00:00:00Z"), ZoneOffset.UTC));
    }

    @Test
    void 새_레이아웃을_원본_작업면_참조와_함께_만든다() {
        when(layouts.create(eq("site-1"), any(), eq(1L), any())).thenReturn(42L);
        when(layouts.findOne("site-1", 42)).thenReturn(Optional.of((ObjectNode) mapper.readTree("{\"revision\":1}")));
        ObjectNode result = service.add(7, "z-1", 1);
        assertThat(result.path("result").asString()).isEqualTo("LINKED");
        assertThat(result.path("layout_id").asLong()).isEqualTo(42);
        ArgumentCaptor<LayoutRequest> request = ArgumentCaptor.forClass(LayoutRequest.class);
        verify(layouts).create(eq("site-1"), request.capture(), eq(1L), any());
        assertThat(request.getValue().items()).singleElement().satisfies(item -> {
            assertThat(item.sourceZoneId()).isEqualTo("z-1");
            assertThat(item.sourceObjectId()).isEqualTo("ws-1");
        });
    }

    @Test
    void 다른_세팅의_구역은_추가하지_않는다() {
        when(jdbc.queryForList(anyString(), any(MapSqlParameterSource.class), eq(Integer.class))).thenReturn(List.of());
        assertThatThrownBy(() -> service.add(7, "z-1", 1)).isInstanceOf(BusinessException.class);
        verify(layouts, never()).create(anyString(), any(), anyLong(), any());
    }

    @Test
    void 구역이_발행되지_않았으면_추가하지_않는다() {
        when(jdbc.queryForList(anyString(), any(MapSqlParameterSource.class), eq(Integer.class)))
                .thenReturn(List.of(1), List.of());
        assertThatThrownBy(() -> service.add(7, "z-1", 1)).isInstanceOf(BusinessException.class);
        verify(layouts, never()).create(anyString(), any(), anyLong(), any());
    }

    @Test
    void 이미_연결된_구역은_레이아웃을_다시_만들지_않는다() {
        when(works.lockSession("site-1", 7)).thenReturn((ObjectNode) mapper.readTree("""
                {"status":"PLANNED","name":"작업","layout_id":42}
                """));
        when(layouts.findOne("site-1", 42)).thenReturn(Optional.of((ObjectNode) mapper.readTree("""
                {"layout_id":42,"name":"작업","revision":1,"items":[
                 {"item_id":"auto-z-1","kind":"WORK_SURFACE","center":[1,2,0.5],"size":[2,3,1],
                  "yaw_rad":0,"label":"구역","source_zone_id":"z-1","source_object_id":"ws-1"}]}
                """)));
        ObjectNode result = service.add(7, "z-1", 1);
        assertThat(result.path("result").asString()).isEqualTo("ALREADY_LINKED");
        verify(layouts, never()).create(anyString(), any(), anyLong(), any());
        verify(layouts, never()).replace(anyString(), anyLong(), any(), anyLong(), any());
        verify(layouts, never()).replaceSetupReferences(anyString(), anyLong(), any(), anyLong(), any());
    }

    @Test
    void 기존_항목과_이름을_보존하고_새_참조를_추가한다() {
        when(works.lockSession("site-1", 7)).thenReturn((ObjectNode) mapper.readTree("""
                {"status":"PLANNED","name":"작업","layout_id":42}
                """));
        when(layouts.findOne("site-1", 42)).thenReturn(Optional.of((ObjectNode) mapper.readTree("""
                {"name":"기존 이름","revision":3,"items":[{"item_id":"existing","kind":"EQUIPMENT",
                 "center":[1,2,1],"size":[1,1,1],"yaw_rad":0,"label":"기존"}]}
                """)));
        service.add(7, "z-1", 1);
        ArgumentCaptor<LayoutRequest> request = ArgumentCaptor.forClass(LayoutRequest.class);
        verify(layouts).replaceSetupReferences(eq("site-1"), eq(42L), request.capture(), eq(1L), any());
        assertThat(request.getValue().name()).isEqualTo("기존 이름");
        assertThat(request.getValue().revision()).isEqualTo(3);
        assertThat(request.getValue().items()).extracting(LayoutRequest.Item::itemId).containsExactly("existing", "auto-z-1");
    }

    @Test
    void 원본_작업면이_바뀌면_다른_항목은_보존하며_참조_형상을_갱신한다() {
        when(works.lockSession("site-1", 7)).thenReturn((ObjectNode) mapper.readTree("""
                {"status":"PLANNED","name":"작업","layout_id":42}
                """));
        when(layouts.findOne("site-1", 42)).thenReturn(Optional.of((ObjectNode) mapper.readTree("""
                {"name":"기존 이름","revision":3,"items":[
                 {"item_id":"other","kind":"EQUIPMENT","center":[0,0,1],"size":[1,1,1],"yaw_rad":0},
                 {"item_id":"auto-z-1","kind":"WORK_SURFACE","center":[4,2,0.5],"size":[2,3,1],
                  "yaw_rad":0,"label":"구역","source_zone_id":"z-1","source_object_id":"ws-1"}]}
                """)));
        service.add(7, "z-1", 1);
        ArgumentCaptor<LayoutRequest> request = ArgumentCaptor.forClass(LayoutRequest.class);
        verify(layouts).replaceSetupReferences(eq("site-1"), eq(42L), request.capture(), eq(1L), any());
        assertThat(request.getValue().items()).extracting(LayoutRequest.Item::itemId).containsExactly("other", "auto-z-1");
        assertThat(request.getValue().items().get(1).center()).containsExactly(1.0, 2.0, 0.5);
    }

    @Test
    void 작업면_방향을_모르면_임의의_값으로_만들지_않는다() {
        when(objects.findCurrent("site-1")).thenReturn(List.of((ObjectNode) mapper.readTree("""
                {"object_id":"ws-1","object_version":1,"kind":"WORK_SURFACE","zone_id":"z-1",
                 "obb":{"center":[1,2,0.5],"size":[2,3,1]}}
                """)));
        assertThatThrownBy(() -> service.add(7, "z-1", 1)).isInstanceOf(BusinessException.class);
        verify(layouts, never()).create(anyString(), any(), anyLong(), any());
    }

    @Test
    void 사각형으로_변환할_수_없는_개구부는_만들지_않는다() {
        when(objects.findCurrent("site-1")).thenReturn(List.of());
        when(jdbc.queryForObject(anyString(), any(MapSqlParameterSource.class), eq(String.class))).thenReturn(null);
        assertThatThrownBy(() -> service.add(7, "z-1", 1)).isInstanceOf(BusinessException.class);
        verify(layouts, never()).create(anyString(), any(), anyLong(), any());
    }

    @Test
    void 개구부의_높이를_모르면_임의로_채우지_않는다() {
        when(objects.findCurrent("site-1")).thenReturn(List.of());
        when(zones.findCurrent("site-1")).thenReturn(List.of((ObjectNode) mapper.readTree("""
                {"zone_id":"z-1","zone_version":1,"zone_kind":"OPENING","name":"구역","z_min_m":null,"z_max_m":2}
                """)));
        assertThatThrownBy(() -> service.add(7, "z-1", 1)).isInstanceOf(BusinessException.class);
        verify(layouts, never()).create(anyString(), any(), anyLong(), any());
    }

    @Test
    void 진행_중인_작업은_변경하지_않는다() {
        when(works.lockSession("site-1", 7)).thenReturn((ObjectNode) mapper.readTree("""
                {"status":"IN_PROGRESS","name":"작업","layout_id":42}
                """));
        assertThatThrownBy(() -> service.add(7, "z-1", 1)).isInstanceOf(BusinessException.class);
        verify(layouts, never()).create(anyString(), any(), anyLong(), any());
        verify(layouts, never()).replaceSetupReferences(anyString(), anyLong(), any(), anyLong(), any());
    }
}
