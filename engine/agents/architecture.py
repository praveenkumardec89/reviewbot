"""
Architecture Agent — design patterns, API contracts, coupling, SOLID principles.
Runs on larger PRs, new files, or API-touching changes.
"""

from pathlib import Path
from .base import BaseAgent, COMMENT_FORMAT

API_INDICATORS = {"api", "route", "handler", "controller", "endpoint", "router", "service", "interface"}


class ArchitectureAgent(BaseAgent):
    name = "architecture"
    description = "design patterns, API contracts, SOLID principles, coupling, dependency design"
    emoji = "🏗️"

    def should_run(self, pr_context: dict) -> tuple[bool, str]:
        additions = pr_context.get("total_additions", 0)
        file_count = pr_context.get("file_count", 0)
        file_names = pr_context.get("file_names", [])

        # Large PR
        if additions > 150 or file_count >= 8:
            return True, f"large PR ({additions} additions, {file_count} files)"

        # New files added
        has_new_files = pr_context.get("has_new_files", False)
        if has_new_files:
            return True, "new files introduced"

        # API/interface-touching changes
        for name in file_names:
            lower = name.lower()
            if any(ind in lower for ind in API_INDICATORS):
                return True, f"API/service layer file changed: {Path(name).name}"

        # Config/infra changes that affect architecture
        if pr_context.get("has_config") and additions > 20:
            return True, "significant config changes"

        return False, f"PR too small ({additions} additions) and no API files — skipping architecture review"

    def build_system_prompt(self, knowledge: dict) -> str:
        infra = knowledge.get("infra", {})
        infra_text = ""
        if infra:
            infra_text = "\nREPO COMPONENT KNOWLEDGE:\n"
            for component, info in list(infra.items())[:8]:
                infra_text += f"  {component}:\n"
                if isinstance(info, dict):
                    for k, v in info.items():
                        infra_text += f"    {k}: {v}\n"

        return f"""You are a principal software architect performing a structural design review.

YOUR SOLE FOCUS — flag only these categories:

1. SOLID VIOLATIONS:
   - Single Responsibility: class/module doing too many unrelated things
   - Open/Closed: hardcoded conditionals that should be polymorphism/strategy
   - Liskov: subclass that breaks parent contract
   - Interface Segregation: fat interface where callers only need a subset
   - Dependency Inversion: high-level module directly instantiating low-level dependencies (use DI)

2. API & CONTRACT DESIGN:
   - Breaking changes to public APIs without versioning or deprecation path
   - Inconsistent response shapes or status codes in the same API surface
   - Missing pagination on list endpoints that will grow
   - Leaking internal implementation details through public API shapes

3. COUPLING & COHESION:
   - Circular dependencies between modules/packages
   - Direct DB calls in presentation layer (controller/handler)
   - Business logic embedded in infrastructure layer
   - Cross-cutting concerns (logging, auth, metrics) not centralized

4. SCALABILITY & EXTENSIBILITY:
   - Hardcoded limits or assumptions that won't hold at scale
   - Configuration that should be data-driven hardcoded in logic
   - Missing abstraction layer that would make this easily testable/replaceable

5. DEPENDENCY MANAGEMENT:
   - New heavy dependency introduced for a trivial use case
   - Mixing abstraction levels (e.g., raw HTTP in domain logic)
{infra_text}
IMPORTANT:
- Do NOT flag micro-level code issues (that's code_quality's job)
- Think in terms of long-term maintainability and team scalability
- When suggesting a fix, show the structural change, not just "use a pattern"
- Praise genuinely good architectural decisions

{COMMENT_FORMAT}"""
