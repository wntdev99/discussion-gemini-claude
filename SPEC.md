# Discussion Automation App — Technical Specification

> 이 문서는 프로젝트의 살아있는 기술 명세서입니다.
> 이슈 수정 에이전트는 자신이 담당한 이슈를 해결한 후 반드시 **"6. 수정 내역 로그"** 섹션을 갱신해야 합니다.

---

## 1. 프로젝트 개요

Chrome DevTools Protocol(CDP)을 통해 Gemini(gemini.google.com)와 Claude(claude.ai) 탭을 자동 제어하여 두 AI 간의 토론을 자동화하는 PyQt5 기반 데스크탑 애플리케이션.

- **chrome_cdp_controller.py** — CDP 저수준 제어 레이어 + 독립 GUI
- **discussion_app.py** — 토론 자동화 고수준 로직 + 메인 GUI

---

## 2. 컴포넌트 구조

### chrome_cdp_controller.py

| 클래스 | 역할 |
|--------|------|
| `ChromeProfileManager` | 로컬 Chrome 프로필 탐색, `--remote-debugging-port`로 Chrome 실행, 프로세스/포트 상태 확인 |
| `CDPClient` | WebSocket 기반 CDP 클라이언트. 탭 연결, 명령 전송/수신, 편의 메서드 제공 |
| `CDPWorkerThread` | GUI 블로킹 없이 CDP 명령을 백그라운드 실행하는 QThread 래퍼 |
| `MainWindow` | CDP 제어 독립 GUI (프로필 선택, 탭 목록, Click/Type/JS/Screenshot/DOM) |

### discussion_app.py

| 클래스 | 역할 |
|--------|------|
| `SelectorConfig` | AI 서비스별 CSS 셀렉터 묶음 (입력창, 전송 버튼, 응답 영역, Stop 버튼) |
| `AITabController` | CDPClient를 감싸는 고수준 컨트롤러. 메시지 전송, 응답 대기, 셀렉터 자동 탐지, 재연결 |
| `DiscussionWorkerThread` | 토론 루프를 백그라운드 QThread로 실행. 자동/반자동 모드, 일시정지/재개/중지 |
| `DiscussionApp` | 전체 GUI — 프로필 연결, 탭 할당, 토론 설정, 로그 표시, 결과 저장 |

---

## 3. CDP 동작 원리

```
Chrome (--remote-debugging-port=9222)
    │
    ├── HTTP GET /json  → 탭 목록 (type=page 필터)
    │
    └── WebSocket ws://localhost:9222/devtools/page/{targetId}
            │
            ├── 요청: {"id": N, "method": "...", "params": {...}}
            ├── 응답: {"id": N, "result": {...}}  ← id 매칭
            └── 이벤트: {"method": "...", "params": {...}}  ← id 없음 (비동기)
```

### 핵심 CDP 명령

| 명령 | 용도 |
|------|------|
| `Target.activateTarget` | 탭을 실제로 활성화(포커스). `Input.*` 계열은 활성 탭 기준 |
| `Input.insertText` | 활성 요소에 텍스트 삽입 (isTrusted: true) |
| `Input.dispatchKeyEvent` | 키보드 이벤트 전송 (isTrusted: true) |
| `Input.dispatchMouseEvent` | 마우스 이벤트 전송 (isTrusted: true) |
| `Runtime.evaluate` | JavaScript 실행 |
| `DOM.getBoxModel` | 요소의 레이아웃 박스(좌표) 조회 |
| `Page.captureScreenshot` | 스크린샷 캡처 |

### isTrusted 원칙

> **CDP `Input.*` 명령은 `isTrusted: true`를 보장합니다.**
> JS `dispatchEvent()` / `.click()`으로 생성한 이벤트는 `isTrusted: false`이며
> Claude/Gemini의 최신 보안 업데이트에서 거부될 수 있습니다.
> 모든 사용자 액션(클릭, 키 입력)은 반드시 CDP `Input.*` 계열을 사용해야 합니다.

---

## 4. 핵심 플로우

### 탭 연결 플로우
```
on_assign_tab()
  → CDPClient 생성 (port)
  → connect_tab(ws_url)
      → Target.activateTarget (탭 활성화)  ← B-1에서 추가
      → WebSocket 연결
  → snapshot_baseline() (기존 응답 스냅샷)
  → auto_detect_selectors() (셀렉터 자동 탐지)
  → check_login_status() (로그인 확인)
```

### 메시지 전송 플로우
```
send_message(text)
  → reconnect() (미연결 시)
  → type_contenteditable(selector, text)
      → Target.activateTarget (재확인)  ← B-2에서 추가
      → Selection API로 기존 내용 클리어  ← C-1에서 변경
      → 클리어 검증  ← C-2에서 추가
      → Step 1: Input.insertText
      → _verify_input_content(selector, text)  ← D-1/D-2에서 시그니처 변경
      → (실패) Step 2: Input.dispatchKeyEvent 글자별
      → (실패) Step 3: execCommand 폴백
  → click(send_selector)  ← E-1에서 CDP dispatchMouseEvent로 교체
  → (실패) press_enter()  ← E-2에서 CDP dispatchKeyEvent로 교체
```

### 응답 완료 판정 플로우
```
wait_for_response(timeout)
  → 단일 루프 (timeout까지)  ← G-1에서 통합
      → read_last_response()  ← F-1에서 전체 텍스트 결합으로 변경
          → querySelectorAll → Array.from → join('\n')
      → is_streaming() (Stop 버튼 존재 확인)
      → stable_duration 동안 텍스트 변경 없음 + 스트리밍 종료 → 완료
```

---

## 5. 이슈 카탈로그

### 그룹 A — send_command WebSocket 수신 루프
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| A-1 (#1) | chrome_cdp_controller.py | `send_command` | `TimeoutException: break` → `continue` |
| A-2 (#2) | chrome_cdp_controller.py | `send_command` | CDP 이벤트 메시지(`id` 없음) `continue` 처리 |

### 그룹 B — 탭 활성화
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| B-1 (#3) | chrome_cdp_controller.py | `connect_tab` | `Target.activateTarget` 호출 추가 |
| B-2 (#4) | chrome_cdp_controller.py | `type_contenteditable` | `Input.insertText` 전 활성화 재확인 |

### 그룹 C — contenteditable 클리어
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| C-1 (#5) | chrome_cdp_controller.py | `type_contenteditable` | `innerHTML=''` → Selection API + CDP Delete |
| C-2 (#6) | chrome_cdp_controller.py | `type_contenteditable` | 클리어 후 검증 JS 추가 |

### 그룹 D — 입력 검증
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| D-1 (#7) | chrome_cdp_controller.py | `_verify_input_content` | `activeElement` → `selector` 파라미터 기반 |
| D-2 (#8) | chrome_cdp_controller.py | `_verify_input_content` | 길이 > 0 → `expected_text` 포함 여부 비교 |

### 그룹 E — isTrusted 이벤트
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| E-1 (#9) | chrome_cdp_controller.py | `click` | JS `.click()` → `Input.dispatchMouseEvent` |
| E-2 (#10) | chrome_cdp_controller.py | `press_enter` | JS `dispatchEvent` → `Input.dispatchKeyEvent` |

### 그룹 F — 응답 텍스트 읽기
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| F-1 (#11) | discussion_app.py | `read_last_response` | `last` 요소만 → 전체 결합 |
| F-2 (#12) | discussion_app.py | `default_claude_selectors` | 스트리밍 완료 후 셀렉터 미매칭 수정 |

### 그룹 G — 응답 완료 판정
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| G-1 (#13) | discussion_app.py | `wait_for_response` | 두 루프 → 단일 루프 통합 |
| G-2 (#14) | discussion_app.py | `wait_for_response` | F-1/F-2 완료 후 검증 항목 |

### 그룹 H — 상태 관리
| 이슈 | 파일 | 함수 | 핵심 수정 |
|------|------|------|-----------|
| H-1 (#15) | chrome_cdp_controller.py | `disconnect` | `_msg_id = 0` 리셋 추가 |
| H-2 (#16) | discussion_app.py | `on_start` | 탭 URL 변경 감지 + 셀렉터 재탐지 |

---

## 6. 이슈 해결 의존성 순서

```
Wave 1 (병렬):   A-1+A-2,  H-1,  E-1+E-2,  F-1+F-2
                    ↓         ↓       ↓          ↓
Wave 2 (병렬):        B-1+B-2+C-1+C-2+D-1+D-2
                              ↓
Wave 3:                    G-1+G-2
                              ↓
Wave 4:                      H-2
```

---

## 7. 수정 내역 로그

> 각 에이전트는 담당 이슈 해결 후 아래 형식으로 추가합니다.

| 날짜 | 이슈 | 브랜치 | 수정 요약 | 상태 |
|------|------|--------|-----------|------|
| - | - | - | - | 대기 중 |
| 2026-03-26 | F-1 (#11) | fix/issue-11-12-response-reading | read_last_response: last 요소만→전체 Array.from+join 결합 | 완료 |
| 2026-03-26 | F-2 (#12) | fix/issue-11-12-response-reading | default_claude_selectors: response_selector 순서 변경으로 완료 후 매칭 보장 | 완료 |
| 2026-03-26 | H-1 (#15) | fix/issue-15-disconnect-msgid | disconnect() 시 _msg_id=0 리셋 추가 | 완료 |
| 2026-03-26 | E-1 (#9) | fix/issue-9-10-istrusted-events | click(): JS .click() → CDP Input.dispatchMouseEvent (isTrusted:true) | 완료 |
| 2026-03-26 | E-2 (#10) | fix/issue-9-10-istrusted-events | press_enter(): JS dispatchEvent → CDP Input.dispatchKeyEvent (isTrusted:true) | 완료 |
| 2026-03-26 | A-1 (#1) | fix/issue-1-2-send-command | send_command WebSocketTimeoutException: break→continue | 완료 |
| 2026-03-26 | A-2 (#2) | fix/issue-1-2-send-command | send_command 이벤트 메시지 explicit continue 추가 | 완료 |
| 2026-03-26 | B-1 (#3) | fix/issue-3-4-5-6-7-8-cdp-input | connect_tab: Target.activateTarget + _active_target_id 저장 | 완료 |
| 2026-03-26 | B-2 (#4) | fix/issue-3-4-5-6-7-8-cdp-input | type_contenteditable: Input.insertText 전 activateTarget 재확인 | 완료 |
| 2026-03-26 | C-1 (#5) | fix/issue-3-4-5-6-7-8-cdp-input | type_contenteditable: innerHTML='' → Selection API+CDP Delete 클리어 | 완료 |
| 2026-03-26 | C-2 (#6) | fix/issue-3-4-5-6-7-8-cdp-input | type_contenteditable: 클리어 후 검증 + execCommand 폴백 추가 | 완료 |
| 2026-03-26 | D-1 (#7) | fix/issue-3-4-5-6-7-8-cdp-input | _verify_input_content: activeElement→selector 파라미터 기반 검증 | 완료 |
| 2026-03-26 | D-2 (#8) | fix/issue-3-4-5-6-7-8-cdp-input | _verify_input_content: 길이>0→expected_text 포함 여부 비교 | 완료 |
| 2026-03-26 | G-1 (#13) | fix/issue-13-14-wait-for-response | wait_for_response: 두 루프→단일 루프 통합, 타이밍 경쟁 조건 해결 | 완료 |
| 2026-03-26 | G-2 (#14) | fix/issue-13-14-wait-for-response | G-1 구현+F-1 read_last_response 수정으로 stable_duration 전체 텍스트 기준 보장 | 완료 |
| 2026-03-26 | H-2 (#16) | fix/issue-16-onstart-url-validation | on_start: 탭 URL 변경 감지 + 셀렉터 자동 재탐지 (_validate_tab_url) | 완료 |
