"""화면: ScreenState → 문구(순수 함수, 헤드리스 시험)와 tkinter 창(노트북 실행).

화면은 코어 상태를 그리기만 하고 판단하지 않는다. 문구는 render_lines 한 곳에서 만들고 Tk 창도 같은 문구를 쓴다.
"""

from __future__ import annotations

import queue
import sys
from collections.abc import Callable
from typing import TextIO

from simai_site_alert.core import ActiveAlert, ScreenState

CHANNEL_LABEL = {"CONTROL_ROOM": "관제 화면", "FOREMAN_PWA": "반장 PWA", "SITE_BUTTON": "현장 확인 버튼"}
TEST_BANNER = "[시험] 시험 경보입니다. 실제 위험이 아닙니다. 소리가 들리고 화면이 보이는지 확인하세요."


def sound_fault_banner(reason: str) -> str:
    return f"소리 장치 오류: {reason} - 소리 없이 화면으로만 경보 중. 스피커·출력 장치를 확인하세요."


def control_line(connected: bool | None) -> str:
    if connected is None:
        return "Tier 0 제어 채널: 연결 전"
    return "Tier 0 제어 채널: 연결됨" if connected else "Tier 0 제어 채널: 끊김 - 새 현장 경보를 받을 수 없음"


def ack_text(alert: ActiveAlert) -> str:
    if not alert.acknowledged:
        return "확인 전 · 소리 반복 중"
    if alert.ack is None:
        return "확인됨"
    if alert.ack.local:
        return "확인됨(현장 확인 버튼, Tier 0 기록 대기)"
    who = CHANNEL_LABEL.get(alert.ack.channel or "", alert.ack.channel or "알 수 없음")
    return f"확인됨({who}{' ' + alert.ack.actor_id if alert.ack.actor_id else ''})"


def alert_line(alert: ActiveAlert) -> str:
    parts = [f"[{alert.severity}]"]
    where = " · ".join(x for x in (alert.rule_id, alert.zone_id) if x)
    if where:
        parts.append(where)
    parts.append(alert.summary)
    if alert.needs_check:
        parts.append("확인 필요")
    parts.append(ack_text(alert))
    return " | ".join(parts)


def render_lines(state: ScreenState) -> list[str]:
    lines: list[str] = []
    if state.sound_fault:
        lines.append("!!! " + sound_fault_banner(state.sound_fault) + " !!!")
    if state.test is not None:
        if state.test.active:
            lines.append(TEST_BANNER)
        else:
            lines.append("[시험] 시험 경보 끝 (소리 재생: " + ("시작됨" if state.test.sound_started else "실패") + ")")
    lines.append(control_line(state.control_connected))
    if state.pending_site_acks:
        lines.append(f"현장 확인 {state.pending_site_acks}건 Tier 0 기록 대기")
    if not state.alerts:
        lines.append("활성 현장 경보 없음")
    lines.extend(alert_line(a) for a in state.alerts)
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
    """

    POLL_MS = 30
    BLINK_MS = 500
    COLORS = {"CRITICAL": ("#b00020", "#ff5252"), "HIGH": ("#c75000", "#ffa040"), None: ("#202020", "#202020")}

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
        for alert in state.alerts:
            row = tk.Frame(self._alerts, bg="#111111")
            row.pack(fill="x", pady=6)
            tk.Label(
                row,
                text=alert_line(alert),
                font=("Malgun Gothic", 22 if alert.severity == "CRITICAL" else 18, "bold"),
                fg="white",
                bg="#111111",
                anchor="w",
                justify="left",
                wraplength=820,
            ).pack(side="left", fill="x", expand=True, padx=8, pady=8)
            tk.Button(
                row,
                text="현장 확인",
                font=("Malgun Gothic", 16, "bold"),
                state="disabled" if alert.acknowledged else "normal",
                command=lambda event_id=alert.event_id: self._on_site_ack(event_id),
            ).pack(side="right", padx=8, pady=8)
        self._paint_background()
