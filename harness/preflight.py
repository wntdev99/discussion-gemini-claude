"""L0 환경 사전검사 — 브라우저 없이도 실행 가능한 기본 환경 검증"""
import sys
import os
import socket
import json
from urllib.request import urlopen
from urllib.error import URLError

from harness.config import CDP_PORT, CHROME_BINARY, PREFLIGHT_HTTP_TIMEOUT
from harness.reporter import HarnessReporter

LEVEL = "L0"


def run(reporter: HarnessReporter) -> None:
    """L0 환경 사전검사 체크를 순서대로 수행한다."""

    # ------------------------------------------------------------------
    # 1. Python 버전 확인
    # ------------------------------------------------------------------
    try:
        vi = sys.version_info
        detail = f"Python {vi.major}.{vi.minor}.{vi.micro}"
        if vi >= (3, 10):
            reporter.ok(LEVEL, "Python 버전", detail)
        else:
            reporter.fail(LEVEL, "Python 버전", f"{detail} — 3.10 이상 필요")
    except Exception as e:
        reporter.fail(LEVEL, "Python 버전", f"예외: {e}")

    # ------------------------------------------------------------------
    # 2. websocket-client 패키지
    # ------------------------------------------------------------------
    try:
        import websocket  # noqa: F401
        reporter.ok(LEVEL, "websocket-client 패키지", "import websocket 성공")
    except ImportError as e:
        reporter.fail(LEVEL, "websocket-client 패키지", f"import 실패: {e}")
    except Exception as e:
        reporter.fail(LEVEL, "websocket-client 패키지", f"예외: {e}")

    # ------------------------------------------------------------------
    # 3. PyQt5 패키지
    # ------------------------------------------------------------------
    try:
        import PyQt5  # noqa: F401
        reporter.ok(LEVEL, "PyQt5 패키지", "import PyQt5 성공")
    except ImportError as e:
        reporter.fail(LEVEL, "PyQt5 패키지", f"import 실패: {e}")
    except Exception as e:
        reporter.fail(LEVEL, "PyQt5 패키지", f"예외: {e}")

    # ------------------------------------------------------------------
    # 4. Chrome 바이너리 존재 확인
    # ------------------------------------------------------------------
    try:
        if os.path.isfile(CHROME_BINARY):
            reporter.ok(LEVEL, "Chrome 바이너리 존재", f"경로 확인: {CHROME_BINARY}")
        else:
            reporter.fail(LEVEL, "Chrome 바이너리 존재", f"파일 없음: {CHROME_BINARY}")
    except Exception as e:
        reporter.fail(LEVEL, "Chrome 바이너리 존재", f"예외: {e}")

    # ------------------------------------------------------------------
    # 5. CDP 포트 응답 확인 (socket connect)
    # ------------------------------------------------------------------
    cdp_port_ok = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(PREFLIGHT_HTTP_TIMEOUT)
            result = s.connect_ex(("localhost", CDP_PORT))
        if result == 0:
            cdp_port_ok = True
            reporter.ok(LEVEL, "CDP 포트 응답", f"포트 {CDP_PORT} 응답 확인")
        else:
            reporter.fail(LEVEL, "CDP 포트 응답", f"포트 {CDP_PORT} 응답 없음 (connect_ex={result})")
    except Exception as e:
        reporter.fail(LEVEL, "CDP 포트 응답", f"예외: {e}")

    # CDP 포트 미응답 시 이후 HTTP/탭 체크는 스킵
    if not cdp_port_ok:
        reporter.fail(LEVEL, "CDP /json 엔드포인트", "CDP 포트 미응답으로 스킵")
        reporter.fail(LEVEL, "탭 목록 비어있지 않음", "CDP 포트 미응답으로 스킵")
        return

    # ------------------------------------------------------------------
    # 6. CDP /json 엔드포인트 확인
    # ------------------------------------------------------------------
    tabs = []
    json_ok = False
    try:
        resp = urlopen(
            f"http://localhost:{CDP_PORT}/json",
            timeout=PREFLIGHT_HTTP_TIMEOUT,
        )
        raw = resp.read()
        tabs = json.loads(raw)
        json_ok = True
        reporter.ok(LEVEL, "CDP /json 엔드포인트", f"JSON 파싱 성공 — 탭 {len(tabs)}개")
    except URLError as e:
        reporter.fail(LEVEL, "CDP /json 엔드포인트", f"HTTP 요청 실패: {e}")
    except json.JSONDecodeError as e:
        reporter.fail(LEVEL, "CDP /json 엔드포인트", f"JSON 파싱 실패: {e}")
    except Exception as e:
        reporter.fail(LEVEL, "CDP /json 엔드포인트", f"예외: {e}")

    # ------------------------------------------------------------------
    # 7. 탭 목록 비어있지 않음 (type=page 탭 1개 이상)
    # ------------------------------------------------------------------
    if not json_ok:
        reporter.fail(LEVEL, "탭 목록 비어있지 않음", "CDP /json 실패로 스킵")
        return

    try:
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if page_tabs:
            reporter.ok(
                LEVEL,
                "탭 목록 비어있지 않음",
                f"type=page 탭 {len(page_tabs)}개 확인",
            )
        else:
            reporter.fail(
                LEVEL,
                "탭 목록 비어있지 않음",
                f"type=page 탭 없음 (전체 탭 {len(tabs)}개)",
            )
    except Exception as e:
        reporter.fail(LEVEL, "탭 목록 비어있지 않음", f"예외: {e}")
