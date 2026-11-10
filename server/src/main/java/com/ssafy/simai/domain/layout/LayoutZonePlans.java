package com.ssafy.simai.domain.layout;

import com.ssafy.simai.domain.zone.ZoneRequest;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;
import java.util.ArrayList;
import java.util.List;
import tools.jackson.databind.JsonNode;

/** 관제 앱 layoutZonePlans 와 같은 World 좌표 변환. */
public final class LayoutZonePlans {
    private LayoutZonePlans() {}
    public record Plan(String itemId, ZoneRequest zone, StaticObjectRequest surface) {}
    private static double r3(double v) { return Math.floor(v * 1000 + 0.5) / 1000; }
    public static List<Plan> from(JsonNode items) {
        List<Plan> out = new ArrayList<>();
        int openings = 0, surfaces = 0;
        for (JsonNode item : items) {
            String kind = item.path("kind").asString();
            boolean surface = kind.equals("WORK_SURFACE") || kind.equals("SCAFFOLD");
            if (!surface && !kind.equals("OPENING")) continue;
            int ordinal = surface ? ++surfaces : ++openings;
            if (surface && !item.path("source_object_id").asString("").isEmpty()) continue;
            if (!surface && !item.path("source_zone_id").asString("").isEmpty()) continue;
            JsonNode c = item.path("center"), s = item.path("size");
            double x = c.get(0).asDouble(), y = c.get(1).asDouble(), z = c.get(2).asDouble();
            double sx = s.get(0).asDouble(), sy = s.get(1).asDouble(), sz = s.get(2).asDouble();
            double yaw = item.path("yaw_rad").asDouble();
            if (Math.abs(yaw) > Math.PI) yaw = Math.atan2(Math.sin(yaw), Math.cos(yaw));
            double top = r3(z + sz / 2);
            if (surface && top <= 0) continue;
            String label = item.path("label").asString("").strip();
            String name = label.isEmpty() ? (surface ? "고소 작업면 " : "개구부 ") + ordinal : label;
            double cos = Math.cos(yaw), sin = Math.sin(yaw), hx = sx / 2 + 0.3, hy = sy / 2 + 0.3;
            List<List<Double>> polygon = new ArrayList<>();
            for (double[] p : new double[][] {{-hx,-hy},{hx,-hy},{hx,hy},{-hx,hy}}) {
                polygon.add(List.of(r3(x + p[0] * cos - p[1] * sin), r3(y + p[0] * sin + p[1] * cos)));
            }
            double bottom = surface ? 0 : Math.max(0, r3(z - sz / 2));
            double zoneTop = surface ? Math.max(2, top) : top - bottom >= 0.5 ? top : bottom + 2;
            ZoneRequest zone = new ZoneRequest(name, "OPENING", "VOLUME_3D", polygon, r3(bottom), r3(zoneTop), "HIGH", null, null);
            StaticObjectRequest body = surface ? new StaticObjectRequest("WORK_SURFACE", name,
                    new StaticObjectRequest.Obb(List.of(x,y,top / 2), List.of(sx,sy,top), yaw), top, null, null) : null;
            out.add(new Plan(item.path("item_id").asString(), zone, body));
        }
        return out;
    }
}
