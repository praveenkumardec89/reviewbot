"""
Base class for all ReviewBot specialized agents.
Each agent has a focused domain, a routing check, and a specialized system prompt.
"""

import os
import json
from anthropic import Anthropic

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
        # Anthropic() reads ANTHROPIC_API_KEY from env automatically — don't pass it
        # explicitly so it's resolved at call time, not module import time.
        client = Anthropic()
        model = knowledge.get("config", {}).get("model", "claude-sonnet-4-20250514")

        pr_number = os.environ.get("PR_NUMBER", "")
        pr_title = os.environ.get("PR_TITLE", "")
        pr_author = os.environ.get("PR_AUTHOR", "")
        pr_body = os.environ.get("PR_BODY", "")

        user_message = (
            f"PR #{pr_number}: {pr_title}\n"
            f"Author: {pr_author}\n"
            f"Description: {pr_body or '(no description)'}\n\n"
            f"CHANGED FILES:\n{json.dumps(files_context, indent=2)}\n\n"
            f"DIFF:\n```\n{diff[:40000]}\n```\n\n"
            f"Focus exclusively on: {self.description}\n"
            f"Return only a JSON array of comments."
        )

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.build_system_prompt(knowledge),
            messages=[{"role": "user", "content": user_message}],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        try:
            comments = json.loads(text)
            # Tag each comment with the agent that produced it
            for c in comments:
                c["agent"] = self.name
            return comments
        except json.JSONDecodeError:
            print(f"[{self.name}] Failed to parse response: {text[:100]}")
            return []
