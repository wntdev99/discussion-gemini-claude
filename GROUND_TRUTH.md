# Discussion Automation App — Ground Truth

> **이 문서는 변경 불가 계약서다.**
>
> - 코드가 이 문서와 충돌하면 **코드가 틀린 것이다.** 이 문서가 틀린 것이 아니다.
> - 기능이 변하지 않는 한 이 문서를 수정해서는 안 된다.
> - 하네스의 모든 검증 체크는 이 문서의 각 계약을 기준으로 PASS/FAIL을 판정한다.
> - 이슈 수정, 리팩터링, 코드 정리 등 어떠한 작업도 이 문서에 명시된 계약을 깨뜨릴 수 없다.

---

## 개정 규칙

| 변경 허용 조건 | 변경 금지 조건 |
|----------------|----------------|
| Gemini/Claude의 입력 메커니즘이 근본적으로 바뀐 경우 | 버그 수정 |
| Chrome CDP 프로토콜 버전이 변경된 경우 | 성능 개선 |
| 프로젝트 핵심 기능(토론 자동화) 자체가 변경된 경우 | 코드 리팩터링 |
| 이 문서에 명시된 해결책이 더 나은 해결책으로 교체되는 경우 (Commander 승인 필수) | 셀렉터 갱신 |

> 개정 시 반드시 이전 계약의 삭제 이유와 새 계약의 근거를 명시해야 한다.

---

## 1. isTrusted 계약

### GT-01: 모든 사용자 액션은 CDP Input.* 계열로 전송해야 한다

**규칙**

```
클릭   → Input.dispatchMouseEvent (mousePressed + mouseReleased)
키 입력 → Input.insertText 또는 Input.dispatchKeyEvent
Enter  → Input.dispatchKeyEvent (keyDown + keyUp, key="Enter", code="Enter")
```

**금지 패턴** — 아래 코드가 사용자 액션(클릭, 키 입력, 전송)에 사용되면 즉시 버그로 판정한다

```javascript
// ❌ FORBIDDEN
element.click()
element.dispatchEvent(new MouseEvent('click', ...))
element.dispatchEvent(new KeyboardEvent('keydown', ...))
```

**근거**: JS로 생성한 이벤트는 `isTrusted: false`다. Gemini/Claude는 보안 정책으로 `isTrusted: false` 이벤트를 거부한다. CDP `Input.*` 명령만 `isTrusted: true`를 보장한다.

**검증 방법**: 더미 HTML에 `addEventListener('click', e => { dataset.trusted = e.isTrusted })`를 주입한 뒤 CDP click 실행 → `dataset.trusted === '1'` 이어야 PASS.

---

## 2. 탭 활성화 계약

### GT-02: connect_tab() 호출 시 반드시 Target.activateTarget을 호출해야 한다

**규칙**

```python
def connect_tab(ws_url):
    # MUST: WebSocket 연결 직후 Target.activateTarget 호출
    # MUST: _active_target_id에 targetId 저장
    self._active_target_id = ws_url.rstrip("/").split("/")[-1]
    self.send_command("Target.activateTarget", {"targetId": self._active_target_id})
```

**근거**: `Input.*` 명령은 Chrome 내부적으로 현재 활성 탭을 기준으로 동작한다. 탭 활성화 없이 `Input.insertText`를 호출하면 엉뚱한 탭에 텍스트가 입력된다.

**금지 패턴**

```python
# ❌ FORBIDDEN: activateTarget 없이 WebSocket만 연결
self._ws = websocket.create_connection(ws_url)
# 여기서 바로 Input.* 사용 → 활성 탭이 다른 탭일 수 있음
```

**검증 방법**: `connect_tab()` 호출 후 `send_command("Target.activateTarget", ...)` 응답에 `"error"` 키가 없어야 PASS.

---

### GT-03: type_contenteditable() 내부에서 Input.insertText 전 activateTarget을 재확인해야 한다

**규칙**

```python
def type_contenteditable(selector, text):
    # ... focus + clear ...
    # MUST: Input.insertText 직전 activateTarget 재호출
    if self._active_target_id:
        self.send_command("Target.activateTarget", {"targetId": self._active_target_id})
    self.send_command("Input.insertText", {"text": text})
```

**근거**: 멀티탭 환경에서 focus 작업 사이에 다른 탭이 활성화될 수 있다. 입력 직전 재확인으로 입력 탭이 보장되어야 한다.

**검증 방법**: `type_contenteditable()` 성공 후 `_verify_input_content(selector, text)` 가 `True`여야 PASS.

---

## 3. WebSocket 수신 루프 계약

### GT-04: send_command()의 수신 루프에서 WebSocketTimeoutException 발생 시 continue해야 한다 (break 금지)

**규칙**

```python
while time.time() < deadline:
    try:
        raw = self._ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == msg_id:
            return resp
        continue  # MUST: 이벤트 메시지는 무시하고 계속 대기
    except websocket.WebSocketTimeoutException:
        continue  # MUST: 타임아웃은 데드라인까지 재시도 (break 금지)
```

**금지 패턴**

```python
# ❌ FORBIDDEN
except websocket.WebSocketTimeoutException:
    break  # 응답을 받지 못한 채 루프를 탈출 → 다음 명령의 ID 매칭 오염
```

**근거**: `WebSocketTimeoutException`은 이 recv()가 타임아웃된 것이지, 서버가 응답하지 않는다는 의미가 아니다. break하면 해당 명령의 응답이 소켓 버퍼에 남아 다음 명령의 응답과 뒤섞인다.

### GT-05: send_command()의 수신 루프에서 id가 없는 메시지(CDP 비동기 이벤트)는 무시하고 계속 대기해야 한다

**규칙**

```python
resp = json.loads(raw)
if resp.get("id") == msg_id:
    return resp
# MUST: id가 없는 메시지(이벤트)는 그냥 continue
continue
```

**근거**: CDP는 명령 응답(`id` 있음)과 비동기 이벤트(`id` 없음)를 같은 WebSocket으로 전송한다. 이벤트를 응답으로 오인하면 잘못된 결과를 반환하거나 무한 대기 상태가 된다.

**검증 방법**: `execute_js("1+1")` 응답의 `result.result.value` 가 정확히 `2`여야 PASS.

---

## 4. contenteditable 클리어 계약

### GT-06: contenteditable 요소의 기존 내용 삭제는 Selection API + CDP Delete 키 순서로 수행해야 한다

**규칙**

```
Step 1: JS → el.focus()
Step 2: JS → document.createRange().selectNodeContents(el)
             window.getSelection().addRange(range)
             (전체 선택 상태)
Step 3: CDP → Input.dispatchKeyEvent(type="keyDown", key="Delete")
Step 4: CDP → Input.dispatchKeyEvent(type="keyUp", key="Delete")
Step 5: 클리어 검증 JS → (el.innerText || el.textContent).trim() === ''
Step 6: (검증 실패 시) fallback: execCommand('selectAll') + execCommand('delete')
```

**금지 패턴**

```javascript
// ❌ FORBIDDEN: React Virtual DOM과 직접 충돌 발생
element.innerHTML = ''
element.textContent = ''
element.innerText = ''
```

**근거**: `innerHTML = ''` 직접 조작은 React의 Virtual DOM 상태와 실제 DOM을 불일치시킨다. React는 이후 상태 업데이트에서 DOM을 덮어쓰거나 이벤트를 올바르게 처리하지 못한다. Selection API + Delete 키는 실제 사용자 입력을 모사하므로 React 이벤트 시스템과 호환된다.

**검증 방법**:
1. 더미 편집기에 "기존 내용" 삽입
2. `type_contenteditable()` 호출
3. `el.innerText.includes('기존 내용')` 이 `false`여야 PASS

---

## 5. 입력 검증 계약

### GT-07: _verify_input_content()는 반드시 selector 파라미터로 요소를 조회해야 한다 (document.activeElement 금지)

**규칙**

```python
def _verify_input_content(selector: str, expected_text: str = "") -> bool:
    # MUST: selector로 요소 직접 조회
    js = f"document.querySelector({json.dumps(selector)})"
    # MUST NOT: document.activeElement 사용 금지
```

**금지 패턴**

```javascript
// ❌ FORBIDDEN
const el = document.activeElement;  // 포커스가 옮겨졌다면 엉뚱한 요소
```

**근거**: 클리어/입력 과정에서 포커스가 의도치 않게 이동할 수 있다. `activeElement`는 항상 입력 대상 요소를 가리킨다고 보장할 수 없다.

### GT-08: _verify_input_content()는 반드시 expected_text 포함 여부로 판정해야 한다 (길이 > 0 판정 금지)

**규칙**

```python
# MUST: expected_text[:30] 이 실제 내용에 포함되어 있는지 확인
snippet = expected_text[:30]
return content.includes(snippet)  # True/False
```

**금지 패턴**

```python
# ❌ FORBIDDEN: 길이만 확인하면 이전 잔존 내용도 PASS로 판정됨
return len(content) > 0
```

**근거**: 클리어가 실패하고 이전 입력이 남아있어도 길이 > 0 이면 PASS가 된다. expected_text 포함 여부만이 "원하는 텍스트가 실제로 입력됐는지"를 보장한다.

**검증 방법**:
- `_verify_input_content(selector, "테스트ABC")` → editor에 "테스트ABC" 입력 후 `True`
- `_verify_input_content(selector, "절대없는텍스트xyz")` → `False`
- 두 조건 모두 만족해야 PASS

---

## 6. 응답 텍스트 읽기 계약

### GT-09: read_last_response()는 querySelectorAll 결과 전체 요소의 텍스트를 결합해야 한다 (마지막 요소만 반환 금지)

**규칙**

```javascript
// MUST: 전체 요소 텍스트 결합
return Array.from(document.querySelectorAll(selector))
    .map(e => (e.innerText || e.textContent || '').trim())
    .filter(Boolean)
    .join('\n');
```

**금지 패턴**

```javascript
// ❌ FORBIDDEN: 마지막 요소만 반환
const els = document.querySelectorAll(selector);
return els[els.length - 1].innerText;
// ❌ FORBIDDEN: querySelector (첫 번째만)
return document.querySelector(selector).innerText;
```

**근거**: AI의 응답은 스트리밍 중 여러 DOM 블록으로 분할되어 생성된다. 마지막 블록만 읽으면 응답의 첫 부분을 잃어버려 완성된 응답을 얻을 수 없다.

---

### GT-10: Claude 응답 셀렉터는 스트리밍 중과 완료 후 양쪽 상태를 모두 매칭해야 한다

**규칙**

```python
# MUST: 완료 상태(.font-claude-message)를 우선으로, 스트리밍 상태도 폴백으로 포함
response_selector = (
    '.font-claude-message .markdown-content, '
    '[data-is-streaming] .markdown-content'
)
```

**금지 패턴**

```python
# ❌ FORBIDDEN: 스트리밍 중 셀렉터만 있으면 완료 후 매칭 실패
response_selector = '[data-is-streaming] .markdown-content'
```

**근거**: 스트리밍 완료 후 `[data-is-streaming]` 속성이 제거되면 셀렉터가 아무 요소도 찾지 못한다. 완료 상태 셀렉터를 우선 배치해야 한다.

---

## 7. 응답 완료 판정 계약

### GT-11: wait_for_response()는 단일 루프로 구현해야 한다 (스트리밍 시작 대기 루프와 완료 판정 루프의 분리 금지)

**규칙**

```python
def wait_for_response(timeout, poll_interval, stable_duration):
    start = time.time()
    last_text = self._last_response    # baseline
    stable_since = None
    response_started = False

    while time.time() - start < timeout:   # MUST: 단일 루프
        current = self.read_last_response()
        streaming = self.is_streaming()

        if not response_started:
            if current != self._last_response or streaming:
                response_started = True  # 응답 시작 감지

        if response_started:
            if current != last_text:
                last_text = current
                stable_since = None       # 텍스트 변경 → stable 타이머 리셋
            else:
                if stable_since is None:
                    stable_since = time.time()

            # MUST: 아래 4가지 조건이 모두 충족되어야 완료 판정
            if (stable_since is not None
                    and time.time() - stable_since >= stable_duration
                    and not streaming       # Stop 버튼 사라짐
                    and current             # 내용 비어있지 않음
                    and current != self._last_response):  # baseline과 다름
                self._last_response = current
                return True, current

        time.sleep(poll_interval)
```

**금지 패턴**

```python
# ❌ FORBIDDEN: 두 루프로 분리
# 루프 1: 스트리밍 시작 대기 (Stop 버튼 나타날 때까지)
# 루프 2: 완료 판정 (Stop 버튼 사라질 때까지)
# → 루프 1이 15초 제한을 초과하면 루프 2에 진입하지 못함
```

**근거**: 두 루프로 분리하면 AI가 응답을 시작하기 전 첫 번째 루프의 타임아웃(예: 15초)이 초과될 경우 응답을 전혀 읽지 못하고 오류 처리된다. 단일 루프는 이 경쟁 조건을 제거한다.

### GT-12: 완료 판정의 4가지 조건은 모두 동시에 충족되어야 한다 (AND 조건)

| 조건 | 검증 방법 | 의미 |
|------|-----------|------|
| `stable_since is not None` | stable 타이머 시작됨 | 텍스트가 한 번 이상 안정됨 |
| `time.time() - stable_since >= stable_duration` | 안정 시간 유지 | 텍스트 변경이 stable_duration초 동안 없음 |
| `not streaming` | Stop 버튼 없음 | AI가 실제로 생성을 완료함 |
| `current and current != self._last_response` | 새 내용 존재 | baseline과 다른 실제 응답이 있음 |

**근거**: 각 조건은 하나씩 떼어내면 오탐(False Positive)을 만든다.
- `stable` 만으로는: 응답이 시작도 안 했는데 baseline과 동일한 상태로 "stable"이 됨
- `not streaming` 만으로는: 스트리밍이 시작되기 전 상태에서도 Stop 버튼이 없어 즉시 완료 판정됨
- `current != baseline` 만으로는: 스트리밍 중간 미완성 텍스트를 완성 응답으로 반환함

---

## 8. 상태 관리 계약

### GT-13: disconnect() 호출 시 _msg_id를 반드시 0으로 리셋해야 한다

**규칙**

```python
def disconnect(self):
    if self._ws:
        try: self._ws.close()
        except Exception: pass
        self._ws = None
    self._msg_id = 0  # MUST: 재연결 후 메시지 ID를 1부터 재시작
```

**금지 패턴**

```python
# ❌ FORBIDDEN: _msg_id 리셋 없이 disconnect
def disconnect(self):
    if self._ws:
        self._ws.close()
        self._ws = None
    # _msg_id가 이전 값으로 남아있음 → 재연결 후 ID가 이어짐
```

**근거**: 재연결 후 `_msg_id`가 이전 세션의 값을 그대로 사용하면, 이전 세션에서 미처 받지 못한 응답 메시지가 새 세션의 요청 ID와 충돌해 잘못된 응답을 반환할 수 있다.

**검증 방법**: `disconnect()` 직후 `cdp._msg_id == 0` 이어야 PASS.

---

### GT-14: on_start() 호출 시 토론 시작 전 반드시 탭 URL 변경을 검사해야 한다

**규칙**

```
on_start() 실행 순서:
  MUST Step 1: _validate_tab_url(gemini_ctrl) 호출
  MUST Step 2: _validate_tab_url(claude_ctrl) 호출
  MUST Step 3: URL 변경 감지 시 auto_detect_selectors() 호출
  MUST Step 4: 재탐지 실패 시 사용자에게 QMessageBox 확인 요청
  Step 5: 토론 시작
```

**금지 패턴**

```python
# ❌ FORBIDDEN: URL 검증 없이 바로 토론 시작
def on_start(self):
    # URL 변경 여부 확인 없이 바로 worker thread 시작
    self._worker = DiscussionWorkerThread(...)
    self._worker.start()
```

**근거**: 탭 할당 이후 사용자가 다른 페이지로 이동했을 수 있다. 이전에 탐지된 셀렉터는 다른 페이지에서 유효하지 않아 입력 실패 또는 잘못된 요소에 입력이 발생한다.

---

## 9. 기본 셀렉터 계약

> 아래 셀렉터 값은 Gemini/Claude UI가 변경되지 않는 한 고정된 기본값이다.
> UI가 변경되면 이 섹션을 개정하고 harness/config.py의 해당 값도 함께 업데이트해야 한다.

### Gemini 기본 셀렉터 (gemini.google.com)

| 용도 | 셀렉터 |
|------|--------|
| 입력창 | `div.ql-editor[contenteditable="true"]` |
| 전송 버튼 | `button[aria-label="Send message"]` |
| 응답 영역 | `.model-response-text` |
| Stop 버튼 | `button[aria-label="Stop"]` |

### Claude 기본 셀렉터 (claude.ai)

| 용도 | 셀렉터 |
|------|--------|
| 입력창 | `div[contenteditable="true"].ProseMirror` |
| 전송 버튼 | `button[aria-label="Send Message"]` |
| 응답 영역 | `.font-claude-message .markdown-content, [data-is-streaming] .markdown-content` |
| Stop 버튼 | `button[aria-label="Stop Response"]` |

---

## 10. 회귀 금지 목록

> 아래 항목들은 한 번 수정된 후 절대 이전 상태로 돌아가서는 안 된다.
> 리팩터링이나 최적화 중 실수로 되돌아가는 것을 방지하기 위한 목록이다.

| ID | 파일 | 함수 | 금지 패턴 | 대응 계약 |
|----|------|------|-----------|-----------|
| A-1 | chrome_cdp_controller.py | `send_command` | `WebSocketTimeoutException: break` | GT-04 |
| A-2 | chrome_cdp_controller.py | `send_command` | 이벤트 메시지를 응답으로 처리 | GT-05 |
| B-1 | chrome_cdp_controller.py | `connect_tab` | `Target.activateTarget` 없이 WebSocket 연결만 | GT-02 |
| B-2 | chrome_cdp_controller.py | `type_contenteditable` | `Input.insertText` 전 `activateTarget` 미호출 | GT-03 |
| C-1 | chrome_cdp_controller.py | `type_contenteditable` | `innerHTML = ''` 직접 조작 | GT-06 |
| C-2 | chrome_cdp_controller.py | `type_contenteditable` | 클리어 후 검증 없음 | GT-06 |
| D-1 | chrome_cdp_controller.py | `_verify_input_content` | `document.activeElement` 기반 조회 | GT-07 |
| D-2 | chrome_cdp_controller.py | `_verify_input_content` | `len(content) > 0` 판정 | GT-08 |
| E-1 | chrome_cdp_controller.py | `click` | JS `.click()` 사용 | GT-01 |
| E-2 | chrome_cdp_controller.py | `press_enter` | JS `dispatchEvent` 사용 | GT-01 |
| F-1 | discussion_app.py | `read_last_response` | `querySelectorAll` 마지막 요소만 반환 | GT-09 |
| F-2 | discussion_app.py | `default_claude_selectors` | 스트리밍 중 셀렉터만 사용 | GT-10 |
| G-1 | discussion_app.py | `wait_for_response` | 스트리밍 시작 루프 + 완료 판정 루프 분리 | GT-11 |
| G-2 | discussion_app.py | `wait_for_response` | 4가지 완료 조건 중 일부 생략 | GT-12 |
| H-1 | chrome_cdp_controller.py | `disconnect` | `_msg_id` 리셋 없음 | GT-13 |
| H-2 | discussion_app.py | `on_start` | URL 변경 감지 없이 토론 시작 | GT-14 |

---

## 11. 하네스-계약 매핑

> 하네스의 각 체크가 어떤 GT 계약을 검증하는지 추적한다.
> 하네스를 수정할 때 이 매핑이 유지되어야 한다.

| 하네스 레이어 | 체크 이름 | 검증 계약 |
|---------------|-----------|-----------|
| L1 | send_command 기본 응답 | GT-04, GT-05 |
| L1 | Target.activateTarget | GT-02 |
| L1 | disconnect 후 _msg_id 리셋 | GT-13 |
| L1 | click isTrusted | GT-01 |
| L1 | press_enter isTrusted | GT-01 |
| L1 | type_contenteditable Selection API 클리어 | GT-06 |
| L1 | type_contenteditable 텍스트 입력 성공 | GT-03, GT-06 |
| L1 | _verify_input_content selector 기반 | GT-07, GT-08 |
| L1 | type_contenteditable N회 반복 사이클 | GT-06, GT-07, GT-08 |
| L1 | activateTarget 재확인 | GT-03 |
| L2 | {AI} 탭 WebSocket 연결 | GT-02 |
| L2 | {AI} 셀렉터 자동 탐지 | GT-10 (Claude) |
| L2 | {AI} 입력창 텍스트 입력 | GT-06, GT-07, GT-08 |
| L2 | {AI} _validate_tab_url | GT-14 |
| L2 | {AI} read_last_response | GT-09 |
| L3 | Gemini E2E 메시지 전송 | GT-01, GT-02, GT-03, GT-06, GT-07, GT-08 |
| L3 | Gemini E2E 응답 완료 대기 | GT-11, GT-12 |

---

## 12. 판정 기준 요약

```
이 문서에 정의된 계약 중 하나라도 위반된 코드가 존재하면
해당 코드는 버그다. 예외 없음.

하네스가 FAIL을 출력하면 코드가 이 문서와 충돌한다는 의미다.
이 문서를 수정하는 것이 아니라 코드를 수정해야 한다.

"지금은 동작하는 것 같다"는 이유로 계약 위반을 허용하지 않는다.
Gemini/Claude 보안 정책은 언제든 강화될 수 있으며,
이 계약들은 그 강화에 대응하기 위해 존재한다.
```
