"""하네스 공통 설정 — 모든 테스트 레이어가 공유하는 상수 및 경로"""
from pathlib import Path
import os

# ---------------------------------------------------------------------------
# CDP 연결 설정
# ---------------------------------------------------------------------------
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
CHROME_BINARY = os.environ.get("CHROME_BINARY", "/usr/bin/google-chrome")

# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
HARNESS_DIR = Path(__file__).parent
PROJECT_DIR = HARNESS_DIR.parent          # chrome_cdp_controller.py, discussion_app.py 위치
ARTIFACTS_DIR = HARNESS_DIR / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 타임아웃 (초)
# ---------------------------------------------------------------------------
PREFLIGHT_HTTP_TIMEOUT = 3    # L0: CDP /json HTTP 응답
L1_COMMAND_TIMEOUT = 10       # L1: CDP 명령 응답
L2_SELECTOR_TIMEOUT = 15      # L2: 셀렉터 탐지
L3_RESPONSE_TIMEOUT = 120     # L3: AI 응답 대기
L3_STABLE_DURATION = 4.0      # L3: 텍스트 안정화 판정 시간

# ---------------------------------------------------------------------------
# L1 더미 페이지
# contenteditable div, click isTrusted 검증용 버튼, Enter 이벤트 검증용 div 포함
# ---------------------------------------------------------------------------
L1_DUMMY_HTML = (
    "data:text/html,"
    "<html><body style='font-family:sans-serif;padding:20px'>"
    "<h3>Harness L1 Dummy Page</h3>"
    "<div id='editor' contenteditable='true' "
    "     style='width:500px;height:80px;border:1px solid #ccc;padding:6px'></div>"
    "<br>"
    "<button id='btn' style='margin-top:8px'>Click Me</button>"
    "<p id='out' style='color:green'></p>"
    "</body></html>"
)

# L1 입력 사이클 반복 횟수 (입력 → 검증 → 클리어)
L1_INPUT_REPEAT = 5

# ---------------------------------------------------------------------------
# L2 실 서비스 도메인
# ---------------------------------------------------------------------------
L2_GEMINI_DOMAIN = "gemini.google.com"
L2_CLAUDE_DOMAIN = "claude.ai"

# L2: 테스트용 입력 텍스트 (실제 전송 안 함)
L2_TEST_INPUT_TEXT = "안녕하세요 하네스 입력 검증 테스트"

# ---------------------------------------------------------------------------
# L3 E2E Smoke
# ---------------------------------------------------------------------------
L3_SMOKE_TEXT = (
    "안녕하세요. 이것은 자동화 테스트입니다. "
    "딱 한 문장으로만 '확인되었습니다'라고 답해주세요."
)
L3_SMOKE_TURNS = 1  # 1턴(Gemini 전송 → 응답 수신)만 검증
