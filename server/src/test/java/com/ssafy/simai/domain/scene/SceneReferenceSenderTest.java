package com.ssafy.simai.domain.scene;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.assertj.core.api.Assertions.assertThat;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

class SceneReferenceSenderTest {
    @Test void 활성화와_되돌림과_재연결에도_현재_참조를_보낸다() throws Exception {
        var registry = mock(I1ConnectionRegistry.class);
        var jdbc = mock(NamedParameterJdbcTemplate.class);
        var sites = mock(SiteLookup.class);
        var a = mock(I1Connection.class);
        var b = mock(I1Connection.class);
        when(a.isOpen()).thenReturn(true);
        when(a.gatewayId()).thenReturn("tier0");
        when(b.isOpen()).thenReturn(true);
        when(b.gatewayId()).thenReturn("tier0");
        when(sites.siteId()).thenReturn("classroom-1");
        when(registry.all()).thenReturn(List.of(a));
        when(jdbc.queryForList(anyString(), anyMap(), eq(String.class)))
                .thenReturn(List.of("scene-a"), List.of("scene-b"), List.of("scene-a"), List.of("scene-a"));
        var sender = new SceneReferenceSender(registry, jdbc, sites, JsonMapper.builder().build(), "default", "");
        sender.send(); sender.send(); sender.send();
        var messages = ArgumentCaptor.forClass(ObjectNode.class);
        verify(a, times(3)).send(eq("scene_reference"), eq("1.0.0"), messages.capture());
        assertThat(messages.getAllValues().stream().map(p -> p.path("scene_version").asString()).toList())
                .containsExactly("scene-a", "scene-b", "scene-a");
        when(registry.all()).thenReturn(List.of(b));
        sender.send();
        verify(b).send(eq("scene_reference"), eq("1.0.0"), argThat(p -> p.path("site_id").asString().equals("classroom-1")
                && p.path("scene_version").asString().equals("scene-a")));
    }
}
