"""L3 E2E Smoke 검증 — 실제 AI 응답을 1턴 생성하여 전체 플로우를 검증한다.

send_message → wait_for_response 흐름을 실제 Gemini 탭에서 실행.
최대 L3_RESPONSE_TIMEOUT(120초)까지 응답을 대기할 수 있습니다.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from chrome_cdp_controller import CDPClient
from discussion_app import AITabController, default_gemini_selectors
from harness.config import (
    CDP_PORT,
    L2_GEMINI_DOMAIN,
    L3_SMOKE_TEXT,
    L3_RESPONSE_TIMEOUT,
    L3_STABLE_DURATION,
)
from harness.reporter import HarnessReporter

LEVEL = "L3"


def run(reporter: HarnessReporter) -> None:
    """L3 E2E Smoke 실행 — Gemini 탭에서 메시지 전송 및 응답 완료 대기 2개 체크."""
    cdp_base = CDPClient(CDP_PORT)

    # Gemini 탭 탐지
    tabs = cdp_base.list_tabs()
    gemini_tab = next(
        (t for t in tabs if L2_GEMINI_DOMAIN in t.get("url", "")), None
    )
    if not gemini_tab:
        reporter.fail(LEVEL, "Gemini E2E Smoke", "Gemini 탭 없음 — L3 스킵")
        return

    ws_url = gemini_tab.get("webSocketDebuggerUrl", "")
    tab_url = gemini_tab.get("url", "")

    gemini_ctrl: AITabController | None = None

    # ── 체크 1: Gemini E2E 메시지 전송 ──────────────────────────────────────
    try:
        gemini_ctrl = AITabController("gemini", CDP_PORT, default_gemini_selectors())
        gemini_ctrl.connect_to_tab(ws_url, tab_url)

        # 베이스라인 스냅샷 저장 (새 응답 판별 기준)
        gemini_ctrl.snapshot_baseline()

        ok, msg = gemini_ctrl.send_message(L3_SMOKE_TEXT)
        if ok:
            reporter.ok(LEVEL, "Gemini E2E 메시지 전송",
                        detail=msg,
                        issue_ref="A-1/A-2/B-1/B-2/C-1/C-2/D-1/D-2/E-1/E-2")
        else:
            reporter.fail(LEVEL, "Gemini E2E 메시지 전송",
                          detail=msg,
                          issue_ref="A-1/A-2/B-1/B-2/C-1/C-2/D-1/D-2/E-1/E-2",
                          screenshot=gemini_ctrl.cdp.screenshot())
            # 전송 실패 시 응답 대기 체크도 스킵
            reporter.fail(LEVEL, "Gemini E2E 응답 완료 대기",
                          detail="메시지 전송 실패로 스킵",
                          issue_ref="G-1/G-2")
            return
    except Exception as e:
        screenshot = None
        try:
            if gemini_ctrl and gemini_ctrl.connected:
                screenshot = gemini_ctrl.cdp.screenshot()
        except Exception:
            pass
        reporter.fail(LEVEL, "Gemini E2E 메시지 전송",
                      detail=f"예외: {type(e).__name__}: {e}",
                      issue_ref="A-1/A-2/B-1/B-2/C-1/C-2/D-1/D-2/E-1/E-2",
                      screenshot=screenshot)
        reporter.fail(LEVEL, "Gemini E2E 응답 완료 대기",
                      detail="메시지 전송 예외로 스킵",
                      issue_ref="G-1/G-2")
        return

    # ── 체크 2: Gemini E2E 응답 완료 대기 ───────────────────────────────────
    try:
        success, response_text = gemini_ctrl.wait_for_response(
            timeout=L3_RESPONSE_TIMEOUT,
            stable_duration=L3_STABLE_DURATION,
        )
        if success and response_text:
            reporter.ok(LEVEL, "Gemini E2E 응답 완료 대기",
                        detail=f"응답 {len(response_text)}자 수신",
                        issue_ref="G-1/G-2")
        else:
            detail = f"success={success}, 응답 길이={len(response_text)}자"
            reporter.fail(LEVEL, "Gemini E2E 응답 완료 대기",
                          detail=detail,
                          issue_ref="G-1/G-2",
                          screenshot=gemini_ctrl.cdp.screenshot())
    except Exception as e:
        screenshot = None
        try:
            if gemini_ctrl and gemini_ctrl.connected:
                screenshot = gemini_ctrl.cdp.screenshot()
        except Exception:
            pass
        reporter.fail(LEVEL, "Gemini E2E 응답 완료 대기",
                      detail=f"예외: {type(e).__name__}: {e}",
                      issue_ref="G-1/G-2",
                      screenshot=screenshot)
