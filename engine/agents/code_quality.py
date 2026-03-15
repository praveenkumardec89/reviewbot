"""
Code Quality Agent — error handling, null safety, complexity, naming, dead code.
Runs when code files are present.
"""

from .base import BaseAgent, COMMENT_FORMAT

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb",
    ".php", ".cs", ".cpp", ".c", ".rs", ".kt", ".swift", ".scala",
}


class CodeQualityAgent(BaseAgent):
    name = "code_quality"
    description = "error handling, null/undefined safety, cyclomatic complexity, naming clarity, dead code"
    emoji = "✨"

    def should_run(self, pr_context: dict) -> tuple[bool, str]:
        if pr_context.get("has_code"):
            return True, f"code files present ({', '.join(sorted(pr_context['extensions'] & CODE_EXTENSIONS))})"
        return False, "no code files in this PR"

    def build_system_prompt(self, knowledge: dict) -> str:
        boosted_rules = [
            r for r in knowledge.get("rules", [])
            if r.get("boosted") and r.get("category") in ("bug", "style", "quality")
        ]
        boosted_text = ""
        if boosted_rules:
            boosted_text = "\nHIGH-CONFIDENCE RULES (team has validated these):\n"
            for r in boosted_rules:
                boosted_text += f"  - {r['description']}\n"

        return f"""You are a senior software engineer performing a code quality review.

YOUR SOLE FOCUS — flag only these categories:

1. ERROR HANDLING:
   - Exceptions/errors swallowed silently (empty catch blocks, `_ = err` in Go)
   - Missing error propagation (function can fail but caller never checks)
   - Panic/crash paths not handled (unchecked array access, type assertions without ok)
   - Resource leaks (file handles, DB connections, goroutines not closed/cancelled)

2. NULL / UNDEFINED SAFETY:
   - Dereferencing pointers/references without nil/null checks
   - Optional/nullable values used without unwrapping safely
   - Missing guard clauses before accessing nested properties

3. COMPLEXITY & READABILITY:
   - Functions over 60 lines — suggest decomposition
   - Nesting depth > 4 levels — suggest early returns or extraction
   - Complex boolean conditions without named variables
   - Magic numbers/strings without named constants

4. NAMING & CLARITY:
   - Misleading names (function does more or less than its name says)
   - Single-letter variables outside of loop counters or standard conventions
   - Inconsistent naming with the rest of the file

5. DEAD CODE & CORRECTNESS:
   - Variables assigned but never used
   - Unreachable code after return/throw
   - Off-by-one errors in loops/slices
   - Incorrect operator precedence
{boosted_text}
IMPORTANT:
- Do NOT flag security issues (that's another agent's job)
- Focus on bugs that will actually manifest, not hypothetical ones
- For complexity issues, suggest the specific refactoring, not just "refactor this"
- Include praise comments for genuinely clean, well-structured code

{COMMENT_FORMAT}"""
