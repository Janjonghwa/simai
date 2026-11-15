"""화면: ScreenState → 문구(순수 함수, 헤드리스 시험)와 tkinter 창(노트북 실행).

화면은 코어 상태를 그리기만 하고 판단하지 않는다. 문구는 render_lines 한 곳에서 만들고 Tk 창도 같은 문구를 쓴다.
"""

from __future__ import annotations

import queue
import sys
from collections.abc import Callable
from typing import TextIO

from simai_site_alert.core import ActiveAlert, ScreenState
from simai_site_alert.link_status import CONTROL_HALTED, NORMAL, REMOTE_FAULT, Reason, SiteStatus

SEVERITY_LABEL = {"CRITICAL": "위험", "HIGH": "경고", "CAUTION": "주의", "INFO": "안내"}

CHANNEL_LABEL = {"CONTROL_ROOM": "관제 화면", "FOREMAN_PWA": "반장 PWA", "SITE_BUTTON": "현장 확인 버튼"}
TEST_BANNER = "[시험] 시험 경보입니다. 실제 위험이 아닙니다. 소리가 들리고 화면이 보이는지 확인하세요."
# 영상 중계가 녹화 파일·합성 영상을 보내는 동안 (S15P31A101-724, AGENTS 규칙 5)
TEST_INPUT_BANNER = "시험 입력 — 실제 경보 꺼짐 (녹화 파일·합성 영상 재생 중: 경보는 화면에만 표시하고 소리를 내지 않습니다)"


def sound_fault_banner(reason: str) -> str:
    return f"소리 장치 오류: {reason} - 소리 없이 화면으로만 경보 중. 스피커·출력 장치를 확인하세요."


REASON_LABEL = {
    Reason.CONTROL_NOT_CONNECTED: "Tier 0 제어 채널 끊김",
    Reason.HEALTH_MISSING: "Tier 0 응답 없음",
    Reason.PROCESSING_STALLED: "Tier 0 처리 정체",
    Reason.TIER0_UNSAFE: "Tier 0 판정 불가",
    Reason.VIDEO_DOWN: "영상 중계 끊김",
    Reason.VIDEO_UNKNOWN: "영상 중계 상태 모름",
    Reason.EC2_UNREACHABLE: "EC2 응답 없음",
}
SITE_TITLE = {
    CONTROL_HALTED: "관제 중단 - 위험 감시가 멈췄습니다",
    REMOTE_FAULT: "원격 관제 장애 - 현장 경보는 동작 중입니다. 관제 화면·반장 PWA 전달이 늦거나 안 됩니다",
    NORMAL: "관제 정상",
}


def site_banner(site: SiteStatus) -> str:
    """장애별 현장 표시 한 줄 (명세 3.10 표). 원인을 함께 쓴다."""
    text = SITE_TITLE.get(site.site_display, site.site_display)
    reasons = site.tier0_reasons if site.site_display == CONTROL_HALTED else site.ec2_reasons
    if site.site_display != NORMAL and reasons:
        text += " (" + ", ".join(REASON_LABEL.get(r, r) for r in reasons) + ")"
    if site.recovering:
        text += " · 복구 확인 중"
    return text


def site_detail(site: SiteStatus) -> str:
    """연결 상세 한 줄: Tier 0 · 영상 · EC2."""
    tier0 = "Tier 0: 연결됨" if site.control_link == "CONNECTED" else "Tier 0: 끊김"
    if site.camera_count is None:
        video = "영상: 상태 모름"
    else:
        video = f"영상: {len(site.video_cameras)}/{site.camera_count}대"
    ec2 = "EC2: 연결됨" if site.ec2_link == "CONNECTED" else "EC2: 끊김"
    return " · ".join((tier0, video, ec2))


def control_line(connected: bool | None) -> str:
    if connected is None:
        return "Tier 0 제어 채널: 연결 전"
    return "Tier 0 제어 채널: 연결됨" if connected else "Tier 0 제어 채널: 끊김 - 새 현장 경보를 받을 수 없음"


def ack_text(alert: ActiveAlert, *, muted: bool = False) -> str:
    if not alert.acknowledged:
        if muted:  # 시험 입력(녹화 파일·합성 영상) 중에 표시된 경보는 해소까지 울리지 않는다(S15P31A101-724)
            return "확인 전 · 시험 입력이라 소리 없음"
        # 소리 결정은 Tier 0 의 sound_mode 다(S15P31A101-497). 같은 사람 상위 경보가 이미 울리면 SILENT 로 온다(-723).
        if alert.sound_mode == "SILENT":
            return "확인 전 · 소리 없음(같은 사람 상위 경보·최근 확인)"
        if alert.sound_mode == "ONCE":
            return "확인 전 · 한 번 울림"
        return "확인 전 · 소리 반복 중"
    if alert.ack is None:
        return "확인됨"
    if alert.ack.local:
        return "확인됨(현장 확인 버튼, Tier 0 기록 대기)"
    who = CHANNEL_LABEL.get(alert.ack.channel or "", alert.ack.channel or "알 수 없음")
    return f"확인됨({who}{' ' + alert.ack.actor_id if alert.ack.actor_id else ''})"


def alert_line(alert: ActiveAlert, *, muted: bool = False) -> str:
    parts = [f"[{SEVERITY_LABEL.get(alert.severity, alert.severity)}]"]
    where = " · ".join(x for x in (alert.rule_id, alert.zone_id) if x)
    if where:
        parts.append(where)
    parts.append(alert.summary)
    if alert.needs_check:
        parts.append("확인 필요")
    parts.append(ack_text(alert, muted=muted))
    return " | ".join(parts)


MEMBER_PREFIX = "  └ 같은 사람 "


def group_by_person(alerts: tuple[ActiveAlert, ...]) -> list[tuple[ActiveAlert, tuple[ActiveAlert, ...]]]:
    """같은 사람(world_id 가 겹침)의 경보를 표시 순서상 첫 경보(가장 높은 등급) 아래로 묶는다(명세 7.4, S15P31A101-723).

    묶음은 표시만 바꾼다. 경보마다 줄·현장 확인 버튼·확인 상태는 그대로 둔다(확인은 이벤트마다, 명세 3.10).
    world_id 를 모르는 경보(재동기화 목록으로만 안 경보)는 묶지 않는다.
    """
    groups: list[tuple[ActiveAlert, list[ActiveAlert], set[str]]] = []
    for alert in alerts:
        worlds = set(alert.world_ids)
        group = next((g for g in groups if worlds and worlds & g[2]), None)
        if group is None:
            groups.append((alert, [], worlds))
        else:
            group[1].append(alert)
            group[2].update(worlds)
    return [(leader, tuple(members)) for leader, members, _ in groups]


def render_lines(state: ScreenState) -> list[str]:
    lines: list[str] = []
    if state.sound_fault:
        lines.append("!!! " + sound_fault_banner(state.sound_fault) + " !!!")
    if state.test is not None:
        if state.test.active:
            lines.append(TEST_BANNER)
        else:
            lines.append("[시험] 시험 경보 끝 (소리 재생: " + ("시작됨" if state.test.sound_started else "실패") + ")")
    if state.test_input:
        lines.append("[" + TEST_INPUT_BANNER + "]")
    if state.site is not None:
        lines.append(site_banner(state.site))
        lines.append(site_detail(state.site))
    else:
        lines.append(control_line(state.control_connected))
    if state.pending_site_acks:
        lines.append(f"현장 확인 {state.pending_site_acks}건 Tier 0 기록 대기")
    if not state.alerts:
        lines.append("활성 현장 경보 없음")
    for leader, members in group_by_person(state.alerts):
        lines.append(alert_line(leader, muted=leader.event_id in state.test_muted))
        lines.extend(MEMBER_PREFIX + alert_line(m, muted=m.event_id in state.test_muted) for m in members)
    return lines


class TextDisplay:
    """헤드리스 실행용. 상태가 바뀔 때마다 문구를 출력한다."""

    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out or sys.stdout
        self._last: list[str] | None = None

    def update(self, state: ScreenState) -> None:
        lines = render_lines(state)
        if lines != self._last:
            self._last = lines
            self._out.write("\n".join(["----", *lines]) + "\n")
            self._out.flush()


class TkDisplay:
    """tkinter 창. update() 는 어느 스레드에서 불러도 되고(대기열), 그리기는 Tk 주 스레드의 after 루프가 한다.

    - CRITICAL·HIGH 경보가 표시 중이면 배경이 점멸한다(확인 뒤에도 해소까지 유지).
    - 경보마다 '현장 확인' 버튼(확인 전일 때만 누를 수 있음).
    - 소리 장치 오류는 맨 위에 큰 빨간 띠로 띄운다.
    - 장애별 현장 표시(관제 중단·원격 관제 장애·정상)는 상태 줄 위 띠로 띄운다(경보 표시와 따로).
    """

    POLL_MS = 30
    BLINK_MS = 500
    COLORS = {"CRITICAL": ("#b00020", "#ff5252"), "HIGH": ("#c75000", "#ffa040"), None: ("#202020", "#202020")}
    # 장애별 현장 표시 띠 (배경, 글자, 글자 크기)
    SITE_STYLE = {CONTROL_HALTED: ("#4a0000", "white", 24), REMOTE_FAULT: ("#ffb000", "black", 20),
                  NORMAL: ("#1e5e2e", "white", 14)}

    def __init__(
        self,
        *,
        on_site_ack: Callable[[str], None],
        on_test_alert: Callable[[], None],
        title: str = "simai 현장 경보",
        fullscreen: bool = False,
    ) -> None:
        import tkinter as tk

        self._tk = tk
        self._on_site_ack = on_site_ack
        self._queue: queue.Queue[ScreenState] = queue.Queue()
        self._state: ScreenState | None = None
        self._blink = False
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("1100x700")
        self.root.attributes("-topmost", True)
        if fullscreen:
            self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#202020")
        self._fault = tk.Label(self.root, font=("Malgun Gothic", 26, "bold"), fg="white", bg="#d00000", wraplength=1050)
        self._test = tk.Label(self.root, font=("Malgun Gothic", 20, "bold"), fg="black", bg="#ffe066", wraplength=1050)
        self._test_input = tk.Label(self.root, font=("Malgun Gothic", 20, "bold"), fg="white", bg="#3a2f8f",
                                    wraplength=1050)
        self._site = tk.Label(self.root, font=("Malgun Gothic", 14, "bold"), fg="white", bg="#202020", wraplength=1050)
        self._status = tk.Label(self.root, font=("Malgun Gothic", 14), fg="#dddddd", bg="#202020", anchor="w")
        self._alerts = tk.Frame(self.root, bg="#202020")
        bottom = tk.Frame(self.root, bg="#202020")
        tk.Button(bottom, text="시험 경보", font=("Malgun Gothic", 12), command=on_test_alert).pack(side="right", padx=8, pady=8)
        self._status.pack(fill="x", padx=12, pady=(8, 0))
        self._alerts.pack(fill="both", expand=True, padx=12, pady=8)
        bottom.pack(fill="x", side="bottom")
        self.root.after(self.POLL_MS, self._poll)
        self.root.after(self.BLINK_MS, self._toggle_blink)

    def update(self, state: ScreenState) -> None:
        self._queue.put(state)

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        self.root.destroy()

    def _poll(self) -> None:
        latest = None
        while True:
            try:
                latest = self._queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._state = latest
            self._render()
        self.root.after(self.POLL_MS, self._poll)

    def _toggle_blink(self) -> None:
        self._blink = not self._blink
        self._paint_background()
        self.root.after(self.BLINK_MS, self._toggle_blink)

    def _top_severity(self) -> str | None:
        if self._state is None or not self._state.alerts:
            return None
        return self._state.alerts[0].severity

    def _paint_background(self) -> None:
        dark, bright = self.COLORS[self._top_severity()]
        color = bright if self._blink else dark
        self.root.configure(bg=color)
        self._alerts.configure(bg=color)
        self._status.configure(bg=color)

    def _render(self) -> None:
        tk = self._tk
        state = self._state
        assert state is not None
        if state.sound_fault:
            self._fault.configure(text=sound_fault_banner(state.sound_fault))
            self._fault.pack(fill="x", before=self._status, ipady=12)
        else:
            self._fault.pack_forget()
        if state.test is not None and state.test.active:
            self._test.configure(text=TEST_BANNER)
            self._test.pack(fill="x", before=self._status, ipady=8)
        else:
            self._test.pack_forget()
        if state.test_input:
            self._test_input.configure(text=TEST_INPUT_BANNER)
            self._test_input.pack(fill="x", before=self._status, ipady=8)
        else:
            self._test_input.pack_forget()
        if state.site is not None:
            bg, fg, size = self.SITE_STYLE.get(state.site.site_display, self.SITE_STYLE[CONTROL_HALTED])
            self._site.configure(text=site_banner(state.site), bg=bg, fg=fg, font=("Malgun Gothic", size, "bold"))
            self._site.pack(fill="x", before=self._status, ipady=8 if state.site.site_display != NORMAL else 2)
            status = site_detail(state.site)
        else:
            self._site.pack_forget()
            status = control_line(state.control_connected)
        if state.pending_site_acks:
            status += f"   현장 확인 {state.pending_site_acks}건 Tier 0 기록 대기"
        self._status.configure(text=status)
        for child in self._alerts.winfo_children():
            child.destroy()
        if not state.alerts:
            tk.Label(self._alerts, text="활성 현장 경보 없음", font=("Malgun Gothic", 18), fg="#aaaaaa", bg="#202020").pack(
                anchor="w", pady=4
            )
        rows = [(a, a is not leader) for leader, members in group_by_person(state.alerts) for a in (leader, *members)]
        for alert, member in rows:
            row = tk.Frame(self._alerts, bg="#111111")
            # 같은 사람 하위 경보는 들여 쓰고 작게 그린다(묶음 표시, -723). 확인 버튼은 경보마다 둔다.
            row.pack(fill="x", pady=(0, 4) if member else 6, padx=(48, 0) if member else 0)
            tk.Label(
                row,
                text=(MEMBER_PREFIX.strip() + " " + alert_line(alert, muted=alert.event_id in state.test_muted)) if member else alert_line(alert, muted=alert.event_id in state.test_muted),
                font=("Malgun Gothic", 14 if member else 22 if alert.severity == "CRITICAL" else 18, "bold"),
                fg="#dddddd" if member else "white",
                bg="#111111",
                anchor="w",
                justify="left",
                wraplength=780 if member else 820,
            ).pack(side="left", fill="x", expand=True, padx=8, pady=4 if member else 8)
            tk.Button(
                row,
                text="현장 확인",
                font=("Malgun Gothic", 16, "bold"),
                state="disabled" if alert.acknowledged else "normal",
                command=lambda event_id=alert.event_id: self._on_site_ack(event_id),
            ).pack(side="right", padx=8, pady=8)
        self._paint_background()
