#!/usr/bin/env python3
"""Chrome CDP Controller - Chrome DevTools Protocol 기반 브라우저 제어 도구"""

import sys
import os
import json
import subprocess
import socket
import base64
import time
from datetime import datetime
from urllib.request import urlopen
from urllib.error import URLError

import websocket
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QPushButton, QLabel, QListWidget, QLineEdit, QTextEdit,
    QGroupBox, QFileDialog, QMessageBox, QListWidgetItem, QSplitter,
    QFormLayout, QTabWidget,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QPixmap, QImage


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHROME_CONFIG_DIR = os.path.expanduser("~/.config/google-chrome")
CHROME_BINARY = "/usr/bin/google-chrome"
BASE_PORT = 9222


# ---------------------------------------------------------------------------
# Chrome Profile Manager
# ---------------------------------------------------------------------------
class ChromeProfileManager:
    """크롬 프로필 탐색 및 실행 관리"""

    def __init__(self):
        self._processes: dict[str, subprocess.Popen] = {}

    @staticmethod
    def is_chrome_running() -> bool:
        """Chrome 메인 프로세스가 이미 실행 중인지 확인"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "chrome/chrome"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def has_debug_port(port: int) -> bool:
        """실행 중인 Chrome이 특정 debug 포트로 시작되었는지 /proc/PID/cmdline에서 확인"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "chrome/chrome"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return False
            flag = f"--remote-debugging-port={port}"
            for pid in result.stdout.strip().split("\n"):
                pid = pid.strip()
                if not pid:
                    continue
                try:
                    cmdline_path = f"/proc/{pid}/cmdline"
                    with open(cmdline_path, "rb") as f:
                        cmdline = f.read().decode("utf-8", errors="replace")
                    if flag in cmdline:
                        return True
                except (OSError, PermissionError):
                    continue
            return False
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def kill_chrome() -> bool:
        """모든 Chrome 프로세스를 종료한다. 성공 여부를 반환."""
        try:
            subprocess.run(["pkill", "-f", "chrome/chrome"], timeout=5)
            # 잠시 대기 후 확인
            time.sleep(1)
            result = subprocess.run(
                ["pgrep", "-f", "chrome/chrome"],
                capture_output=True, timeout=5,
            )
            return result.returncode != 0  # 프로세스가 없으면 성공
        except (OSError, subprocess.TimeoutExpired):
            return False

    def scan_profiles(self) -> list[dict]:
        """로컬 크롬 프로필 목록을 반환한다."""
        profiles = []
        if not os.path.isdir(CHROME_CONFIG_DIR):
            return profiles

        for entry in sorted(os.listdir(CHROME_CONFIG_DIR)):
            path = os.path.join(CHROME_CONFIG_DIR, entry)
            if not os.path.isdir(path):
                continue
            if entry != "Default" and not entry.startswith("Profile"):
                continue

            display_name = entry
            prefs_path = os.path.join(path, "Preferences")
            if os.path.isfile(prefs_path):
                try:
                    with open(prefs_path, encoding="utf-8") as f:
                        prefs = json.load(f)
                    display_name = prefs.get("profile", {}).get("name", entry)
                except (json.JSONDecodeError, OSError):
                    pass

            port = self._assign_port(entry)
            profiles.append({
                "dir_name": entry,
                "display_name": display_name,
                "port": port,
            })
        return profiles

    def launch_chrome(self, profile_dir: str, port: int) -> str | None:
        """특정 프로필로 크롬을 실행한다.

        반환값:
            None  — 성공 (새로 실행됨)
            "CHROME_ALREADY_CDP" — Chrome이 이미 CDP 플래그로 실행 중 (포트 대기 필요)
            "CHROME_RUNNING_NO_CDP" — Chrome이 CDP 없이 실행 중 (사용자에게 안내 필요)
            기타 문자열 — OS 에러 메시지
        """
        # 1. 포트 이미 열림 → CDP가 이미 활성 (OK)
        if self.is_port_open(port):
            return None

        # 2. Chrome 프로세스가 실행 중인데 포트가 안 열림
        #    → 싱글 프로세스 아키텍처 문제: --remote-debugging-port가 무시됨
        if self.is_chrome_running():
            if self.has_debug_port(port):
                # CDP 플래그로 시작되었지만 포트가 아직 안 열린 경우 (시작 중)
                return "CHROME_ALREADY_CDP"
            return "CHROME_RUNNING_NO_CDP"

        # 3. Chrome 프로세스 없음 → 정상 실행
        try:
            proc = subprocess.Popen(
                [
                    CHROME_BINARY,
                    f"--profile-directory={profile_dir}",
                    f"--remote-debugging-port={port}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._processes[profile_dir] = proc
            return None
        except OSError as e:
            return str(e)

    @staticmethod
    def is_port_open(port: int) -> bool:
        """포트가 열려 있는지 확인한다 (IPv4/IPv6 모두 시도)."""
        try:
            conn = socket.create_connection(("localhost", port), timeout=0.5)
            conn.close()
            return True
        except OSError:
            return False

    @staticmethod
    def _assign_port(profile_dir: str) -> int:
        if profile_dir == "Default":
            return BASE_PORT
        try:
            n = int(profile_dir.split()[-1])
            return BASE_PORT + n
        except (ValueError, IndexError):
            return BASE_PORT + abs(hash(profile_dir)) % 100


# ---------------------------------------------------------------------------
# CDP Client
# ---------------------------------------------------------------------------
class CDPClient:
    """Chrome DevTools Protocol 클라이언트"""

    def __init__(self, port: int, host: str = "localhost"):
        self.port = port
        self._host = host
        self._ws: websocket.WebSocket | None = None
        self._msg_id = 0
        self._active_target_id: str = ""

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.connected

    def list_tabs(self) -> list[dict]:
        """열린 탭 목록을 반환한다."""
        try:
            host = getattr(self, "_host", "localhost")
            host_str = f"[{host}]" if ":" in host else host
            resp = urlopen(f"http://{host_str}:{self.port}/json", timeout=3)
            tabs = json.loads(resp.read().decode())
            return [t for t in tabs if t.get("type") == "page"]
        except (URLError, OSError, json.JSONDecodeError):
            return []

    def connect_tab(self, ws_url: str):
        """특정 탭에 WebSocket으로 연결하고 활성화한다."""
        self.disconnect()
        # Chrome은 Origin 헤더 검사를 수행한다. IPv6(::1)로 연결할 때도
        # Origin은 'http://localhost'로 고정하여 403 거부를 방지한다.
        self._ws = websocket.create_connection(
            ws_url, timeout=10, origin="http://localhost"
        )
        # B-1: Input.* 명령이 올바른 탭에 적용되도록 탭 활성화
        # ws_url에서 targetId 파싱: "ws://localhost:PORT/devtools/page/{targetId}"
        self._active_target_id = ws_url.rstrip("/").split("/")[-1]
        self.send_command("Target.activateTarget", {"targetId": self._active_target_id})

    def disconnect(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._msg_id = 0  # H-1: 재연결 후 메시지 ID 오염 방지

    def send_command(self, method: str, params: dict | None = None) -> dict:
        """CDP 명령을 보내고 응답을 반환한다."""
        if not self.connected:
            return {"error": {"message": "WebSocket 연결이 없습니다"}}

        self._msg_id += 1
        msg_id = self._msg_id
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        self._ws.send(payload)

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                raw = self._ws.recv()
                resp = json.loads(raw)
                if resp.get("id") == msg_id:
                    return resp
                # A-2: CDP 비동기 이벤트 메시지 (id 없음) — 무시하고 계속 대기
                continue
            except websocket.WebSocketTimeoutException:
                # A-1: break → continue — 데드라인까지 재시도
                continue
            except Exception as e:
                return {"error": {"message": str(e)}}

        return {"error": {"message": "응답 타임아웃"}}

    # -- 편의 메서드 --

    def navigate(self, url: str) -> dict:
        return self.send_command("Page.navigate", {"url": url})

    def _get_element_center(self, selector: str) -> tuple[float, float] | None:
        """CSS 셀렉터로 요소의 중앙 좌표를 반환한다. 요소가 없으면 None."""
        resp = self.query_selector(selector)
        node_id = resp.get("result", {}).get("nodeId", 0)
        if node_id == 0:
            return None
        box_resp = self.send_command("DOM.getBoxModel", {"nodeId": node_id})
        model = box_resp.get("result", {}).get("model", {})
        # content quad: [x0,y0, x1,y1, x2,y2, x3,y3] (시계 방향, 좌상단부터)
        content = model.get("content", [])
        if len(content) < 8:
            return None
        x = (content[0] + content[2] + content[4] + content[6]) / 4
        y = (content[1] + content[3] + content[5] + content[7]) / 4
        return x, y

    def click(self, selector: str) -> dict:
        """CSS 셀렉터로 요소를 CDP 마우스 이벤트로 클릭한다. (isTrusted: true)"""
        center = self._get_element_center(selector)
        if center is None:
            return {"error": {"message": f"Element not found or no box model: {selector}"}}
        x, y = center
        self.send_command("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x, "y": y,
            "button": "left",
            "clickCount": 1,
            "modifiers": 0,
        })
        return self.send_command("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x, "y": y,
            "button": "left",
            "clickCount": 1,
            "modifiers": 0,
        })

    def type_text(self, selector: str, text: str) -> dict:
        js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return 'Element not found';
            el.focus();
            el.value = {json.dumps(text)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'OK';
        }})()
        """
        return self.execute_js(js)

    def type_contenteditable(self, selector: str, text: str, fallback_selectors: list[str] | None = None) -> dict:
        """contenteditable div에 텍스트 입력 (3단계 전략: CDP insertText → dispatchKeyEvent → execCommand)"""
        selectors = [selector] + (fallback_selectors or [])

        # Step 0: JS로 요소를 찾고 focus + Selection API로 기존 내용 클리어 (React DOM 유지)
        focus_js = f"""
        (() => {{
            const selectors = {json.dumps(selectors)};
            let el = null;
            for (const sel of selectors) {{
                el = document.querySelector(sel);
                if (el) break;
            }}
            if (!el) return 'Element not found: ' + selectors.join(', ');
            el.focus();
            // C-1: innerHTML='' 대신 Selection API로 전체 선택 후 React 친화적 클리어
            const range = document.createRange();
            range.selectNodeContents(el);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            return 'FOCUSED';
        }})()
        """
        resp = self.execute_js(focus_js)
        result_val = resp.get("result", {}).get("result", {}).get("value", "")
        if "not found" in result_val.lower():
            return resp

        # Selection 후 Delete 키로 내용 삭제 (isTrusted: true)
        self.send_command("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Delete", "code": "Delete",
            "windowsVirtualKeyCode": 46, "nativeVirtualKeyCode": 46,
        })
        self.send_command("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Delete", "code": "Delete",
            "windowsVirtualKeyCode": 46, "nativeVirtualKeyCode": 46,
        })

        # C-2: 클리어 검증
        clear_verify_js = f"""
        (() => {{
            const selectors = {json.dumps(selectors)};
            let el = null;
            for (const sel of selectors) {{
                el = document.querySelector(sel);
                if (el) break;
            }}
            if (!el) return false;
            return (el.innerText || el.textContent || '').trim() === '';
        }})()
        """
        clear_ok = self.execute_js(clear_verify_js).get("result", {}).get("result", {}).get("value", False)
        if not clear_ok:
            # 클리어 실패 시 execCommand 폴백
            self.execute_js("document.execCommand('selectAll', false, null)")
            self.execute_js("document.execCommand('delete', false, null)")

        # B-2: Input.insertText 전 탭 활성화 재확인
        if hasattr(self, '_active_target_id') and self._active_target_id:
            self.send_command("Target.activateTarget", {"targetId": self._active_target_id})

        # Step 1: CDP Input.insertText (가장 자연스러운 네이티브 입력)
        insert_resp = self.send_command("Input.insertText", {"text": text})
        if "error" not in insert_resp:
            verify = self._verify_input_content(selector, text)
            if verify:
                return {"result": {"result": {"value": "OK (CDP insertText)"}}}

        # Step 2: CDP Input.dispatchKeyEvent (글자별 전송, 500자 이하만)
        if len(text) <= 500:
            self.execute_js(focus_js)
            success = True
            for char in text:
                r = self.send_command("Input.dispatchKeyEvent", {
                    "type": "keyDown",
                    "text": char,
                    "key": char,
                    "code": "",
                    "unmodifiedText": char,
                })
                if "error" in r:
                    success = False
                    break
                self.send_command("Input.dispatchKeyEvent", {
                    "type": "keyUp",
                    "key": char,
                    "code": "",
                })
            if success:
                verify = self._verify_input_content(selector, text)
                if verify:
                    return {"result": {"result": {"value": "OK (CDP dispatchKeyEvent)"}}}

        # Step 3: 레거시 execCommand 폴백
        self.execute_js(focus_js)
        legacy_js = f"""
        (() => {{
            const el = document.activeElement;
            if (!el) return 'No active element';
            const text = {json.dumps(text)};
            const success = document.execCommand('insertText', false, text);
            if (success) return 'OK (execCommand)';
            el.innerText = text;
            el.dispatchEvent(new InputEvent('input', {{
                bubbles: true, cancelable: true,
                inputType: 'insertText', data: text
            }}));
            return 'OK (innerText fallback)';
        }})()
        """
        return self.execute_js(legacy_js)

    def _verify_input_content(self, selector: str, expected_text: str = "") -> bool:
        """입력된 요소에 expected_text 앞부분이 실제로 포함되었는지 확인한다.
        D-1: activeElement 대신 selector 기반으로 검증
        D-2: 텍스트 길이 > 0 대신 expected_text 포함 여부 비교
        """
        if expected_text:
            snippet = json.dumps(expected_text[:30])
            verify_js = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return false;
                const content = (el.innerText || el.textContent || '').trim();
                return content.includes({snippet});
            }})()
            """
        else:
            verify_js = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return false;
                const content = (el.innerText || el.textContent || '').trim();
                return content.length > 0;
            }})()
            """
        resp = self.execute_js(verify_js)
        return resp.get("result", {}).get("result", {}).get("value", False)

    def press_enter(self, selector: str) -> dict:
        """지정된 요소에 CDP Enter 키 이벤트를 전송한다. (isTrusted: true)"""
        # focus는 JS로 (isTrusted 불필요)
        self.execute_js(
            f"document.querySelector({json.dumps(selector)})?.focus()"
        )
        # CDP keyDown + keyUp (isTrusted: true)
        self.send_command("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
            "unmodifiedText": "\r",
            "text": "\r",
        })
        return self.send_command("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        })

    def execute_js(self, expression: str) -> dict:
        return self.send_command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })

    def screenshot(self) -> bytes | None:
        resp = self.send_command("Page.captureScreenshot", {"format": "png"})
        data = resp.get("result", {}).get("data")
        if data:
            return base64.b64decode(data)
        return None

    def get_document(self) -> dict:
        return self.send_command("DOM.getDocument")

    def query_selector(self, selector: str) -> dict:
        doc = self.get_document()
        node_id = doc.get("result", {}).get("root", {}).get("nodeId", 1)
        return self.send_command("DOM.querySelector", {
            "nodeId": node_id,
            "selector": selector,
        })

    def get_outer_html(self, selector: str) -> str:
        resp = self.query_selector(selector)
        node_id = resp.get("result", {}).get("nodeId", 0)
        if node_id == 0:
            return "Element not found"
        html_resp = self.send_command("DOM.getOuterHTML", {"nodeId": node_id})
        return html_resp.get("result", {}).get("outerHTML", "")


# ---------------------------------------------------------------------------
# CDP Worker Thread (GUI 블로킹 방지)
# ---------------------------------------------------------------------------
class CDPWorkerThread(QThread):
    finished = pyqtSignal(str, object)  # (command_name, result)

    def __init__(self, func, name="command"):
        super().__init__()
        self._func = func
        self._name = name

    def run(self):
        try:
            result = self._func()
            self.finished.emit(self._name, result)
        except Exception as e:
            self.finished.emit(self._name, {"error": {"message": str(e)}})


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Chrome CDP Controller")
        self.setMinimumSize(900, 700)

        self.profile_mgr = ChromeProfileManager()
        self.cdp: CDPClient | None = None
        self.profiles: list[dict] = []
        self._workers: list[CDPWorkerThread] = []

        self._build_ui()
        self._refresh_profiles()

    # -- UI 구성 --

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)

        # 1) 프로필 영역
        profile_box = QGroupBox("Chrome 프로필")
        pl = QHBoxLayout(profile_box)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(250)
        pl.addWidget(self.profile_combo)

        self.btn_launch = QPushButton("Launch Chrome")
        self.btn_launch.clicked.connect(self.on_launch)
        pl.addWidget(self.btn_launch)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.on_connect)
        pl.addWidget(self.btn_connect)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        pl.addWidget(self.btn_disconnect)

        self.status_label = QLabel("⚫ Disconnected")
        pl.addWidget(self.status_label)
        pl.addStretch()
        layout.addWidget(profile_box)

        # 2) 탭 목록 + URL 바
        tab_box = QGroupBox("탭 목록")
        tl = QVBoxLayout(tab_box)

        tab_top = QHBoxLayout()
        self.btn_refresh_tabs = QPushButton("Refresh Tabs")
        self.btn_refresh_tabs.clicked.connect(self.on_refresh_tabs)
        tab_top.addWidget(self.btn_refresh_tabs)
        tab_top.addStretch()
        tl.addLayout(tab_top)

        self.tab_list = QListWidget()
        self.tab_list.itemClicked.connect(self.on_tab_selected)
        tl.addWidget(self.tab_list)

        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("URL:"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com")
        self.url_input.returnPressed.connect(self.on_navigate)
        url_layout.addWidget(self.url_input)
        self.btn_go = QPushButton("Go")
        self.btn_go.clicked.connect(self.on_navigate)
        url_layout.addWidget(self.btn_go)
        tl.addLayout(url_layout)

        layout.addWidget(tab_box)

        # 3) 자동화 패널 (탭 위젯)
        auto_tabs = QTabWidget()

        # 3a) Click
        click_w = QWidget()
        cl = QFormLayout(click_w)
        self.click_selector = QLineEdit()
        self.click_selector.setPlaceholderText("#submit-btn, .my-class, etc.")
        cl.addRow("CSS Selector:", self.click_selector)
        btn_click = QPushButton("Click")
        btn_click.clicked.connect(self.on_click)
        cl.addRow(btn_click)
        auto_tabs.addTab(click_w, "Click")

        # 3b) Type
        type_w = QWidget()
        tyl = QFormLayout(type_w)
        self.type_selector = QLineEdit()
        self.type_selector.setPlaceholderText("input[name='email']")
        tyl.addRow("CSS Selector:", self.type_selector)
        self.type_text_input = QLineEdit()
        self.type_text_input.setPlaceholderText("입력할 텍스트")
        tyl.addRow("Text:", self.type_text_input)
        btn_type = QPushButton("Type")
        btn_type.clicked.connect(self.on_type)
        tyl.addRow(btn_type)
        auto_tabs.addTab(type_w, "Type")

        # 3c) JS
        js_w = QWidget()
        jl = QVBoxLayout(js_w)
        self.js_input = QTextEdit()
        self.js_input.setPlaceholderText("document.title")
        self.js_input.setMaximumHeight(100)
        jl.addWidget(self.js_input)
        btn_js = QPushButton("Execute JS")
        btn_js.clicked.connect(self.on_execute_js)
        jl.addWidget(btn_js)
        auto_tabs.addTab(js_w, "JavaScript")

        # 3d) Screenshot
        ss_w = QWidget()
        sl = QVBoxLayout(ss_w)
        btn_ss = QPushButton("Take Screenshot")
        btn_ss.clicked.connect(self.on_screenshot)
        sl.addWidget(btn_ss)
        self.screenshot_label = QLabel()
        self.screenshot_label.setAlignment(Qt.AlignCenter)
        sl.addWidget(self.screenshot_label)
        sl.addStretch()
        auto_tabs.addTab(ss_w, "Screenshot")

        # 3e) DOM 조회
        dom_w = QWidget()
        dl = QFormLayout(dom_w)
        self.dom_selector = QLineEdit()
        self.dom_selector.setPlaceholderText("body > div")
        dl.addRow("CSS Selector:", self.dom_selector)
        btn_dom = QPushButton("Get HTML")
        btn_dom.clicked.connect(self.on_get_html)
        dl.addRow(btn_dom)
        self.dom_output = QTextEdit()
        self.dom_output.setReadOnly(True)
        self.dom_output.setMaximumHeight(150)
        dl.addRow(self.dom_output)
        auto_tabs.addTab(dom_w, "DOM")

        layout.addWidget(auto_tabs)

        # 4) 로그 영역
        log_box = QGroupBox("로그")
        ll = QVBoxLayout(log_box)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Monospace", 9))
        self.log_output.setMaximumHeight(180)
        ll.addWidget(self.log_output)
        btn_clear_log = QPushButton("Clear Log")
        btn_clear_log.clicked.connect(self.log_output.clear)
        ll.addWidget(btn_clear_log)
        layout.addWidget(log_box)

    # -- 로그 --

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_output.append(f"[{ts}] {msg}")

    def _update_status(self, connected: bool, port: int = 0):
        if connected:
            self.status_label.setText(f"🟢 Connected (port {port})")
        else:
            self.status_label.setText("⚫ Disconnected")

    # -- 프로필 --

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

    # -- 슬롯 --

    def on_launch(self):
        p = self._current_profile()
        if not p:
            self._log("프로필을 선택하세요.")
            return

        if self.profile_mgr.is_port_open(p["port"]):
            self._log(f"포트 {p['port']}에 이미 크롬이 실행 중입니다. Connect를 시도합니다.")
            self.on_connect()
            return

        result = self.profile_mgr.launch_chrome(p["dir_name"], p["port"])

        if result == "CHROME_ALREADY_CDP":
            self._log(f"크롬이 이미 CDP 모드로 실행 중 (port {p['port']}). 포트 열림 대기 중...")
            QTimer.singleShot(2500, self.on_connect)
            return

        if result == "CHROME_RUNNING_NO_CDP":
            self._log("Chrome이 CDP 모드 없이 실행 중입니다.")
            reply = QMessageBox.warning(
                self,
                "Chrome 이미 실행 중",
                "Chrome이 이미 실행 중이지만 CDP 모드가 아닙니다.\n\n"
                "모든 Chrome 창을 닫은 후 다시 Launch를 눌러주세요.\n\n"
                "'강제 종료'를 누르면 모든 Chrome 프로세스를 종료합니다.",
                QMessageBox.Retry | QMessageBox.Abort,
                QMessageBox.Retry,
            )
            if reply == QMessageBox.Abort:
                killed = self.profile_mgr.kill_chrome()
                if killed:
                    self._log("Chrome 프로세스가 종료되었습니다. 다시 Launch를 눌러주세요.")
                else:
                    self._log("Chrome 종료에 실패했습니다. 수동으로 종료해 주세요.")
            return

        if result:
            self._log(f"크롬 실행 실패: {result}")
            return

        self._log(f"크롬 실행 중... ({p['display_name']}, port {p['port']})")
        QTimer.singleShot(2500, self.on_connect)

    def on_connect(self):
        p = self._current_profile()
        if not p:
            self._log("프로필을 선택하세요.")
            return

        port = p["port"]
        if not self.profile_mgr.is_port_open(port):
            self._log(f"포트 {port}에 크롬이 실행되고 있지 않습니다. Launch를 먼저 실행하세요.")
            return

        self.cdp = CDPClient(port)
        tabs = self.cdp.list_tabs()
        if not tabs:
            self._log(f"포트 {port}에서 탭을 가져올 수 없습니다.")
            self._update_status(False)
            return

        self._populate_tabs(tabs)
        # 첫 번째 탭에 자동 연결
        ws_url = tabs[0].get("webSocketDebuggerUrl")
        if ws_url:
            self.cdp.connect_tab(ws_url)
            self._update_status(True, port)
            self._log(f"연결 성공: {tabs[0].get('title', '?')}")
        else:
            self._log("webSocketDebuggerUrl을 찾을 수 없습니다.")
            self._update_status(False)

    def on_disconnect(self):
        if self.cdp:
            self.cdp.disconnect()
        self._update_status(False)
        self._log("연결 해제됨")

    def on_refresh_tabs(self):
        if not self.cdp:
            self._log("먼저 Connect 하세요.")
            return
        tabs = self.cdp.list_tabs()
        self._populate_tabs(tabs)
        self._log(f"탭 {len(tabs)}개 조회됨")

    def _populate_tabs(self, tabs: list[dict]):
        self.tab_list.clear()
        for t in tabs:
            title = t.get("title", "(no title)")
            url = t.get("url", "")
            item = QListWidgetItem(f"{title}\n  {url}")
            item.setData(Qt.UserRole, t.get("webSocketDebuggerUrl", ""))
            item.setData(Qt.UserRole + 1, url)
            self.tab_list.addItem(item)

    def on_tab_selected(self, item: QListWidgetItem):
        ws_url = item.data(Qt.UserRole)
        tab_url = item.data(Qt.UserRole + 1)
        if not ws_url or not self.cdp:
            return
        self.cdp.connect_tab(ws_url)
        self.url_input.setText(tab_url or "")
        self._update_status(True, self.cdp.port)
        self._log(f"탭 전환: {item.text().split(chr(10))[0]}")

    def on_navigate(self):
        url = self.url_input.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://", "file://", "chrome://")):
            url = "https://" + url
            self.url_input.setText(url)
        if not self.cdp or not self.cdp.connected:
            self._log("먼저 Connect 하세요.")
            return
        resp = self.cdp.navigate(url)
        if "error" in resp:
            self._log(f"Navigate 오류: {resp['error'].get('message', resp['error'])}")
        else:
            self._log(f"Navigate: {url}")

    def on_click(self):
        sel = self.click_selector.text().strip()
        if not sel:
            self._log("CSS Selector를 입력하세요.")
            return
        if not self.cdp or not self.cdp.connected:
            self._log("먼저 Connect 하세요.")
            return
        resp = self.cdp.click(sel)
        self._log_cdp_result("Click", resp)

    def on_type(self):
        sel = self.type_selector.text().strip()
        text = self.type_text_input.text()
        if not sel:
            self._log("CSS Selector를 입력하세요.")
            return
        if not self.cdp or not self.cdp.connected:
            self._log("먼저 Connect 하세요.")
            return
        resp = self.cdp.type_text(sel, text)
        self._log_cdp_result("Type", resp)

    def on_execute_js(self):
        js = self.js_input.toPlainText().strip()
        if not js:
            self._log("JavaScript 코드를 입력하세요.")
            return
        if not self.cdp or not self.cdp.connected:
            self._log("먼저 Connect 하세요.")
            return
        resp = self.cdp.execute_js(js)
        self._log_cdp_result("JS", resp)

    def on_screenshot(self):
        if not self.cdp or not self.cdp.connected:
            self._log("먼저 Connect 하세요.")
            return

        def _do():
            return self.cdp.screenshot()

        worker = CDPWorkerThread(_do, "screenshot")
        worker.finished.connect(self._on_screenshot_done)
        self._workers.append(worker)
        worker.start()
        self._log("스크린샷 촬영 중...")

    def _on_screenshot_done(self, name: str, result):
        if isinstance(result, dict) and "error" in result:
            self._log(f"스크린샷 오류: {result['error'].get('message', '')}")
            return
        if not result:
            self._log("스크린샷 데이터가 없습니다.")
            return

        # 미리보기 표시
        img = QImage.fromData(result)
        if not img.isNull():
            pix = QPixmap.fromImage(img).scaledToWidth(
                400, Qt.SmoothTransformation
            )
            self.screenshot_label.setPixmap(pix)

        # 저장 다이얼로그
        path, _ = QFileDialog.getSaveFileName(
            self, "스크린샷 저장", "screenshot.png", "PNG (*.png)"
        )
        if path:
            with open(path, "wb") as f:
                f.write(result)
            self._log(f"스크린샷 저장: {path}")
        else:
            self._log("스크린샷 촬영 완료 (저장 안 함)")

    def on_get_html(self):
        sel = self.dom_selector.text().strip()
        if not sel:
            self._log("CSS Selector를 입력하세요.")
            return
        if not self.cdp or not self.cdp.connected:
            self._log("먼저 Connect 하세요.")
            return
        html = self.cdp.get_outer_html(sel)
        self.dom_output.setPlainText(html)
        self._log(f"DOM 조회: {sel} ({len(html)} chars)")

    def _log_cdp_result(self, label: str, resp: dict):
        if "error" in resp:
            self._log(f"{label} 오류: {resp['error'].get('message', resp['error'])}")
        else:
            result = resp.get("result", {})
            value = result.get("result", {})
            if isinstance(value, dict):
                display = value.get("value", value.get("description", str(value)))
            else:
                display = str(value)
            self._log(f"{label} 결과: {display}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
