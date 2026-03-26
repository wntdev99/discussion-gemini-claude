"""L2 통합 검증 — 실제 Chrome에 연결된 Gemini/Claude 탭을 대상으로 AITabController 동작 검증.

AI 응답을 생성하지 않음 (메시지 전송 없음).
브라우저에 실제 AI 서비스 탭이 열려 있어야 합니다.
"""
import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from chrome_cdp_controller import CDPClient
from discussion_app import (
    AITabController,
    SelectorConfig,
    default_gemini_selectors,
    default_claude_selectors,
)
from harness.config import (
    CDP_PORT,
    L2_GEMINI_DOMAIN,
    L2_CLAUDE_DOMAIN,
    L2_TEST_INPUT_TEXT,
    L2_SELECTOR_TIMEOUT,
)
from harness.reporter import HarnessReporter

LEVEL = "L2"


def _find_tab(cdp: CDPClient, domain: str) -> dict | None:
    """탭 목록에서 domain을 포함하는 첫 번째 탭 반환"""
    tabs = cdp.list_tabs()
    for t in tabs:
        if domain in t.get("url", ""):
            return t
    return None


def _run_ai_checks(
    reporter: HarnessReporter,
    cdp_base: CDPClient,
    ai_name: str,
    domain: str,
    make_selectors,
    check_offset: int,
) -> None:
    """Gemini 또는 Claude 탭에 대한 공통 L2 체크 8종을 실행한다.

    Args:
        reporter: 결과 수집 리포터
        cdp_base: 탭 목록 조회용 CDPClient (포트만 보유, 연결 없음)
        ai_name: "Gemini" 또는 "Claude" (표시용)
        domain: 탭 탐지용 도메인 문자열
        make_selectors: 기본 SelectorConfig 생성 함수
        check_offset: 체크 번호 오프셋 (Gemini=1, Claude=9)
    """

    # ── 체크 1(9): 탭 탐지 ──────────────────────────────────────────────────
    check_num = check_offset
    tab_info = None
    try:
        tab_info = _find_tab(cdp_base, domain)
        if tab_info is not None:
            reporter.ok(LEVEL, f"{ai_name} 탭 탐지",
                        detail=f"url={tab_info.get('url', '')[:60]}")
        else:
            reporter.fail(LEVEL, f"{ai_name} 탭 탐지",
                          detail=f"domain={domain} 탭을 찾을 수 없음")
            # 이하 체크 전체를 스킵
            for name in [
                f"{ai_name} 탭 WebSocket 연결",
                f"{ai_name} 로그인 상태 확인",
                f"{ai_name} 셀렉터 자동 탐지",
                f"{ai_name} 스냅샷 베이스라인",
                f"{ai_name} 입력창 텍스트 입력",
                f"{ai_name} _validate_tab_url",
                f"{ai_name} read_last_response",
            ]:
                reporter.fail(LEVEL, name,
                              detail=f"{ai_name} 탭 없어 스킵", issue_ref="")
            return
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} 탭 탐지",
                      detail=f"예외: {type(e).__name__}: {e}")
        for name in [
            f"{ai_name} 탭 WebSocket 연결",
            f"{ai_name} 로그인 상태 확인",
            f"{ai_name} 셀렉터 자동 탐지",
            f"{ai_name} 스냅샷 베이스라인",
            f"{ai_name} 입력창 텍스트 입력",
            f"{ai_name} _validate_tab_url",
            f"{ai_name} read_last_response",
        ]:
            reporter.fail(LEVEL, name,
                          detail=f"{ai_name} 탭 탐지 예외로 스킵", issue_ref="")
        return

    ws_url = tab_info.get("webSocketDebuggerUrl", "")
    tab_url = tab_info.get("url", "")
    ctrl: AITabController | None = None

    # ── 체크 2(10): WebSocket 연결 ───────────────────────────────────────────
    try:
        ctrl = AITabController(ai_name.lower(), CDP_PORT, make_selectors())
        ctrl.connect_to_tab(ws_url, tab_url)
        if ctrl.connected:
            reporter.ok(LEVEL, f"{ai_name} 탭 WebSocket 연결",
                        detail=f"ws_url={ws_url[:60]}",
                        issue_ref="B-1")
        else:
            reporter.fail(LEVEL, f"{ai_name} 탭 WebSocket 연결",
                          detail="connected=False",
                          issue_ref="B-1")
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} 탭 WebSocket 연결",
                      detail=f"예외: {type(e).__name__}: {e}",
                      issue_ref="B-1")
        for name in [
            f"{ai_name} 로그인 상태 확인",
            f"{ai_name} 셀렉터 자동 탐지",
            f"{ai_name} 스냅샷 베이스라인",
            f"{ai_name} 입력창 텍스트 입력",
            f"{ai_name} _validate_tab_url",
            f"{ai_name} read_last_response",
        ]:
            reporter.fail(LEVEL, name,
                          detail="WebSocket 연결 실패로 스킵", issue_ref="")
        return

    # ── 체크 3(11): 로그인 상태 확인 ────────────────────────────────────────
    try:
        logged_in, login_msg = ctrl.check_login_status()
        if logged_in:
            reporter.ok(LEVEL, f"{ai_name} 로그인 상태 확인",
                        detail=login_msg)
        else:
            reporter.fail(LEVEL, f"{ai_name} 로그인 상태 확인",
                          detail=login_msg)
        # 로그인 실패해도 탭이 연결되어 있으면 나머지 체크 계속 진행
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} 로그인 상태 확인",
                      detail=f"예외: {type(e).__name__}: {e}")

    # ── 체크 4(12): 셀렉터 자동 탐지 ────────────────────────────────────────
    try:
        config, msg = ctrl.auto_detect_selectors()
        if config is not None and config.input_selector:
            reporter.ok(LEVEL, f"{ai_name} 셀렉터 자동 탐지",
                        detail=msg, issue_ref="F-2")
        else:
            reporter.fail(LEVEL, f"{ai_name} 셀렉터 자동 탐지",
                          detail=msg, issue_ref="F-2")
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} 셀렉터 자동 탐지",
                      detail=f"예외: {type(e).__name__}: {e}",
                      issue_ref="F-2")

    # ── 체크 5(13): 스냅샷 베이스라인 ───────────────────────────────────────
    try:
        baseline = ctrl.snapshot_baseline()
        if isinstance(baseline, str):
            reporter.ok(LEVEL, f"{ai_name} 스냅샷 베이스라인",
                        detail=f"baseline 길이: {len(baseline)}자")
        else:
            reporter.fail(LEVEL, f"{ai_name} 스냅샷 베이스라인",
                          detail=f"반환 타입 오류: {type(baseline)}")
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} 스냅샷 베이스라인",
                      detail=f"예외: {type(e).__name__}: {e}")

    # ── 체크 6(14): 입력창 텍스트 입력 ──────────────────────────────────────
    try:
        ctrl.cdp.type_contenteditable(
            ctrl.selectors.input_selector, L2_TEST_INPUT_TEXT
        )
        verified = ctrl.cdp._verify_input_content(
            ctrl.selectors.input_selector, L2_TEST_INPUT_TEXT
        )
        if verified:
            reporter.ok(LEVEL, f"{ai_name} 입력창 텍스트 입력",
                        detail=f"입력 검증 성공: '{L2_TEST_INPUT_TEXT[:20]}...'",
                        issue_ref="C-1/C-2/D-1/D-2")
        else:
            reporter.fail(LEVEL, f"{ai_name} 입력창 텍스트 입력",
                          detail="입력 내용 검증 실패 (_verify_input_content=False)",
                          issue_ref="C-1/C-2/D-1/D-2",
                          screenshot=ctrl.cdp.screenshot())
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} 입력창 텍스트 입력",
                      detail=f"예외: {type(e).__name__}: {e}",
                      issue_ref="C-1/C-2/D-1/D-2",
                      screenshot=ctrl.cdp.screenshot() if ctrl and ctrl.connected else None)
    finally:
        # 입력창 내용 클리어 (전송하지 않음)
        try:
            ctrl.cdp.execute_js(
                f"const e=document.querySelector({json.dumps(ctrl.selectors.input_selector)});"
                f"if(e){{e.innerHTML='';e.dispatchEvent(new Event('input',{{bubbles:true}}))}}"
            )
        except Exception:
            pass

    # ── 체크 7(15): _validate_tab_url ───────────────────────────────────────
    try:
        tab_url_valid = bool(ctrl._tab_url)

        resp = ctrl.cdp.execute_js("window.location.href")
        current_url = resp.get("result", {}).get("result", {}).get("value", "")
        url_matches_domain = domain in current_url

        if tab_url_valid and url_matches_domain:
            reporter.ok(LEVEL, f"{ai_name} _validate_tab_url",
                        detail=f"_tab_url 설정됨, 현재 URL: {current_url[:60]}",
                        issue_ref="H-2")
        else:
            detail_parts = []
            if not tab_url_valid:
                detail_parts.append("_tab_url 비어있음")
            if not url_matches_domain:
                detail_parts.append(f"현재 URL에 도메인 없음: {current_url[:60]}")
            reporter.fail(LEVEL, f"{ai_name} _validate_tab_url",
                          detail="; ".join(detail_parts),
                          issue_ref="H-2")
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} _validate_tab_url",
                      detail=f"예외: {type(e).__name__}: {e}",
                      issue_ref="H-2")

    # ── 체크 8(16): read_last_response ──────────────────────────────────────
    try:
        text = ctrl.read_last_response()
        if isinstance(text, str):
            reporter.ok(LEVEL, f"{ai_name} read_last_response",
                        detail=f"응답 길이: {len(text)}자",
                        issue_ref="F-1")
        else:
            reporter.fail(LEVEL, f"{ai_name} read_last_response",
                          detail=f"반환 타입 오류: {type(text)}",
                          issue_ref="F-1")
    except Exception as e:
        reporter.fail(LEVEL, f"{ai_name} read_last_response",
                      detail=f"예외: {type(e).__name__}: {e}",
                      issue_ref="F-1",
                      screenshot=ctrl.cdp.screenshot() if ctrl and ctrl.connected else None)


def run(reporter: HarnessReporter) -> None:
    """L2 통합 검증 실행 — Gemini 탭 8종 + Claude 탭 8종 = 총 16개 체크."""
    cdp_base = CDPClient(CDP_PORT)

    # ── Gemini 탭 검증 (체크 1~8) ────────────────────────────────────────────
    _run_ai_checks(
        reporter=reporter,
        cdp_base=cdp_base,
        ai_name="Gemini",
        domain=L2_GEMINI_DOMAIN,
        make_selectors=default_gemini_selectors,
        check_offset=1,
    )

    # ── Claude 탭 검증 (체크 9~16) ───────────────────────────────────────────
    _run_ai_checks(
        reporter=reporter,
        cdp_base=cdp_base,
        ai_name="Claude",
        domain=L2_CLAUDE_DOMAIN,
        make_selectors=default_claude_selectors,
        check_offset=9,
    )
