"""
Base class for all ReviewCrew specialized agents.
Each agent has a focused domain, a routing check, and a specialized system prompt.
"""

import os
import json
import time
from pathlib import Path
from anthropic import Anthropic, RateLimitError

def _format_project_context(ctx: dict, files_context: list) -> str:
    """
    Format the project context into a concise section injected into every agent's
    user message. Covers tech stack, topology, module graph of changed files,
    and test coverage gaps.
    """
    if not ctx:
        return ""

    lines = ["## PROJECT CONTEXT\n"]

    # ── Tech Stack ──
    ts = ctx.get("tech_stack", {})
    if ts.get("language") and ts["language"] != "unknown":
        stack_parts = [ts["language"]]
        if ts.get("framework") and ts["framework"] not in ("unknown", ts["language"]):
            stack_parts.append(ts["framework"])
        if ts.get("build_tool") and ts["build_tool"] != "unknown":
            stack_parts.append(ts["build_tool"])
        if ts.get("test_framework") and ts["test_framework"] != "unknown":
            stack_parts.append(f"tests:{ts['test_framework']}")
        lines.append(f"**Stack:** {' · '.join(stack_parts)}")

    # ── Service Topology ──
    topo = ctx.get("service_topology", {})
    if topo.get("databases"):
        lines.append(f"**Databases:** {', '.join(topo['databases'][:5])}")
    if topo.get("message_queues"):
        lines.append(f"**Message queues:** {', '.join(topo['message_queues'][:5])}")
    if topo.get("caches"):
        lines.append(f"**Caches:** {', '.join(topo['caches'][:3])}")
    if topo.get("external_apis"):
        lines.append(f"**External APIs called:** {', '.join(topo['external_apis'][:5])}")

    # ── DB Schema ──
    db = ctx.get("db_schema", {})
    if db.get("orm") and db["orm"] != "unknown":
        lines.append(f"**ORM:** {db['orm']}")
    if db.get("tables_detected"):
        lines.append(f"**DB tables:** {', '.join(db['tables_detected'][:10])}")
    if db.get("migration_files"):
        lines.append(f"**Migration files:** {len(db['migration_files'])} files")

    # ── Security-sensitive dependencies ──
    deps = ctx.get("dependencies", {})
    if deps.get("security_relevant"):
        lines.append(f"**Security-sensitive deps:** {', '.join(deps['security_relevant'][:8])}")

    # ── API surface ──
    api = ctx.get("api_contracts", {})
    if api.get("endpoint_count"):
        lines.append(f"**API endpoints in repo:** {api['endpoint_count']}")
    if api.get("openapi_files"):
        lines.append(f"**OpenAPI specs:** {', '.join(api['openapi_files'][:3])}")
    if api.get("proto_files"):
        lines.append(f"**Proto files:** {', '.join(api['proto_files'][:3])}")

    # ── Module graph for changed files ──
    module_graph = ctx.get("module_graph", {})
    test_map = ctx.get("test_coverage_map", {})

    changed_paths = [f["filename"] for f in files_context]
    if module_graph:
        lines.append("\n**CHANGED FILE ANALYSIS:**")
        for path in changed_paths:
            info = module_graph.get(path, {})
            if not info:
                continue
            layer = info.get("layer", "unknown")
            upstream = info.get("upstream", [])
            downstream = info.get("downstream", [])
            blast = info.get("blast_radius", 0)
            tests = test_map.get(path, [])

            file_lines = [f"\n`{path}` (layer: **{layer}**)"]

            if upstream:
                file_lines.append(f"  - Imports: {', '.join(f'`{u}`' for u in upstream[:5])}"
                                   + (" ..." if len(upstream) > 5 else ""))
            if downstream:
                file_lines.append(f"  - Imported by ({blast} file{'s' if blast != 1 else ''}): "
                                   + ', '.join(f'`{d}`' for d in downstream[:5])
                                   + (" ..." if blast > 5 else ""))
                if blast >= 5:
                    file_lines.append(f"  - ⚠️  HIGH BLAST RADIUS: {blast} files depend on this — "
                                       "interface changes will cascade")
            else:
                file_lines.append("  - No downstream dependents found (leaf module)")

            if tests:
                file_lines.append(f"  - Test files: {', '.join(f'`{t}`' for t in tests)}")
            else:
                file_lines.append("  - ⚠️  NO TEST COVERAGE FOUND for this file")

            lines.extend(file_lines)

    # ── Directory purpose map (filtered to changed dirs) ──
    dir_map = ctx.get("directory_map", {})
    if dir_map and changed_paths:
        changed_dirs = {str(Path(p).parent) for p in changed_paths}
        relevant_dirs = {d: purpose for d, purpose in dir_map.items()
                         if any(d.startswith(cd) or cd.startswith(d)
                                for cd in changed_dirs)}
        if relevant_dirs:
            lines.append("\n**DIRECTORY PURPOSES (changed paths):**")
            for d, purpose in list(relevant_dirs.items())[:6]:
                lines.append(f"  `{d}/` → {purpose}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


COMMENT_FORMAT = """
RESPONSE FORMAT — return a JSON array only. Each item:
{
  "file": "path/to/file.ext",
  "line": <line number in diff>,
  "severity": "critical|high|medium|low|praise",
  "category": "<your agent's category>",
  "comment": "Specific, actionable finding. Reference exact variable/function names. Suggest a fix.",
  "suggested_fix": "optional: corrected code snippet",
  "rule_id": "rule id or descriptive slug"
}

RULES:
- Be specific — name the exact variable, function, or line
- Always suggest a fix, not just a problem
- Do NOT repeat the same finding type more than twice
- Return [] if nothing significant found
- ONLY output valid JSON — no markdown, no prose
"""

# Agents are staggered to avoid hitting the free-tier 10K token/min rate limit
# when all 6 fire simultaneously. Each agent waits before its first API call.
AGENT_STAGGER_SECONDS = {
    "security":       0,
    "code_quality":   0,
    "architecture":   0,
    "simplification": 0,
    "test_coverage":  12,
    "performance":    24,
}


class BaseAgent:
    name: str = "base"
    description: str = ""
    emoji: str = "🔍"

    def should_run(self, pr_context: dict) -> tuple[bool, str]:
        """Return (should_run, reason). Fast rule-based check — no API call."""
        raise NotImplementedError

    def build_system_prompt(self, knowledge: dict) -> str:
        raise NotImplementedError

    def review(self, diff: str, files_context: list, knowledge: dict) -> list[dict]:
        # Stagger agents to avoid simultaneous rate-limit hits on free tier
        delay = AGENT_STAGGER_SECONDS.get(self.name, 0)
        if delay:
            print(f"[{self.name}] Waiting {delay}s (rate limit stagger)...")
            time.sleep(delay)

        # Anthropic() reads ANTHROPIC_API_KEY from env automatically — don't pass
        # it explicitly so it's resolved at call time, not module import time.
        client = Anthropic()
        model = knowledge.get("config", {}).get("model", "claude-sonnet-4-20250514")

        pr_number = os.environ.get("PR_NUMBER", "")
        pr_title  = os.environ.get("PR_TITLE", "")
        pr_author = os.environ.get("PR_AUTHOR", "")
        pr_body   = os.environ.get("PR_BODY", "")

        project_context = knowledge.get("project_context", {})
        context_section = _format_project_context(project_context, files_context)

        user_message = (
            f"PR #{pr_number}: {pr_title}\n"
            f"Author: {pr_author}\n"
            f"Description: {pr_body or '(no description)'}\n\n"
            f"{context_section}\n"
            f"CHANGED FILES:\n{json.dumps(files_context, indent=2)}\n\n"
            f"DIFF:\n```\n{diff[:35000]}\n```\n\n"
            f"Focus exclusively on: {self.description}\n"
            f"Return only a JSON array of comments."
        )

        # Retry up to 3 times on rate limit with exponential backoff
        response = None
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=2048,
                    system=self.build_system_prompt(knowledge),
                    messages=[{"role": "user", "content": user_message}],
                )
                break
            except RateLimitError:
                wait = 30 * (attempt + 1)
                if attempt < 2:
                    print(f"[{self.name}] Rate limited — retrying in {wait}s (attempt {attempt+1}/3)")
                    time.sleep(wait)
                else:
                    print(f"[{self.name}] Rate limited after 3 attempts — skipping")
                    return []

        if response is None:
            return []

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        try:
            comments = json.loads(text)
            for c in comments:
                c["agent"] = self.name
            return comments
        except json.JSONDecodeError:
            print(f"[{self.name}] Failed to parse response: {text[:100]}")
            return []
