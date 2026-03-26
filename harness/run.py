#!/usr/bin/env python3
"""하네스 CLI 실행기

사용법:
  python -m harness.run           # 모든 레이어 실행 (L0 → L1 → L2 → L3)
  python -m harness.run --level L0  # L0만 실행
  python -m harness.run --level L1  # L0 + L1 실행
  python -m harness.run --level L2  # L0 + L1 + L2 실행
  python -m harness.run --level L3  # L0 + L1 + L2 + L3 실행
  python -m harness.run --skip-e2e  # L3 건너뛰기
  python -m harness.run --no-stop-on-fail  # 레이어 실패해도 다음 레이어 계속 실행
"""
import sys
import argparse
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.reporter import HarnessReporter


def _parse_args() -> argparse.Namespace:
    """CLI 인수를 파싱하여 Namespace 반환."""
    parser = argparse.ArgumentParser(
        description="Discussion Automation 하네스 검증 실행기",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python -m harness.run                # 전체 실행 (L0~L3)\n"
            "  python -m harness.run --level L1     # L0 + L1만 실행\n"
            "  python -m harness.run --skip-e2e     # L3 건너뛰고 L0~L2 실행\n"
            "  python -m harness.run --no-stop-on-fail  # 실패해도 계속 실행\n"
        ),
    )
    parser.add_argument(
        "--level",
        choices=["L0", "L1", "L2", "L3"],
        default="L3",
        help="실행할 최대 레이어 (기본값: L3 = 전체 실행)",
    )
    parser.add_argument(
        "--skip-e2e",
        action="store_true",
        default=False,
        help="L3 E2E Smoke 테스트를 건너뜀",
    )
    parser.add_argument(
        "--no-stop-on-fail",
        action="store_true",
        default=False,
        help="레이어 실패 시에도 다음 레이어를 계속 실행",
    )
    return parser.parse_args()


def main() -> None:
    """하네스 CLI 메인 진입점 — L0→L1→L2→L3 순서로 레이어를 실행한다."""
    args = _parse_args()
    reporter = HarnessReporter()

    # ── L0: 환경 사전검사 ────────────────────────────────────────────────────
    print("\n[L0] 환경 사전검사...")
    from harness import preflight
    preflight.run(reporter)

    if not reporter.level_passed("L0") and not args.no_stop_on_fail:
        print("  L0 실패 — 하네스 중단")
        reporter.print_summary()
        reporter.save_report()
        sys.exit(1)

    if args.level == "L0":
        reporter.print_summary()
        reporter.save_report()
        sys.exit(0 if reporter.all_passed() else 1)

    # ── L1: CDP 단위 검증 ────────────────────────────────────────────────────
    print("\n[L1] CDP 단위 검증...")
    from harness import test_cdp_unit
    test_cdp_unit.run(reporter)

    if not reporter.level_passed("L1") and not args.no_stop_on_fail:
        print("  L1 실패 — L2/L3 건너뜀")
        reporter.print_summary()
        reporter.save_report()
        sys.exit(1)

    if args.level == "L1":
        reporter.print_summary()
        reporter.save_report()
        sys.exit(0 if reporter.all_passed() else 1)

    # ── L2: 통합 검증 ────────────────────────────────────────────────────────
    print("\n[L2] 통합 검증 (실 Gemini/Claude 탭)...")
    from harness import test_integration
    test_integration.run(reporter)

    if not reporter.level_passed("L2") and not args.no_stop_on_fail:
        print("  L2 실패")
        # L2 실패해도 경고만 하고 계속 (E2E는 독립적으로 가치 있음)

    if args.level == "L2":
        reporter.print_summary()
        reporter.save_report()
        sys.exit(0 if reporter.all_passed() else 1)

    # ── L3: E2E Smoke ────────────────────────────────────────────────────────
    if not args.skip_e2e:
        print("\n[L3] E2E Smoke 토론...")
        from harness import test_e2e
        test_e2e.run(reporter)
    else:
        print("\n[L3] 건너뜀 (--skip-e2e)")

    reporter.print_summary()
    reporter.save_report()
    sys.exit(0 if reporter.all_passed() else 1)


if __name__ == "__main__":
    main()
