package com.ssafy.simai.domain.report;

import com.ssafy.simai.domain.work.WorkTasks;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/**
 * 날짜 작업 TBM 보고서의 양식 칸(report.form, S15P31A101-609, docs/design/tbm-daily-form.md).
 *
 * <p>모양: info(일시·작업일 동일·작업명·작업내용·장소·위험성평가 실시) · risks 3줄 · focus · checks 5줄 · review(일일·종료).
 * 리더·참석자는 양식에 넣지 않는다(리더는 leader_user_id, 참석자는 report_signature).
 * 저장할 때는 아는 칸만 골라 다시 만든다. 점검 줄의 문구는 서버가 정하므로 받은 label 은 버린다.
 */
final class TbmForm {

    static final int RISK_ROWS = 3;
    static final int MAX_TASKS = 20;
    static final int MAX_SHORT = 100;
    static final int MAX_PLACE = 200;
    static final int MAX_TEXT = 500;
    static final int MAX_REVIEW = 2000;

    record Check(String code, String label) { }

    /** 안전조치 점검 5줄. 순서도 고정이다. */
    static final List<Check> CHECKS = List.of(
            new Check("PPE", "안전모 등 보호구 착용 상태 확인"),
            new Check("ZONE_CONTROL", "작업 구간 위험구역 표시 및 접근 통제"),
            new Check("OPENING_GUARD", "개구부 덮개·난간 설치 확인"),
            new Check("WORK_SURFACE_FALL", "고소 작업면 상태 및 추락 방호 확인"),
            new Check("HEALTH", "작업자 건강 상태 확인"));

    private static final JsonNodeFactory F = JsonNodeFactory.instance;

    private TbmForm() {
    }

    /** 날짜 작업을 만들 때 쓰는 빈 양식. 일시와 작업일 동일(예)만 채운다. */
    static ObjectNode blank(long dateTimeMs) {
        ObjectNode form = F.objectNode();
        ObjectNode info = form.putObject("info");
        info.put("date_time_ms", dateTimeMs);
        info.put("same_day", true);
        info.put("task_name", "");
        info.putArray("tasks");
        info.put("place", "");
        info.putNull("risk_assessed");
        ArrayNode risks = form.putArray("risks");
        for (int i = 0; i < RISK_ROWS; i++) {
            risks.addObject().put("risk", "").put("measure", "");
        }
        form.putObject("focus").put("risk", "").put("measure", "");
        ArrayNode checks = form.putArray("checks");
        for (Check c : CHECKS) {
            ObjectNode row = checks.addObject();
            row.put("code", c.code());
            row.put("label", c.label());
            row.putNull("yes");
            row.put("action", "");
        }
        form.putObject("review").put("daily", "").put("ending", "");
        return form;
    }

    /**
     * 받은 양식을 검사하고 저장할 모양으로 다시 만든다. 빠진 글자 칸은 "", 빠진 예·아니오 칸은 null 로 본다.
     * 모양이 틀리면 INVALID_REPORT_FORM(400).
     */
    static ObjectNode normalize(JsonNode in) {
        object(in);
        ObjectNode form = F.objectNode();
        JsonNode info = object(in.path("info"));
        ObjectNode outInfo = form.putObject("info");
        JsonNode dt = info.path("date_time_ms");
        if (dt.isMissingNode() || dt.isNull()) {
            outInfo.putNull("date_time_ms");
        } else if (dt.isIntegralNumber() && dt.longValue() >= 0) {
            outInfo.put("date_time_ms", dt.longValue());
        } else {
            throw invalid();
        }
        putBool(outInfo, "same_day", info.path("same_day"));
        outInfo.put("task_name", text(info.path("task_name"), MAX_SHORT));
        outInfo.set("tasks", tasks(info.path("tasks")));
        outInfo.put("place", text(info.path("place"), MAX_PLACE));
        putBool(outInfo, "risk_assessed", info.path("risk_assessed"));

        JsonNode risks = in.path("risks");
        if (!risks.isArray() || risks.size() != RISK_ROWS) {
            throw invalid();
        }
        ArrayNode outRisks = form.putArray("risks");
        for (JsonNode r : risks) {
            pair(r, outRisks.addObject());
        }
        pair(in.path("focus"), form.putObject("focus"));

        JsonNode checks = in.path("checks");
        if (!checks.isArray() || checks.size() != CHECKS.size()) {
            throw invalid();
        }
        ArrayNode outChecks = form.putArray("checks");
        for (int i = 0; i < CHECKS.size(); i++) {
            JsonNode row = object(checks.get(i));
            Check c = CHECKS.get(i);
            JsonNode code = row.path("code");
            if (!code.isString() || !c.code().equals(code.asString())) {
                throw invalid();
            }
            ObjectNode out = outChecks.addObject();
            out.put("code", c.code());
            out.put("label", c.label());
            putBool(out, "yes", row.path("yes"));
            out.put("action", text(row.path("action"), MAX_TEXT));
        }

        JsonNode review = object(in.path("review"));
        form.putObject("review")
                .put("daily", text(review.path("daily"), MAX_REVIEW))
                .put("ending", text(review.path("ending"), MAX_REVIEW));
        return form;
    }

    /** 작업내용: {code}(작업내용 항목) 또는 {text}(직접 쓴 작업). 같은 항목을 두 번 넣지 않는다. */
    private static ArrayNode tasks(JsonNode tasks) {
        ArrayNode out = F.arrayNode();
        if (tasks.isMissingNode() || tasks.isNull()) {
            return out;
        }
        if (!tasks.isArray() || tasks.size() > MAX_TASKS) {
            throw invalid();
        }
        Set<String> seen = new HashSet<>();
        for (JsonNode t : tasks) {
            if (!t.isObject() || t.size() != 1) {
                throw invalid();
            }
            JsonNode code = t.path("code");
            JsonNode text = t.path("text");
            if (code.isString() && WorkTasks.find(code.asString()).isPresent()) {
                if (!seen.add("code:" + code.asString())) {
                    throw invalid();
                }
                out.addObject().put("code", code.asString());
            } else if (text.isString()) {
                String value = text(text, MAX_SHORT);
                if (value.isEmpty() || !seen.add("text:" + value)) {
                    throw invalid();
                }
                out.addObject().put("text", value);
            } else {
                throw invalid();
            }
        }
        return out;
    }

    private static void pair(JsonNode in, ObjectNode out) {
        JsonNode row = object(in);
        out.put("risk", text(row.path("risk"), MAX_TEXT));
        out.put("measure", text(row.path("measure"), MAX_TEXT));
    }

    private static JsonNode object(JsonNode node) {
        if (node == null || !node.isObject()) {
            throw invalid();
        }
        return node;
    }

    private static String text(JsonNode node, int max) {
        if (node.isMissingNode() || node.isNull()) {
            return "";
        }
        if (!node.isString()) {
            throw invalid();
        }
        // 앞뒤 공백은 지운다(공백만 있으면 빈 칸). NUL 은 jsonb 가 받지 않으므로 400 으로 막는다.
        String value = node.asString().strip();
        if (value.indexOf('\u0000') >= 0 || value.codePointCount(0, value.length()) > max) {
            throw invalid();
        }
        return value;
    }

    private static void putBool(ObjectNode out, String field, JsonNode node) {
        if (node.isMissingNode() || node.isNull()) {
            out.putNull(field);
        } else if (node.isBoolean()) {
            out.put(field, node.booleanValue());
        } else {
            throw invalid();
        }
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Report.INVALID_REPORT_FORM);
    }
}
