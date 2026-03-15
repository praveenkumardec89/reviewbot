"""
ReviewBot — Core Review Engine
Reads knowledge store, analyzes PR diff, posts intelligent review comments.
"""

import os
import json
import yaml
import hashlib
from pathlib import Path
from anthropic import Anthropic

# ─── Configuration ────────────────────────────────────────
KNOWLEDGE_DIR = Path(".reviewbot")
RULES_FILE = KNOWLEDGE_DIR / "rules.yaml"
PATTERNS_FILE = KNOWLEDGE_DIR / "patterns.json"
SCORES_FILE = KNOWLEDGE_DIR / "scores.json"
INFRA_FILE = KNOWLEDGE_DIR / "infra.yaml"
CONFIG_FILE = KNOWLEDGE_DIR / "config.yaml"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
PR_NUMBER = os.environ["PR_NUMBER"]
PR_TITLE = os.environ.get("PR_TITLE", "")
PR_BODY = os.environ.get("PR_BODY", "")
PR_AUTHOR = os.environ.get("PR_AUTHOR", "")
BASE_SHA = os.environ.get("BASE_SHA", "")
HEAD_SHA = os.environ.get("HEAD_SHA", "")


def load_knowledge():
    """Load the entire knowledge store for context injection."""
    knowledge = {
        "rules": [],
        "patterns": {},
        "scores": {},
        "infra": {},
        "config": get_default_config(),
    }

    if RULES_FILE.exists():
        knowledge["rules"] = yaml.safe_load(RULES_FILE.read_text()) or []

    if PATTERNS_FILE.exists():
        knowledge["patterns"] = json.loads(PATTERNS_FILE.read_text())

    if SCORES_FILE.exists():
        knowledge["scores"] = json.loads(SCORES_FILE.read_text())

    if INFRA_FILE.exists():
        knowledge["infra"] = yaml.safe_load(INFRA_FILE.read_text()) or {}

    if CONFIG_FILE.exists():
        user_config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        knowledge["config"].update(user_config)

    return knowledge


def get_default_config():
    return {
        "model": "claude-sonnet-4-20250514",
        "review": {
            "auto_review": True,
            "severity_threshold": "low",
            "max_comments_per_pr": 15,
            "review_tests": True,
            "review_docs": False,
        },
        "learning": {
            "enabled": True,
            "min_feedback_samples": 5,
        },
    }


def get_effective_rules(knowledge):
    """Filter rules by their feedback scores — suppress low-scoring rules."""
    rules = knowledge["rules"]
    scores = knowledge["scores"]
    min_samples = knowledge["config"]["learning"].get("min_feedback_samples", 5)

    effective_rules = []
    suppressed_rules = []

    for rule in rules:
        rule_id = rule.get("id", "")
        score_data = scores.get(f"rule:{rule_id}", {})
        total_score = score_data.get("score", 0)
        sample_count = score_data.get("samples", 0)

        # Only suppress if we have enough samples AND score is very negative
        if sample_count >= min_samples and total_score < -10:
            suppressed_rules.append(rule_id)
            continue

        # Boost high-scoring rules by marking them
        if sample_count >= min_samples and total_score > 10:
            rule["_boosted"] = True

        effective_rules.append(rule)

    if suppressed_rules:
        print(f"[ReviewBot] Suppressed {len(suppressed_rules)} low-scoring rules: {suppressed_rules}")

    return effective_rules


def build_system_prompt(knowledge):
    """Build the review system prompt with all knowledge context."""
    effective_rules = get_effective_rules(knowledge)
    config = knowledge["config"]
    infra = knowledge["infra"]
    patterns = knowledge["patterns"]

    # Separate boosted vs normal rules
    boosted = [r for r in effective_rules if r.get("_boosted")]
    normal = [r for r in effective_rules if not r.get("_boosted")]

    rules_text = ""
    if boosted:
        rules_text += "HIGH-CONFIDENCE RULES (proven effective by team feedback):\n"
        for r in boosted:
            rules_text += f"  - [{r.get('severity', 'medium')}] {r.get('description', '')}\n"
            if r.get("example"):
                rules_text += f"    Example: {r['example']}\n"

    if normal:
        rules_text += "\nSTANDARD RULES:\n"
        for r in normal:
            rules_text += f"  - [{r.get('severity', 'medium')}] {r.get('description', '')}\n"
            if r.get("example"):
                rules_text += f"    Example: {r['example']}\n"

    # Build component/infra context
    infra_text = ""
    if infra:
        infra_text = "\nINFRASTRUCTURE & COMPONENT KNOWLEDGE:\n"
        for component, info in infra.items():
            infra_text += f"  {component}:\n"
            if isinstance(info, dict):
                for k, v in info.items():
                    infra_text += f"    {k}: {v}\n"
            else:
                infra_text += f"    {info}\n"

    # Build known patterns context
    patterns_text = ""
    if patterns.get("known_bad"):
        patterns_text += "\nKNOWN BAD PATTERNS (flag these):\n"
        for p in patterns["known_bad"][:20]:  # Cap at 20
            patterns_text += f"  - {p.get('pattern', '')}: {p.get('reason', '')}\n"

    if patterns.get("known_good"):
        patterns_text += "\nKNOWN GOOD PATTERNS (encourage these):\n"
        for p in patterns["known_good"][:10]:
            patterns_text += f"  - {p.get('pattern', '')}: {p.get('reason', '')}\n"

    severity_threshold = config["review"].get("severity_threshold", "low")

    return f"""You are ReviewBot, an AI code reviewer that learns and improves over time.
You are reviewing a pull request. Your review must be actionable, specific, and helpful.

REVIEW CONFIGURATION:
- Severity threshold: {severity_threshold} (only comment on issues at this level or above)
- Review tests: {config['review'].get('review_tests', True)}
- Max comments: {config['review'].get('max_comments_per_pr', 15)}

{rules_text}
{infra_text}
{patterns_text}

SEVERITY LEVELS (in order):
- critical: Security vulnerabilities, data loss risks, production outages
- high: Bugs, race conditions, missing error handling, breaking changes
- medium: Code quality, performance, maintainability issues
- low: Style, naming, minor suggestions
- praise: Positive feedback for good patterns (ALWAYS include 1-2 of these)

RESPONSE FORMAT:
Respond with a JSON array of review comments. Each comment must have:
{{
  "file": "path/to/file.ext",
  "line": <line_number_in_diff>,
  "severity": "critical|high|medium|low|praise",
  "category": "security|bug|performance|style|pattern|architecture|test|praise",
  "comment": "Your review comment (be specific, suggest fix)",
  "suggested_fix": "optional code suggestion",
  "rule_id": "id of the rule that triggered this (or 'learned' for pattern-based)"
}}

IMPORTANT GUIDELINES:
1. Be specific — reference exact lines and variables
2. Suggest fixes, don't just point out problems
3. Acknowledge good patterns with praise comments
4. Consider the component/infra context when reviewing
5. If a pattern matches a known bad pattern, flag it with higher confidence
6. If a pattern matches a known good pattern, praise it
7. Don't repeat the same type of comment more than 3 times
8. Focus on logic and correctness over style
9. Every comment you make will be scored by the team — unhelpful comments lower your credibility

CRITICAL: Only output valid JSON. No markdown, no explanation — just the JSON array."""


def get_pr_diff():
    """Read the PR diff from file."""
    diff_file = os.environ.get("DIFF_FILE", "/tmp/pr_diff.patch")
    if Path(diff_file).exists():
        return Path(diff_file).read_text()

    # Fallback: use GitHub API
    import requests

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.diff",
    }
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
        headers=headers,
    )
    resp.raise_for_status()
    return resp.text


def get_changed_files_context():
    """Get full file content for changed files (for deeper analysis)."""
    import requests

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files",
        headers=headers,
    )
    resp.raise_for_status()
    files = resp.json()

    context = []
    for f in files[:15]:  # Cap at 15 files
        context.append({
            "filename": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch": f.get("patch", ""),
        })
    return context


def run_review(knowledge, diff, files_context):
    """Run the Claude-powered review."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    model = knowledge["config"].get("model", "claude-sonnet-4-20250514")
    system_prompt = build_system_prompt(knowledge)

    user_message = f"""PR #{PR_NUMBER}: {PR_TITLE}
Author: {PR_AUTHOR}
Description: {PR_BODY or '(no description)'}

CHANGED FILES:
{json.dumps(files_context, indent=2)}

FULL DIFF:
```
{diff[:50000]}
```

Review this PR according to your rules and knowledge. Return only a JSON array of comments."""

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    response_text = response.content[0].text.strip()

    # Parse JSON (handle potential markdown wrapping)
    if response_text.startswith("```"):
        response_text = response_text.split("```")[1]
        if response_text.startswith("json"):
            response_text = response_text[4:]

    try:
        comments = json.loads(response_text)
    except json.JSONDecodeError:
        print(f"[ReviewBot] Failed to parse review response: {response_text[:200]}")
        comments = []

    return comments


def post_review_comments(comments):
    """Post review comments to the PR via GitHub API."""
    import requests

    if not comments:
        print("[ReviewBot] No comments to post.")
        return

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Build the review body
    severity_emoji = {
        "critical": "🚨",
        "high": "⚠️",
        "medium": "💡",
        "low": "📝",
        "praise": "✅",
    }

    review_comments = []
    summary_parts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "praise": 0}

    for c in comments:
        severity = c.get("severity", "medium")
        summary_parts[severity] = summary_parts.get(severity, 0) + 1

        emoji = severity_emoji.get(severity, "💡")
        body = f"{emoji} **[{severity.upper()}]** ({c.get('category', 'general')})\n\n{c['comment']}"

        if c.get("suggested_fix"):
            body += f"\n\n```suggestion\n{c['suggested_fix']}\n```"

        # Tag with rule_id for feedback tracking
        rule_id = c.get("rule_id", "unknown")
        comment_hash = hashlib.md5(f"{c['file']}:{c.get('line', 0)}:{c['comment'][:50]}".encode()).hexdigest()[:8]
        body += f"\n\n<sub>reviewbot:{rule_id}:{comment_hash}</sub>"

        if c.get("file") and c.get("line"):
            review_comments.append({
                "path": c["file"],
                "line": c["line"],
                "body": body,
            })

    # Create the review
    summary = " | ".join(
        f"{severity_emoji.get(k, '')} {k}: {v}"
        for k, v in summary_parts.items()
        if v > 0
    )

    review_body = {
        "body": f"## 🤖 ReviewBot Analysis\n\n{summary}\n\n"
                f"_I learn from your feedback! React with 👍/👎 on my comments, "
                f"or resolve/dismiss them to help me improve._",
        "event": "COMMENT",
        "comments": review_comments[:15],  # GitHub API limit
    }

    resp = requests.post(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/reviews",
        headers=headers,
        json=review_body,
    )

    if resp.status_code in (200, 201):
        print(f"[ReviewBot] Posted review with {len(review_comments)} comments.")
    else:
        print(f"[ReviewBot] Failed to post review: {resp.status_code} {resp.text}")


def record_review_metadata(comments):
    """Save review metadata for later feedback tracking."""
    metadata_file = KNOWLEDGE_DIR / "history" / "reviews.json"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if metadata_file.exists():
        try:
            existing = json.loads(metadata_file.read_text())
        except json.JSONDecodeError:
            existing = []

    review_record = {
        "pr_number": int(PR_NUMBER),
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        "author": PR_AUTHOR,
        "comments": [
            {
                "file": c.get("file"),
                "line": c.get("line"),
                "severity": c.get("severity"),
                "category": c.get("category"),
                "rule_id": c.get("rule_id", "unknown"),
                "comment_hash": hashlib.md5(
                    f"{c['file']}:{c.get('line', 0)}:{c['comment'][:50]}".encode()
                ).hexdigest()[:8],
            }
            for c in comments
        ],
    }

    existing.append(review_record)
    # Keep last 500 reviews
    existing = existing[-500:]
    metadata_file.write_text(json.dumps(existing, indent=2))


def main():
    print(f"[ReviewBot] Reviewing PR #{PR_NUMBER}: {PR_TITLE}")

    # Load knowledge
    knowledge = load_knowledge()
    print(f"[ReviewBot] Loaded {len(knowledge['rules'])} rules, "
          f"{len(knowledge.get('patterns', {}).get('known_bad', []))} bad patterns")

    # Get diff and context
    diff = get_pr_diff()
    files_context = get_changed_files_context()
    print(f"[ReviewBot] Analyzing {len(files_context)} changed files")

    # Run review
    comments = run_review(knowledge, diff, files_context)
    print(f"[ReviewBot] Generated {len(comments)} review comments")

    # Post to GitHub
    post_review_comments(comments)

    # Record for feedback tracking
    record_review_metadata(comments)


if __name__ == "__main__":
    main()
