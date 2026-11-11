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

    /**
     * AI 초안 요청의 existing(계약 tbm_form_draft_request, S15P31A101-616): 초안이 채우는 칸만, 빈 칸은 ""·null 그대로.
     * 작업내용·일시·점검 문구·종료 미팅은 넣지 않는다. form 은 normalize 를 거친 저장 모양이다.
     */
    static ObjectNode draftFields(JsonNode form) {
        JsonNode info = form.path("info");
        ObjectNode out = F.objectNode();
        out.put("task_name", info.path("task_name").asString(""));
        out.put("place", info.path("place").asString(""));
        out.set("risk_assessed", bool(info.path("risk_assessed")));
        ArrayNode risks = out.putArray("risks");
        for (JsonNode r : form.path("risks")) {
            risks.addObject().put("risk", r.path("risk").asString("")).put("measure", r.path("measure").asString(""));
        }
        out.putObject("focus").put("risk", form.path("focus").path("risk").asString(""))
                .put("measure", form.path("focus").path("measure").asString(""));
        ArrayNode checks = out.putArray("checks");
        for (JsonNode c : form.path("checks")) {
            ObjectNode row = checks.addObject();
            row.put("code", c.path("code").asString(""));
            row.set("yes", bool(c.path("yes")));
            row.put("action", c.path("action").asString(""));
        }
        out.putObject("review").put("daily", form.path("review").path("daily").asString(""));
        return out;
    }

    /**
     * AI 초안(form_draft)을 지금 양식의 빈 칸에만 채운다(글자 "", 예·아니오 null). 사람이 쓴 칸은 덮지 않는다.
     * 위험·대책 한 쌍은 같은 줄의 다른 칸이 비어 있거나 요청 때(sent, 요청의 existing)와 같을 때만 채운다.
     * 요청 뒤 사람이 그 줄의 다른 칸을 새로 쓰면 AI 가 그 글을 보지 못했으므로 짝이 맞지 않는 대책을 넣지 않는다.
     * 점검 줄은 코드로 맞춘다. 종료 미팅(review.ending)·작업내용·일시·점검 문구는 건드리지 않는다.
     *
     * @return 채운 칸이 하나라도 있으면 true
     */
    static boolean fillDraft(ObjectNode form, JsonNode draft, JsonNode sent) {
        boolean changed = false;
        ObjectNode info = (ObjectNode) form.path("info");
        changed |= fillText(info, "task_name", draft.path("task_name"));
        changed |= fillText(info, "place", draft.path("place"));
        if (info.path("risk_assessed").isNull() && draft.path("risk_assessed").isBoolean()) {
            info.put("risk_assessed", draft.path("risk_assessed").booleanValue());
            changed = true;
        }
        JsonNode risks = form.path("risks");
        for (int i = 0; i < risks.size(); i++) {
            changed |= fillPair((ObjectNode) risks.get(i), draft.path("risks").path(i), sent.path("risks").path(i));
        }
        changed |= fillPair((ObjectNode) form.path("focus"), draft.path("focus"), sent.path("focus"));
        for (JsonNode row : form.path("checks")) {
            ObjectNode check = (ObjectNode) row;
            for (JsonNode d : draft.path("checks")) {
                if (!d.path("code").asString("").equals(check.path("code").asString())) {
                    continue;
                }
                // 초안의 yes 는 계약상 늘 null 이라 여기서 채워지지 않는다(사람이 현장에서 확인할 칸).
                if (check.path("yes").isNull() && d.path("yes").isBoolean()) {
                    check.put("yes", d.path("yes").booleanValue());
                    changed = true;
                }
                changed |= fillText(check, "action", d.path("action"));
            }
        }
        changed |= fillText((ObjectNode) form.path("review"), "daily", draft.path("review").path("daily"));
        return changed;
    }

    private static boolean fillPair(ObjectNode current, JsonNode draft, JsonNode sent) {
        boolean changed = false;
        String risk = current.path("risk").asString("");
        String measure = current.path("measure").asString("");
        if (measure.isEmpty() || measure.equals(sent.path("measure").asString(""))) {
            changed |= fillText(current, "risk", draft.path("risk"));
        }
        if (risk.isEmpty() || risk.equals(sent.path("risk").asString(""))) {
            changed |= fillText(current, "measure", draft.path("measure"));
        }
        return changed;
    }

    private static boolean fillText(ObjectNode target, String field, JsonNode value) {
        if (!target.path(field).asString("").isEmpty() || !value.isString() || value.asString().isBlank()) {
            return false;
        }
        target.put(field, value.asString().strip());
        return true;
    }

    private static JsonNode bool(JsonNode node) {
        return node.isBoolean() ? node : F.nullNode();
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
