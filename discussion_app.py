#!/usr/bin/env python3
"""Gemini vs Claude 토론 자동화 앱 - CDP 기반 두 AI 탭 간 자동 토론 진행"""

import sys
import os
import json
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum, auto

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QPushButton, QLabel, QLineEdit, QTextEdit,
    QGroupBox, QFileDialog, QMessageBox, QSpinBox,
    QFormLayout, QRadioButton, QButtonGroup, QScrollArea,
    QSplitter, QFrame, QSizePolicy,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QTextCursor, QColor

from chrome_cdp_controller import ChromeProfileManager, CDPClient


# ---------------------------------------------------------------------------
# Selector Configs
# ---------------------------------------------------------------------------
@dataclass
class SelectorConfig:
    """AI 서비스의 CSS 셀렉터 묶음"""
    input_selector: str = ""
    send_selector: str = ""          # 비어있으면 Enter 키 사용
    response_selector: str = ""
    stop_button_selector: str = ""


def default_gemini_selectors() -> SelectorConfig:
    return SelectorConfig(
        input_selector='div.ql-editor[contenteditable="true"]',
        send_selector='button[aria-label="Send message"]',
        response_selector='.model-response-text',
        stop_button_selector='button[aria-label="Stop"]',
    )


def default_claude_selectors() -> SelectorConfig:
    return SelectorConfig(
        input_selector='div[contenteditable="true"].ProseMirror',
        send_selector='button[aria-label="Send Message"]',
        response_selector='[data-is-streaming] .markdown-content, .font-claude-message .markdown-content',
        stop_button_selector='button[aria-label="Stop Response"]',
    )


# ---------------------------------------------------------------------------
# Discussion State
# ---------------------------------------------------------------------------
class DiscussionState(Enum):
    IDLE = auto()
    SENDING = auto()
    WAITING_RESPONSE = auto()
    READING = auto()
    PAUSED = auto()
    COMPLETED = auto()
    ERROR = auto()


# ---------------------------------------------------------------------------
# AI Tab Controller
# ---------------------------------------------------------------------------
class AITabController:
    """CDPClient 하나 + SelectorConfig를 감싸는 고수준 컨트롤러"""

    def __init__(self, name: str, port: int, selectors: SelectorConfig):
        self.name = name
        self.cdp = CDPClient(port)
        self.selectors = selectors
        self._ws_url: str = ""
        self._last_response: str = ""

    @property
    def connected(self) -> bool:
        return self.cdp.connected

    def connect_to_tab(self, ws_url: str):
        """특정 탭에 WebSocket 연결"""
        self._ws_url = ws_url
        self.cdp.connect_tab(ws_url)

    def reconnect(self, max_retries: int = 3) -> bool:
        """WebSocket 재연결 시도"""
        for i in range(max_retries):
            try:
                if self._ws_url:
                    self.cdp.connect_tab(self._ws_url)
                    if self.cdp.connected:
                        return True
            except Exception:
                time.sleep(1)
        return False

    def check_login_status(self) -> tuple[bool, str]:
        """로그인 여부를 확인한다. (로그인됨 여부, 상태 메시지)"""
        if not self.connected:
            return False, "WebSocket 미연결"

        # 현재 URL 가져오기
        resp = self.cdp.execute_js("window.location.href")
        url = resp.get("result", {}).get("result", {}).get("value", "")

        if self.name.lower() == "gemini":
            if "accounts.google.com" in url:
                return False, "Google 로그인 페이지로 리다이렉트됨"
            if "gemini.google.com" not in url:
                return False, f"예상치 못한 URL: {url}"
            # 입력 필드 존재 확인
            resp = self.cdp.execute_js(
                f"!!document.querySelector({json.dumps(self.selectors.input_selector)})"
            )
            has_input = resp.get("result", {}).get("result", {}).get("value", False)
            if has_input:
                return True, "로그인 확인됨"
            return False, "입력 필드를 찾을 수 없음 (로그인 필요할 수 있음)"

        elif self.name.lower() == "claude":
            if "/login" in url:
                return False, "Claude 로그인 페이지로 리다이렉트됨"
            if "claude.ai" not in url:
                return False, f"예상치 못한 URL: {url}"
            resp = self.cdp.execute_js(
                f"!!document.querySelector({json.dumps(self.selectors.input_selector)})"
            )
            has_input = resp.get("result", {}).get("result", {}).get("value", False)
            if has_input:
                return True, "로그인 확인됨"
            return False, "입력 필드를 찾을 수 없음 (로그인 필요할 수 있음)"

        return False, "알 수 없는 서비스"

    def send_message(self, text: str) -> tuple[bool, str]:
        """입력 필드에 텍스트를 주입하고 전송한다."""
        if not self.connected:
            if not self.reconnect():
                return False, "WebSocket 연결 끊김, 재연결 실패"

        # contenteditable에 텍스트 입력
        resp = self.cdp.type_contenteditable(self.selectors.input_selector, text)
        result_val = resp.get("result", {}).get("result", {}).get("value", "")
        if "not found" in str(result_val).lower():
            return False, f"입력 필드를 찾을 수 없음: {result_val}"

        time.sleep(0.5)

        # 전송: send 버튼 클릭 또는 Enter 키
        if self.selectors.send_selector:
            resp = self.cdp.click(self.selectors.send_selector)
            # 버튼이 없으면 Enter 키 폴백
            click_val = resp.get("result", {}).get("result", {}).get("value")
            if click_val is None:
                resp = self.cdp.press_enter(self.selectors.input_selector)
        else:
            resp = self.cdp.press_enter(self.selectors.input_selector)

        return True, "전송 완료"

    def is_streaming(self) -> bool:
        """Stop 버튼 존재 여부로 스트리밍 상태 확인"""
        if not self.selectors.stop_button_selector:
            return False
        resp = self.cdp.execute_js(
            f"!!document.querySelector({json.dumps(self.selectors.stop_button_selector)})"
        )
        return resp.get("result", {}).get("result", {}).get("value", False)

    def read_last_response(self) -> str:
        """마지막 응답 텍스트를 추출한다."""
        sel = self.selectors.response_selector
        js = f"""
        (() => {{
            const els = document.querySelectorAll({json.dumps(sel)});
            if (els.length === 0) return '';
            const last = els[els.length - 1];
            return last.innerText || last.textContent || '';
        }})()
        """
        resp = self.cdp.execute_js(js)
        text = resp.get("result", {}).get("result", {}).get("value", "")
        return text.strip()

    def wait_for_response(self, timeout: int = 120, poll_interval: float = 2.0,
                          stable_duration: float = 3.0) -> tuple[bool, str]:
        """응답 완료를 대기한다.

        전략: poll_interval 간격으로 응답을 확인하고,
        stable_duration 동안 텍스트 변경 없음 + Stop 버튼 사라짐 → 완료 판정
        """
        start = time.time()
        last_text = ""
        stable_since: float | None = None

        # 먼저 스트리밍이 시작될 때까지 대기 (최대 15초)
        stream_wait_start = time.time()
        while time.time() - stream_wait_start < 15:
            current = self.read_last_response()
            if current and current != self._last_response:
                break
            if self.is_streaming():
                break
            time.sleep(1)

        while time.time() - start < timeout:
            current = self.read_last_response()
            streaming = self.is_streaming()

            if current != last_text:
                last_text = current
                stable_since = None
            else:
                if stable_since is None:
                    stable_since = time.time()

            # 안정성 검사: 텍스트 변경 없음 + Stop 버튼 없음
            if (stable_since is not None
                    and time.time() - stable_since >= stable_duration
                    and not streaming
                    and current
                    and current != self._last_response):
                self._last_response = current
                return True, current

            time.sleep(poll_interval)

        # 타임아웃
        current = self.read_last_response()
        if current and current != self._last_response:
            self._last_response = current
            return True, current
        return False, "응답 타임아웃"


# ---------------------------------------------------------------------------
# Discussion Worker Thread
# ---------------------------------------------------------------------------
class DiscussionWorkerThread(QThread):
    """토론 루프를 백그라운드에서 실행"""
    turn_completed = pyqtSignal(int, str, str)       # turn_num, speaker_name, text
    state_changed = pyqtSignal(str)                   # state description
    error_occurred = pyqtSignal(str)                  # error message
    discussion_finished = pyqtSignal()
    waiting_for_next = pyqtSignal()                   # 반자동: 다음 턴 대기 중

    def __init__(self, first_ai: AITabController, second_ai: AITabController,
                 topic: str, prompt_template: str, max_turns: int,
                 timeout: int, auto_mode: bool):
        super().__init__()
        self.first_ai = first_ai
        self.second_ai = second_ai
        self.topic = topic
        self.prompt_template = prompt_template
        self.max_turns = max_turns
        self.timeout = timeout
        self.auto_mode = auto_mode

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()   # 초기 상태: 일시정지 아님
        self._next_turn_event = threading.Event()

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()
        self._next_turn_event.set()

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def next_turn(self):
        self._next_turn_event.set()

    def _check_stop(self) -> bool:
        return self._stop_event.is_set()

    def _wait_if_paused(self):
        while not self._pause_event.is_set():
            if self._check_stop():
                return
            time.sleep(0.5)

    def _check_login(self, ai: AITabController) -> bool:
        """턴 시작 전 로그인 상태 확인"""
        ok, msg = ai.check_login_status()
        if not ok:
            self.state_changed.emit(f"PAUSED - {ai.name} 로그인 필요: {msg}")
            self.error_occurred.emit(
                f"{ai.name} 로그인이 필요합니다: {msg}\n수동 로그인 후 Resume을 눌러주세요."
            )
            self.pause()
            self._wait_if_paused()
            if self._check_stop():
                return False
            # 재확인
            ok2, _ = ai.check_login_status()
            return ok2
        return True

    def run(self):
        try:
            self._run_discussion()
        except Exception as e:
            self.error_occurred.emit(f"예기치 못한 오류: {e}")
            self.state_changed.emit("ERROR")

    def _run_discussion(self):
        current_speaker = self.first_ai
        opponent = self.second_ai
        last_response = ""

        for turn in range(1, self.max_turns + 1):
            if self._check_stop():
                break

            self._wait_if_paused()
            if self._check_stop():
                break

            # 반자동 모드: 턴 대기
            if not self.auto_mode and turn > 1:
                self.waiting_for_next.emit()
                self.state_changed.emit(f"WAITING_USER - 턴 {turn} 시작 대기 (다음 턴 버튼을 눌러주세요)")
                self._next_turn_event.clear()
                self._next_turn_event.wait()
                if self._check_stop():
                    break

            # 로그인 확인
            if not self._check_login(current_speaker):
                break

            # 프롬프트 구성
            if turn == 1 and not last_response:
                # 첫 턴: 초기 프롬프트
                prompt = self.prompt_template.format(
                    topic=self.topic,
                    opponent_name=opponent.name,
                    opponent_response="(첫 발언입니다. 주제에 대한 당신의 입장을 먼저 제시해 주세요.)",
                    turn_number=turn,
                    max_turns=self.max_turns,
                )
            else:
                prompt = self.prompt_template.format(
                    topic=self.topic,
                    opponent_name=opponent.name if turn > 1 else current_speaker.name,
                    opponent_response=last_response,
                    turn_number=turn,
                    max_turns=self.max_turns,
                )

            # 메시지 전송
            self.state_changed.emit(f"SENDING - 턴 {turn}/{self.max_turns} ({current_speaker.name})")
            ok, msg = current_speaker.send_message(prompt)
            if not ok:
                self.error_occurred.emit(f"전송 실패 ({current_speaker.name}): {msg}")
                self.state_changed.emit("ERROR")
                return

            # 응답 대기
            self.state_changed.emit(f"WAITING_RESPONSE - 턴 {turn}/{self.max_turns} ({current_speaker.name})")
            ok, response = current_speaker.wait_for_response(timeout=self.timeout)
            if self._check_stop():
                break
            if not ok:
                self.error_occurred.emit(f"응답 타임아웃 ({current_speaker.name}): {response}")
                self.state_changed.emit("ERROR")
                return

            # 응답 읽기 완료
            self.state_changed.emit(f"READING - 턴 {turn}/{self.max_turns} ({current_speaker.name})")
            self.turn_completed.emit(turn, current_speaker.name, response)
            last_response = response

            # 역할 교체
            current_speaker, opponent = opponent, current_speaker

        if not self._check_stop():
            self.state_changed.emit("COMPLETED")
            self.discussion_finished.emit()
        else:
            self.state_changed.emit("STOPPED")


# ---------------------------------------------------------------------------
# Default prompt template
# ---------------------------------------------------------------------------
DEFAULT_PROMPT_TEMPLATE = """당신은 "{topic}" 주제에 대해 토론 중입니다.
상대방({opponent_name})이 다음과 같이 주장했습니다:

---
{opponent_response}
---

이에 대해 반론하거나 자신의 입장을 전개해 주세요. (턴 {turn_number}/{max_turns})"""


# ---------------------------------------------------------------------------
# Discussion App GUI
# ---------------------------------------------------------------------------
class DiscussionApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gemini vs Claude - 토론 자동화")
        self.setMinimumSize(950, 850)

        self.profile_mgr = ChromeProfileManager()
        self.profiles: list[dict] = []
        self._cdp_port: int = 0
        self._tabs: list[dict] = []

        self.gemini_ctrl: AITabController | None = None
        self.claude_ctrl: AITabController | None = None
        self._worker: DiscussionWorkerThread | None = None
        self._turn_log: list[dict] = []

        self._build_ui()
        self._refresh_profiles()

    # ----------------------------------------------------------------
    # UI 구성
    # ----------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)
        layout.setSpacing(6)

        # 1) Chrome 연결
        conn_box = QGroupBox("Chrome 연결")
        cl = QHBoxLayout(conn_box)
        cl.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(200)
        cl.addWidget(self.profile_combo)
        self.btn_launch = QPushButton("Launch Chrome")
        self.btn_launch.clicked.connect(self.on_launch)
        cl.addWidget(self.btn_launch)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.on_connect)
        cl.addWidget(self.btn_connect)
        self.conn_status = QLabel("⚫ Disconnected")
        cl.addWidget(self.conn_status)
        cl.addStretch()
        layout.addWidget(conn_box)

        # 2) 탭 할당
        tab_box = QGroupBox("탭 할당")
        tl = QVBoxLayout(tab_box)

        btn_row = QHBoxLayout()
        self.btn_refresh_tabs = QPushButton("Refresh Tabs")
        self.btn_refresh_tabs.clicked.connect(self.on_refresh_tabs)
        btn_row.addWidget(self.btn_refresh_tabs)
        self.btn_check_login = QPushButton("로그인 상태 확인")
        self.btn_check_login.clicked.connect(self.on_check_login)
        btn_row.addWidget(self.btn_check_login)
        btn_row.addStretch()
        tl.addLayout(btn_row)

        gemini_row = QHBoxLayout()
        gemini_row.addWidget(QLabel("Gemini 탭:"))
        self.gemini_tab_combo = QComboBox()
        self.gemini_tab_combo.setMinimumWidth(350)
        gemini_row.addWidget(self.gemini_tab_combo)
        self.btn_assign_gemini = QPushButton("Assign")
        self.btn_assign_gemini.clicked.connect(lambda: self.on_assign_tab("gemini"))
        gemini_row.addWidget(self.btn_assign_gemini)
        self.gemini_status = QLabel("⚫ 미연결")
        gemini_row.addWidget(self.gemini_status)
        tl.addLayout(gemini_row)

        claude_row = QHBoxLayout()
        claude_row.addWidget(QLabel("Claude 탭:"))
        self.claude_tab_combo = QComboBox()
        self.claude_tab_combo.setMinimumWidth(350)
        claude_row.addWidget(self.claude_tab_combo)
        self.btn_assign_claude = QPushButton("Assign")
        self.btn_assign_claude.clicked.connect(lambda: self.on_assign_tab("claude"))
        claude_row.addWidget(self.btn_assign_claude)
        self.claude_status = QLabel("⚫ 미연결")
        claude_row.addWidget(self.claude_status)
        tl.addLayout(claude_row)

        layout.addWidget(tab_box)

        # 3) 토론 설정
        settings_box = QGroupBox("토론 설정")
        sl = QVBoxLayout(settings_box)

        topic_row = QHBoxLayout()
        topic_row.addWidget(QLabel("주제:"))
        self.topic_input = QLineEdit()
        self.topic_input.setPlaceholderText("토론 주제를 입력하세요...")
        topic_row.addWidget(self.topic_input)
        sl.addLayout(topic_row)

        sl.addWidget(QLabel("프롬프트 템플릿:"))
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlainText(DEFAULT_PROMPT_TEMPLATE)
        self.prompt_edit.setMaximumHeight(120)
        sl.addWidget(self.prompt_edit)

        options_row = QHBoxLayout()

        # 선공 선택
        options_row.addWidget(QLabel("선공:"))
        self.first_group = QButtonGroup(self)
        self.radio_gemini_first = QRadioButton("Gemini")
        self.radio_gemini_first.setChecked(True)
        self.radio_claude_first = QRadioButton("Claude")
        self.first_group.addButton(self.radio_gemini_first)
        self.first_group.addButton(self.radio_claude_first)
        options_row.addWidget(self.radio_gemini_first)
        options_row.addWidget(self.radio_claude_first)

        options_row.addSpacing(20)

        # 모드 선택
        options_row.addWidget(QLabel("모드:"))
        self.mode_group = QButtonGroup(self)
        self.radio_auto = QRadioButton("자동")
        self.radio_auto.setChecked(True)
        self.radio_semi = QRadioButton("반자동")
        self.mode_group.addButton(self.radio_auto)
        self.mode_group.addButton(self.radio_semi)
        options_row.addWidget(self.radio_auto)
        options_row.addWidget(self.radio_semi)

        options_row.addSpacing(20)

        options_row.addWidget(QLabel("최대 턴:"))
        self.max_turns_spin = QSpinBox()
        self.max_turns_spin.setRange(1, 100)
        self.max_turns_spin.setValue(10)
        options_row.addWidget(self.max_turns_spin)

        options_row.addSpacing(10)
        options_row.addWidget(QLabel("타임아웃(초):"))
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(30, 600)
        self.timeout_spin.setValue(120)
        options_row.addWidget(self.timeout_spin)
        options_row.addStretch()
        sl.addLayout(options_row)

        layout.addWidget(settings_box)

        # 4) 제어
        ctrl_box = QGroupBox("제어")
        ctrl_l = QHBoxLayout(ctrl_box)
        self.btn_start = QPushButton("▶ Start")
        self.btn_start.clicked.connect(self.on_start)
        ctrl_l.addWidget(self.btn_start)
        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.clicked.connect(self.on_pause)
        self.btn_pause.setEnabled(False)
        ctrl_l.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_stop.setEnabled(False)
        ctrl_l.addWidget(self.btn_stop)
        self.btn_next_turn = QPushButton("→ Next Turn")
        self.btn_next_turn.clicked.connect(self.on_next_turn)
        self.btn_next_turn.setEnabled(False)
        ctrl_l.addWidget(self.btn_next_turn)
        self.discussion_status = QLabel("상태: IDLE")
        ctrl_l.addWidget(self.discussion_status)
        ctrl_l.addStretch()
        layout.addWidget(ctrl_box)

        # 5) 대화 로그
        log_box = QGroupBox("대화 로그")
        ll = QVBoxLayout(log_box)
        self.chat_log = QTextEdit()
        self.chat_log.setReadOnly(True)
        self.chat_log.setFont(QFont("Monospace", 10))
        self.chat_log.setMinimumHeight(250)
        ll.addWidget(self.chat_log)

        log_btn_row = QHBoxLayout()
        self.btn_export = QPushButton("Export Log")
        self.btn_export.clicked.connect(self.on_export_log)
        log_btn_row.addWidget(self.btn_export)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear_log)
        log_btn_row.addWidget(btn_clear)
        log_btn_row.addStretch()
        ll.addLayout(log_btn_row)
        layout.addWidget(log_box)

        # 6) 셀렉터 설정 (접히는 패널)
        self.selector_box = QGroupBox("셀렉터 설정 (클릭하여 펼치기)")
        self.selector_box.setCheckable(True)
        self.selector_box.setChecked(False)
        self._selector_content = QWidget()
        sel_l = QVBoxLayout(self._selector_content)

        # Gemini 셀렉터
        sel_l.addWidget(QLabel("--- Gemini ---"))
        gf = QFormLayout()
        self.sel_gemini_input = QLineEdit(default_gemini_selectors().input_selector)
        gf.addRow("Input:", self.sel_gemini_input)
        self.sel_gemini_send = QLineEdit(default_gemini_selectors().send_selector)
        gf.addRow("Send:", self.sel_gemini_send)
        self.sel_gemini_response = QLineEdit(default_gemini_selectors().response_selector)
        gf.addRow("Response:", self.sel_gemini_response)
        self.sel_gemini_stop = QLineEdit(default_gemini_selectors().stop_button_selector)
        gf.addRow("Stop:", self.sel_gemini_stop)
        sel_l.addLayout(gf)

        # Claude 셀렉터
        sel_l.addWidget(QLabel("--- Claude ---"))
        cf = QFormLayout()
        self.sel_claude_input = QLineEdit(default_claude_selectors().input_selector)
        cf.addRow("Input:", self.sel_claude_input)
        self.sel_claude_send = QLineEdit(default_claude_selectors().send_selector)
        cf.addRow("Send:", self.sel_claude_send)
        self.sel_claude_response = QLineEdit(default_claude_selectors().response_selector)
        cf.addRow("Response:", self.sel_claude_response)
        self.sel_claude_stop = QLineEdit(default_claude_selectors().stop_button_selector)
        cf.addRow("Stop:", self.sel_claude_stop)
        sel_l.addLayout(cf)

        btn_reset_sel = QPushButton("Reset Defaults")
        btn_reset_sel.clicked.connect(self.on_reset_selectors)
        sel_l.addWidget(btn_reset_sel)

        sel_box_layout = QVBoxLayout(self.selector_box)
        sel_box_layout.addWidget(self._selector_content)
        self._selector_content.setVisible(False)
        self.selector_box.toggled.connect(self._selector_content.setVisible)

        layout.addWidget(self.selector_box)

        # 7) 시스템 로그
        sys_box = QGroupBox("시스템 로그")
        sys_l = QVBoxLayout(sys_box)
        self.sys_log = QTextEdit()
        self.sys_log.setReadOnly(True)
        self.sys_log.setFont(QFont("Monospace", 9))
        self.sys_log.setMaximumHeight(120)
        sys_l.addWidget(self.sys_log)
        layout.addWidget(sys_box)

        scroll.setWidget(scroll_widget)
        main_layout.addWidget(scroll)

    # ----------------------------------------------------------------
    # 로그 유틸리티
    # ----------------------------------------------------------------
    def _syslog(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.sys_log.append(f"[{ts}] {msg}")

    def _append_chat(self, speaker: str, turn: int, text: str):
        color = "#2196F3" if speaker.lower() == "gemini" else "#FF9800"
        header = f'<div style="margin:8px 0 2px 0; font-weight:bold; color:{color};">' \
                 f'{speaker} (Turn {turn})</div>'
        body = f'<div style="margin:0 0 8px 12px; white-space:pre-wrap;">{text}</div>'
        self.chat_log.append(header + body)
        cursor = self.chat_log.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_log.setTextCursor(cursor)

    def _clear_log(self):
        self.chat_log.clear()
        self._turn_log.clear()

    # ----------------------------------------------------------------
    # 프로필 관리
    # ----------------------------------------------------------------
    def _refresh_profiles(self):
        self.profiles = self.profile_mgr.scan_profiles()
        self.profile_combo.clear()
        for p in self.profiles:
            label = f"{p['display_name']}  ({p['dir_name']}, port {p['port']})"
            self.profile_combo.addItem(label)

    def _current_profile(self) -> dict | None:
        idx = self.profile_combo.currentIndex()
        if 0 <= idx < len(self.profiles):
            return self.profiles[idx]
        return None

    # ----------------------------------------------------------------
    # Chrome 연결
    # ----------------------------------------------------------------
    def on_launch(self):
        p = self._current_profile()
        if not p:
            self._syslog("프로필을 선택하세요.")
            return
        if self.profile_mgr.is_port_open(p["port"]):
            self._syslog(f"포트 {p['port']}에 이미 크롬이 실행 중. Connect를 시도합니다.")
            self.on_connect()
            return

        err = self.profile_mgr.launch_chrome(p["dir_name"], p["port"])

        if err == "CHROME_RUNNING_NO_CDP":
            self._syslog("Chrome이 CDP 모드 없이 실행 중입니다.")
            reply = QMessageBox.warning(
                self,
                "Chrome 이미 실행 중",
                "Chrome이 이미 실행 중이지만 CDP(DevTools Protocol) 모드가 아닙니다.\n\n"
                "Chrome은 싱글 프로세스 아키텍처이므로, 이미 실행 중인 Chrome이 있으면\n"
                "--remote-debugging-port 플래그가 무시됩니다.\n\n"
                "모든 Chrome 창을 닫은 후 다시 Launch를 눌러주세요.\n\n"
                "'강제 종료'를 누르면 모든 Chrome 프로세스를 종료합니다.",
                QMessageBox.Retry | QMessageBox.Abort,
                QMessageBox.Retry,
            )
            if reply == QMessageBox.Abort:
                # 사용자가 강제 종료 선택
                killed = self.profile_mgr.kill_chrome()
                if killed:
                    self._syslog("Chrome 프로세스가 종료되었습니다. 다시 Launch를 눌러주세요.")
                else:
                    self._syslog("Chrome 종료에 실패했습니다. 수동으로 종료해 주세요.")
            return

        if err:
            self._syslog(f"크롬 실행 실패: {err}")
            return

        self._syslog(f"크롬 실행 중... ({p['display_name']}, port {p['port']})")
        QTimer.singleShot(2500, self.on_connect)

    def on_connect(self):
        p = self._current_profile()
        if not p:
            self._syslog("프로필을 선택하세요.")
            return
        port = p["port"]
        if not self.profile_mgr.is_port_open(port):
            self._syslog(f"포트 {port}에 크롬이 없습니다. Launch를 먼저 실행하세요.")
            return
        self._cdp_port = port
        # 탭 목록 가져오기 테스트
        test_cdp = CDPClient(port)
        tabs = test_cdp.list_tabs()
        if not tabs:
            self._syslog(f"포트 {port}에서 탭을 가져올 수 없습니다.")
            self.conn_status.setText("⚫ Disconnected")
            return
        self.conn_status.setText(f"🟢 Connected (port {port})")
        self._syslog(f"연결 성공: port {port}, 탭 {len(tabs)}개")
        self._populate_tabs(tabs)

    def on_refresh_tabs(self):
        if not self._cdp_port:
            self._syslog("먼저 Connect 하세요.")
            return
        test_cdp = CDPClient(self._cdp_port)
        tabs = test_cdp.list_tabs()
        self._populate_tabs(tabs)
        self._syslog(f"탭 {len(tabs)}개 조회됨")

    def _populate_tabs(self, tabs: list[dict]):
        self._tabs = tabs
        for combo in (self.gemini_tab_combo, self.claude_tab_combo):
            combo.clear()
            for t in tabs:
                title = t.get("title", "(no title)")[:60]
                url = t.get("url", "")[:60]
                combo.addItem(f"{title} | {url}")

    # ----------------------------------------------------------------
    # 탭 할당
    # ----------------------------------------------------------------
    def on_assign_tab(self, ai_type: str):
        if not self._cdp_port:
            self._syslog("먼저 Connect 하세요.")
            return

        combo = self.gemini_tab_combo if ai_type == "gemini" else self.claude_tab_combo
        idx = combo.currentIndex()
        if idx < 0 or idx >= len(self._tabs):
            self._syslog("탭을 선택하세요.")
            return

        tab = self._tabs[idx]
        ws_url = tab.get("webSocketDebuggerUrl", "")
        if not ws_url:
            self._syslog(f"탭에 webSocketDebuggerUrl이 없습니다.")
            return

        selectors = self._get_selectors(ai_type)
        name = "Gemini" if ai_type == "gemini" else "Claude"
        ctrl = AITabController(name, self._cdp_port, selectors)
        try:
            ctrl.connect_to_tab(ws_url)
        except Exception as e:
            self._syslog(f"{name} 탭 연결 실패: {e}")
            return

        if ai_type == "gemini":
            self.gemini_ctrl = ctrl
            self.gemini_status.setText("✅ 연결됨")
        else:
            self.claude_ctrl = ctrl
            self.claude_status.setText("✅ 연결됨")

        self._syslog(f"{name} 탭 할당 완료: {tab.get('title', '?')}")

        # 자동 로그인 확인
        ok, msg = ctrl.check_login_status()
        status_label = self.gemini_status if ai_type == "gemini" else self.claude_status
        if ok:
            status_label.setText(f"✅ 연결됨 (로그인: ✅)")
            self._syslog(f"{name} 로그인 상태: {msg}")
        else:
            status_label.setText(f"✅ 연결됨 (로그인: ❌)")
            self._syslog(f"{name} 로그인 상태: {msg}")

    def _get_selectors(self, ai_type: str) -> SelectorConfig:
        if ai_type == "gemini":
            return SelectorConfig(
                input_selector=self.sel_gemini_input.text(),
                send_selector=self.sel_gemini_send.text(),
                response_selector=self.sel_gemini_response.text(),
                stop_button_selector=self.sel_gemini_stop.text(),
            )
        else:
            return SelectorConfig(
                input_selector=self.sel_claude_input.text(),
                send_selector=self.sel_claude_send.text(),
                response_selector=self.sel_claude_response.text(),
                stop_button_selector=self.sel_claude_stop.text(),
            )

    # ----------------------------------------------------------------
    # 로그인 확인
    # ----------------------------------------------------------------
    def on_check_login(self):
        for name, ctrl, label in [
            ("Gemini", self.gemini_ctrl, self.gemini_status),
            ("Claude", self.claude_ctrl, self.claude_status),
        ]:
            if ctrl is None:
                self._syslog(f"{name}: 탭이 할당되지 않음")
                continue
            ok, msg = ctrl.check_login_status()
            login_icon = "✅" if ok else "❌"
            label.setText(f"✅ 연결됨 (로그인: {login_icon})")
            self._syslog(f"{name} 로그인: {msg}")

    # ----------------------------------------------------------------
    # 셀렉터 초기화
    # ----------------------------------------------------------------
    def on_reset_selectors(self):
        g = default_gemini_selectors()
        self.sel_gemini_input.setText(g.input_selector)
        self.sel_gemini_send.setText(g.send_selector)
        self.sel_gemini_response.setText(g.response_selector)
        self.sel_gemini_stop.setText(g.stop_button_selector)
        c = default_claude_selectors()
        self.sel_claude_input.setText(c.input_selector)
        self.sel_claude_send.setText(c.send_selector)
        self.sel_claude_response.setText(c.response_selector)
        self.sel_claude_stop.setText(c.stop_button_selector)
        self._syslog("셀렉터가 기본값으로 초기화되었습니다.")

    # ----------------------------------------------------------------
    # 토론 제어
    # ----------------------------------------------------------------
    def on_start(self):
        # 유효성 검사
        if not self.gemini_ctrl or not self.gemini_ctrl.connected:
            QMessageBox.warning(self, "오류", "Gemini 탭이 연결되지 않았습니다.")
            return
        if not self.claude_ctrl or not self.claude_ctrl.connected:
            QMessageBox.warning(self, "오류", "Claude 탭이 연결되지 않았습니다.")
            return

        topic = self.topic_input.text().strip()
        if not topic:
            QMessageBox.warning(self, "오류", "토론 주제를 입력하세요.")
            return

        # 로그인 확인
        for name, ctrl in [("Gemini", self.gemini_ctrl), ("Claude", self.claude_ctrl)]:
            ok, msg = ctrl.check_login_status()
            if not ok:
                reply = QMessageBox.question(
                    self, "로그인 확인",
                    f"{name}의 로그인 상태가 불확실합니다: {msg}\n계속 진행하시겠습니까?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply == QMessageBox.No:
                    return

        # 셀렉터 최신화
        self.gemini_ctrl.selectors = self._get_selectors("gemini")
        self.claude_ctrl.selectors = self._get_selectors("claude")

        # 선공 결정
        if self.radio_gemini_first.isChecked():
            first, second = self.gemini_ctrl, self.claude_ctrl
        else:
            first, second = self.claude_ctrl, self.gemini_ctrl

        auto_mode = self.radio_auto.isChecked()

        self._worker = DiscussionWorkerThread(
            first_ai=first,
            second_ai=second,
            topic=topic,
            prompt_template=self.prompt_edit.toPlainText(),
            max_turns=self.max_turns_spin.value(),
            timeout=self.timeout_spin.value(),
            auto_mode=auto_mode,
        )
        self._worker.turn_completed.connect(self._on_turn_completed)
        self._worker.state_changed.connect(self._on_state_changed)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.discussion_finished.connect(self._on_finished)
        self._worker.waiting_for_next.connect(self._on_waiting_next)

        self._turn_log.clear()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_next_turn.setEnabled(not auto_mode)

        self._syslog(f"토론 시작: 주제='{topic}', 선공={first.name}, 모드={'자동' if auto_mode else '반자동'}")
        self._worker.start()

    def on_pause(self):
        if not self._worker:
            return
        if self.btn_pause.text() == "⏸ Pause":
            self._worker.pause()
            self.btn_pause.setText("▶ Resume")
            self._syslog("토론 일시정지")
        else:
            self._worker.resume()
            self.btn_pause.setText("⏸ Pause")
            self._syslog("토론 재개")

    def on_stop(self):
        if self._worker:
            self._worker.stop()
            self._syslog("토론 중단 요청")
        self._reset_controls()

    def on_next_turn(self):
        if self._worker:
            self._worker.next_turn()
            self._syslog("다음 턴 진행")

    def _reset_controls(self):
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ Pause")
        self.btn_stop.setEnabled(False)
        self.btn_next_turn.setEnabled(False)

    # ----------------------------------------------------------------
    # 워커 시그널 핸들러
    # ----------------------------------------------------------------
    def _on_turn_completed(self, turn: int, speaker: str, text: str):
        self._turn_log.append({"turn": turn, "speaker": speaker, "text": text})
        self._append_chat(speaker, turn, text)
        self._syslog(f"턴 {turn} 완료 ({speaker}, {len(text)}자)")

    def _on_state_changed(self, state: str):
        self.discussion_status.setText(f"상태: {state}")
        if state in ("COMPLETED", "STOPPED", "ERROR"):
            self._reset_controls()

    def _on_error(self, msg: str):
        self._syslog(f"오류: {msg}")
        QMessageBox.warning(self, "토론 오류", msg)

    def _on_finished(self):
        self._syslog("토론이 완료되었습니다.")
        self._reset_controls()

    def _on_waiting_next(self):
        self.btn_next_turn.setEnabled(True)

    # ----------------------------------------------------------------
    # 로그 내보내기
    # ----------------------------------------------------------------
    def on_export_log(self):
        if not self._turn_log:
            self._syslog("내보낼 로그가 없습니다.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "토론 로그 저장", f"discussion_{datetime.now():%Y%m%d_%H%M%S}.json",
            "JSON (*.json);;Text (*.txt)"
        )
        if not path:
            return

        if path.endswith(".json"):
            export_data = {
                "topic": self.topic_input.text().strip(),
                "timestamp": datetime.now().isoformat(),
                "turns": self._turn_log,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"토론 주제: {self.topic_input.text().strip()}\n")
                f.write(f"시간: {datetime.now().isoformat()}\n")
                f.write("=" * 60 + "\n\n")
                for entry in self._turn_log:
                    f.write(f"--- {entry['speaker']} (Turn {entry['turn']}) ---\n")
                    f.write(entry["text"] + "\n\n")

        self._syslog(f"로그 저장: {path}")

    # ----------------------------------------------------------------
    # 종료 처리
    # ----------------------------------------------------------------
    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        if self.gemini_ctrl:
            self.gemini_ctrl.cdp.disconnect()
        if self.claude_ctrl:
            self.claude_ctrl.cdp.disconnect()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = DiscussionApp()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
