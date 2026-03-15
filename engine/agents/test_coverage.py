"""
Test Coverage Agent — missing tests, test quality, coverage gaps.
Runs when non-test code files are present.
"""

from pathlib import Path
from .base import BaseAgent, COMMENT_FORMAT

TEST_INDICATORS = {"test", "spec", "_test.", ".test.", "mock", "fixture", "factory"}


def is_test_file(filename: str) -> bool:
    lower = filename.lower()
    return any(ind in lower for ind in TEST_INDICATORS)


class TestCoverageAgent(BaseAgent):
    name = "test_coverage"
    description = "missing tests for new/changed logic, test quality, edge cases not covered"
    emoji = "🧪"

    def should_run(self, pr_context: dict) -> tuple[bool, str]:
        if not pr_context.get("has_code"):
            return False, "no code files"

        file_names = pr_context.get("file_names", [])
        non_test_files = [f for f in file_names if not is_test_file(f)]
        test_files = [f for f in file_names if is_test_file(f)]

        if not non_test_files:
            return False, "PR only touches test files"

        # If there's production code but no test files at all
        if not test_files and non_test_files:
            return True, f"{len(non_test_files)} production files changed with no test files in PR"

        # If production code additions significantly outnumber test additions
        additions = pr_context.get("total_additions", 0)
        if additions > 40:
            return True, f"{additions} lines added — checking test coverage"

        return False, "small change with tests present — skipping coverage review"

    def build_system_prompt(self, knowledge: dict) -> str:
        config = knowledge.get("config", {})
        review_tests = config.get("review", {}).get("review_tests", True)
        if not review_tests:
            return "Return []"  # Disabled by config

        return f"""You are a senior engineer with a strong TDD background, reviewing test coverage.

YOUR SOLE FOCUS — flag only these categories:

1. MISSING TESTS FOR NEW LOGIC:
   - New public functions/methods with no corresponding test
   - New branches (if/switch cases) not covered by existing tests
   - New error paths that aren't tested
   - When flagging: suggest the test case structure (describe what to test, not just "add tests")

2. MISSING EDGE CASES:
   - Boundary conditions not tested (empty list, zero, max int, empty string)
   - Error/exception paths not covered
   - Concurrent access scenarios for shared state
   - Null/nil inputs for functions that don't explicitly guard against them

3. TEST QUALITY ISSUES:
   - Tests that assert on implementation details rather than behavior (brittle tests)
   - Tests with no assertions or trivial assertions (`assert true`)
   - Tests that don't actually test the function name suggests (misleading test names)
   - Over-mocking: mocking so much that the test doesn't verify real behavior
   - Setup so complex the test is harder to understand than the code

4. TEST STRUCTURE:
   - Missing test for the happy path (just edge cases tested)
   - Tests that test multiple unrelated things (should be split)
   - Test data that's too specific and will break on minor changes

IMPORTANT:
- Only flag missing tests for code that was ADDED or CHANGED in this PR (not pre-existing gaps)
- Suggest the specific test scenario/case, not just "add a test"
- Praise good test patterns: clear naming, good coverage, readable setup
- Don't flag missing tests for private/internal helpers that are tested indirectly
- Respect the testing framework already in use

{COMMENT_FORMAT}"""
