"""하네스 결과 기록 및 리포트 — CheckResult, HarnessReporter"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from harness.config import ARTIFACTS_DIR


# ---------------------------------------------------------------------------
# 검증 결과 단위
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    level: str           # "L0", "L1", "L2", "L3"
    name: str            # 테스트 이름
    passed: bool
    detail: str = ""
    issue_ref: str = ""  # 관련 이슈 태그 (예: "A-1", "E-1/E-2")
    screenshot_path: str = ""


# ---------------------------------------------------------------------------
# 리포터
# ---------------------------------------------------------------------------
class HarnessReporter:
    """모든 레이어의 CheckResult를 수집하고 리포트를 출력/저장한다."""

    def __init__(self):
        self._results: list[CheckResult] = []
        self._start = datetime.now()

    # -- 결과 기록 --

    def record(self, result: CheckResult) -> None:
        self._results.append(result)
        icon = "✅" if result.passed else "❌"
        ref = f" [{result.issue_ref}]" if result.issue_ref else ""
        detail_str = f": {result.detail}" if result.detail else ""
        print(f"  {icon} [{result.level}] {result.name}{ref}{detail_str}")

    def ok(self, level: str, name: str, detail: str = "", issue_ref: str = "") -> None:
        """PASS 결과 기록"""
        self.record(CheckResult(
            level=level, name=name, passed=True,
            detail=detail, issue_ref=issue_ref,
        ))

    def fail(
        self,
        level: str,
        name: str,
        detail: str = "",
        issue_ref: str = "",
        screenshot: Optional[bytes] = None,
    ) -> None:
        """FAIL 결과 기록. screenshot이 주어지면 artifacts/에 저장."""
        ss_path = ""
        if screenshot:
            ss_path = self.save_screenshot(name, screenshot)
        self.record(CheckResult(
            level=level, name=name, passed=False,
            detail=detail, issue_ref=issue_ref,
            screenshot_path=ss_path,
        ))

    # -- 스크린샷 --

    def save_screenshot(self, name: str, data: bytes) -> str:
        ts = datetime.now().strftime("%H%M%S")
        safe = name.replace(" ", "_").replace("/", "_").replace("[", "").replace("]", "")
        path = ARTIFACTS_DIR / f"{ts}_{safe}.png"
        path.write_bytes(data)
        return str(path)

    # -- 쿼리 --

    def level_passed(self, level: str) -> bool:
        """해당 레이어의 모든 체크가 통과했는지 반환. 결과가 없으면 True."""
        results = [r for r in self._results if r.level == level]
        return all(r.passed for r in results) if results else True

    def all_passed(self) -> bool:
        return all(r.passed for r in self._results)

    def failed_results(self) -> list[CheckResult]:
        return [r for r in self._results if not r.passed]

    # -- 출력 --

    def print_summary(self) -> None:
        elapsed = (datetime.now() - self._start).total_seconds()
        passed = sum(1 for r in self._results if r.passed)
        total = len(self._results)
        failed = total - passed

        print("\n" + "=" * 65)
        print(f"  하네스 검증 결과 — 소요 {elapsed:.1f}s")
        print("=" * 65)

        for level in ("L0", "L1", "L2", "L3"):
            level_results = [r for r in self._results if r.level == level]
            if not level_results:
                continue
            lp = sum(1 for r in level_results if r.passed)
            lf = len(level_results) - lp
            status = "PASS" if lf == 0 else f"FAIL ({lf}건)"
            print(f"\n  ▶ {level}  [{status}]  {lp}/{len(level_results)} 통과")
            for r in level_results:
                icon = "  ✅" if r.passed else "  ❌"
                ref = f" [{r.issue_ref}]" if r.issue_ref else ""
                detail_str = f": {r.detail}" if r.detail else ""
                print(f"    {icon} {r.name}{ref}{detail_str}")
                if r.screenshot_path:
                    print(f"         📸 {r.screenshot_path}")

        bar = "✅ ALL PASS" if failed == 0 else f"❌ {failed}건 실패"
        print(f"\n  총계: {passed}/{total}  {bar}")
        print("=" * 65)

    # -- JSON 저장 --

    def to_json(self) -> str:
        return json.dumps(
            {
                "timestamp": self._start.isoformat(),
                "all_passed": self.all_passed(),
                "elapsed_s": (datetime.now() - self._start).total_seconds(),
                "results": [
                    {
                        "level": r.level,
                        "name": r.name,
                        "passed": r.passed,
                        "detail": r.detail,
                        "issue_ref": r.issue_ref,
                        "screenshot_path": r.screenshot_path,
                    }
                    for r in self._results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    def save_report(self) -> str:
        ts = self._start.strftime("%Y%m%d_%H%M%S")
        path = ARTIFACTS_DIR / f"report_{ts}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        print(f"\n  📄 리포트 저장: {path}")
        return str(path)
