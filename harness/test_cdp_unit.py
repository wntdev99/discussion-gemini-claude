"""L1 CDP 단위 검증 — 실 Chrome 연결, 더미 HTML 페이지 대상 CDP 명령 검증"""
import sys
import time
import json as _json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from urllib.request import urlopen

from chrome_cdp_controller import CDPClient
from harness.config import CDP_PORT, L1_DUMMY_HTML_CONTENT, L1_INPUT_REPEAT, L1_COMMAND_TIMEOUT
from harness.reporter import HarnessReporter

LEVEL = "L1"

# isTrusted 검증용 이벤트 리스너 주입 JS
_INJECT_LISTENER_JS = """
(() => {
    const btn = document.getElementById('btn');
    if (btn) {
        btn.addEventListener('click', function(e) {
            this.dataset.clickTrusted = e.isTrusted ? '1' : '0';
        });
    }
    const ed = document.getElementById('editor');
    if (ed) {
        ed.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                this.dataset.enterTrusted = e.isTrusted ? '1' : '0';
            }
        });
    }
    return 'OK';
})()
"""


def run(reporter: HarnessReporter) -> None:
    """L1 CDP 단위 검증 체크를 순서대로 수행한다."""

    # ------------------------------------------------------------------
    # 탭 목록 조회 및 CDPClient 연결
    # ------------------------------------------------------------------
    try:
        tabs = _json.loads(urlopen(f"http://localhost:{CDP_PORT}/json", timeout=3).read())
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            reporter.fail(LEVEL, "탭 연결", "type=page 탭 없음")
            return
        cdp = CDPClient(CDP_PORT)
        cdp.connect_tab(page_tabs[0]["webSocketDebuggerUrl"])
    except Exception as e:
        reporter.fail(LEVEL, "탭 연결", f"예외: {e}")
        return

    # ------------------------------------------------------------------
    # [연결/기본]
    # ------------------------------------------------------------------

    # 1. connect_tab 성공
    try:
        if cdp.connected:
            reporter.ok(LEVEL, "connect_tab 성공", "cdp.connected == True")
        else:
            reporter.fail(LEVEL, "connect_tab 성공", "cdp.connected == False")
    except Exception as e:
        reporter.fail(LEVEL, "connect_tab 성공", f"예외: {e}")

    # 2. send_command 기본 응답 [A-1/A-2]
    try:
        resp = cdp.execute_js("1+1")
        value = resp.get("result", {}).get("result", {}).get("value")
        if value == 2:
            reporter.ok(LEVEL, "send_command 기본 응답", "value=2 확인됨", issue_ref="A-1/A-2")
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "send_command 기본 응답",
                f"예상 value=2, 실제 value={value!r}",
                issue_ref="A-1/A-2",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "send_command 기본 응답", f"예외: {e}", issue_ref="A-1/A-2")

    # 3. Target.activateTarget [B-1]
    try:
        resp = cdp.send_command(
            "Target.activateTarget",
            {"targetId": cdp._active_target_id},
        )
        if "error" not in resp:
            reporter.ok(LEVEL, "Target.activateTarget", "error 없음", issue_ref="B-1")
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "Target.activateTarget",
                f"error 응답: {resp['error']}",
                issue_ref="B-1",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "Target.activateTarget", f"예외: {e}", issue_ref="B-1")

    # 4. disconnect 후 _msg_id 리셋 [H-1]
    try:
        cdp.disconnect()
        ok = cdp._msg_id == 0
        if ok:
            reporter.ok(LEVEL, "disconnect 후 _msg_id 리셋", "_msg_id == 0 확인됨", issue_ref="H-1")
        else:
            reporter.fail(
                LEVEL,
                "disconnect 후 _msg_id 리셋",
                f"_msg_id == {cdp._msg_id} (0이어야 함)",
                issue_ref="H-1",
            )
        # 재연결
        cdp.connect_tab(page_tabs[0]["webSocketDebuggerUrl"])
    except Exception as e:
        reporter.fail(LEVEL, "disconnect 후 _msg_id 리셋", f"예외: {e}", issue_ref="H-1")
        # 재연결 시도
        try:
            cdp.connect_tab(page_tabs[0]["webSocketDebuggerUrl"])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # [더미 페이지 이동]
    # ------------------------------------------------------------------

    # 5. navigate 더미 페이지
    # data: URL은 < > 문자 부분 인코딩 문제로 깨질 수 있으므로
    # about:blank → document.write() 방식으로 HTML을 주입한다.
    navigate_ok = False
    try:
        cdp.navigate("about:blank")
        time.sleep(0.5)
        inject_js = (
            "(() => {"
            f"  document.open(); document.write({_json.dumps(L1_DUMMY_HTML_CONTENT)}); document.close();"
            "  return document.getElementById('editor') !== null;"
            "})()"
        )
        resp = cdp.execute_js(inject_js)
        has_editor = resp.get("result", {}).get("result", {}).get("value", False)
        time.sleep(0.3)
        if has_editor:
            navigate_ok = True
            reporter.ok(LEVEL, "navigate 더미 페이지", "about:blank + document.write 성공, #editor 존재 확인")
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "navigate 더미 페이지",
                "#editor 요소 없음 — document.write 실패 가능성",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "navigate 더미 페이지", f"예외: {e}")

    # ------------------------------------------------------------------
    # isTrusted 검증 — 이벤트 리스너 주입
    # ------------------------------------------------------------------
    if navigate_ok:
        try:
            cdp.execute_js(_INJECT_LISTENER_JS)
        except Exception:
            pass  # 리스너 주입 실패 시 6~8번 체크에서 개별 fail

    # 6. _get_element_center 좌표 반환
    try:
        center = cdp._get_element_center("#btn")
        if center is not None:
            x, y = center
            reporter.ok(LEVEL, "_get_element_center 좌표 반환", f"x={x:.1f}, y={y:.1f}")
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "_get_element_center 좌표 반환",
                "#btn 좌표 반환 None",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "_get_element_center 좌표 반환", f"예외: {e}")

    # 7. click isTrusted [E-1]
    try:
        cdp.click("#btn")
        time.sleep(0.3)
        resp = cdp.execute_js("document.getElementById('btn').dataset.clickTrusted")
        val = resp.get("result", {}).get("result", {}).get("value")
        if val == "1":
            reporter.ok(LEVEL, "click isTrusted", "isTrusted=True 확인됨", issue_ref="E-1")
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "click isTrusted",
                f"dataset.clickTrusted={val!r} (기대값 '1')",
                issue_ref="E-1",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "click isTrusted", f"예외: {e}", issue_ref="E-1")

    # 8. press_enter isTrusted [E-2]
    try:
        cdp.press_enter("#editor")
        time.sleep(0.3)
        resp = cdp.execute_js("document.getElementById('editor').dataset.enterTrusted")
        val = resp.get("result", {}).get("result", {}).get("value")
        if val == "1":
            reporter.ok(LEVEL, "press_enter isTrusted", "isTrusted=True 확인됨", issue_ref="E-2")
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "press_enter isTrusted",
                f"dataset.enterTrusted={val!r} (기대값 '1')",
                issue_ref="E-2",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "press_enter isTrusted", f"예외: {e}", issue_ref="E-2")

    # ------------------------------------------------------------------
    # [contenteditable 입력 검증]
    # ------------------------------------------------------------------

    # 9. type_contenteditable Selection API 클리어 [C-1/C-2]
    try:
        # 기존 내용 삽입
        cdp.execute_js(
            "document.getElementById('editor').innerText = '기존 내용';"
        )
        time.sleep(0.2)
        cdp.type_contenteditable("#editor", "새 내용")
        time.sleep(0.3)
        resp = cdp.execute_js(
            "document.getElementById('editor').innerText.includes('기존 내용')"
        )
        still_has_old = resp.get("result", {}).get("result", {}).get("value", True)
        if not still_has_old:
            reporter.ok(
                LEVEL,
                "type_contenteditable Selection API 클리어",
                "기존 내용 제거 확인됨",
                issue_ref="C-1/C-2",
            )
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "type_contenteditable Selection API 클리어",
                "기존 내용이 여전히 남아있음",
                issue_ref="C-1/C-2",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(
            LEVEL,
            "type_contenteditable Selection API 클리어",
            f"예외: {e}",
            issue_ref="C-1/C-2",
        )

    # 10. type_contenteditable 텍스트 입력 성공
    try:
        cdp.type_contenteditable("#editor", "안녕하세요 테스트")
        time.sleep(0.3)
        ok = cdp._verify_input_content("#editor", "안녕하세요 테스트")
        if ok:
            reporter.ok(LEVEL, "type_contenteditable 텍스트 입력 성공", "'안녕하세요 테스트' 입력 확인됨")
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "type_contenteditable 텍스트 입력 성공",
                "_verify_input_content 반환 False",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "type_contenteditable 텍스트 입력 성공", f"예외: {e}")

    # 11. _verify_input_content selector 기반 [D-1/D-2]
    try:
        cdp.type_contenteditable("#editor", "테스트ABC")
        time.sleep(0.3)
        ok_true = cdp._verify_input_content("#editor", "테스트ABC")
        ok_false = cdp._verify_input_content("#editor", "전혀없는텍스트xyz")
        if ok_true and not ok_false:
            reporter.ok(
                LEVEL,
                "_verify_input_content selector 기반",
                "True/False 판정 정확 확인됨",
                issue_ref="D-1/D-2",
            )
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "_verify_input_content selector 기반",
                f"ok_true={ok_true}, ok_false={ok_false} (기대: True, False)",
                issue_ref="D-1/D-2",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(
            LEVEL,
            "_verify_input_content selector 기반",
            f"예외: {e}",
            issue_ref="D-1/D-2",
        )

    # 12. type_contenteditable L1_INPUT_REPEAT 반복 사이클
    try:
        success_count = 0
        for i in range(L1_INPUT_REPEAT):
            unique_text = f"반복테스트_{i+1}_사이클"
            try:
                cdp.type_contenteditable("#editor", unique_text)
                time.sleep(0.3)
                if cdp._verify_input_content("#editor", unique_text):
                    success_count += 1
                # 다음 사이클을 위해 내용 클리어
                cdp.execute_js("document.getElementById('editor').innerText = '';")
                time.sleep(0.1)
            except Exception:
                pass

        total = L1_INPUT_REPEAT
        detail = f"{success_count}/{total} 성공"
        if success_count == total:
            reporter.ok(
                LEVEL,
                f"type_contenteditable {L1_INPUT_REPEAT}회 반복 사이클",
                detail,
            )
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                f"type_contenteditable {L1_INPUT_REPEAT}회 반복 사이클",
                detail,
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(
            LEVEL,
            f"type_contenteditable {L1_INPUT_REPEAT}회 반복 사이클",
            f"예외: {e}",
        )

    # 13. activateTarget 재확인 [B-2]
    try:
        cdp.type_contenteditable("#editor", "B2검증텍스트")
        time.sleep(0.3)
        ok = cdp._verify_input_content("#editor", "B2검증텍스트")
        if ok:
            reporter.ok(
                LEVEL,
                "activateTarget 재확인",
                "type_contenteditable 성공 — activateTarget 내부 호출 정상",
                issue_ref="B-2",
            )
        else:
            ss = cdp.screenshot()
            reporter.fail(
                LEVEL,
                "activateTarget 재확인",
                "type_contenteditable 입력 내용 불일치",
                issue_ref="B-2",
                screenshot=ss,
            )
    except Exception as e:
        reporter.fail(LEVEL, "activateTarget 재확인", f"예외: {e}", issue_ref="B-2")

    # ------------------------------------------------------------------
    # [정리]
    # ------------------------------------------------------------------
    try:
        cdp.disconnect()
    except Exception:
        pass
