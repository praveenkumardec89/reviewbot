"""
ReviewBot — Entry Point
Loads knowledge, fetches PR data, runs the multi-agent orchestrator, posts results.
"""

import os
import json
import yaml
import hashlib
from pathlib import Path

import requests

from .orchestrator import orchestrate

# ─── Config ───────────────────────────────────────────────────────────────────

KNOWLEDGE_DIR = Path(".reviewbot")
RULES_FILE    = KNOWLEDGE_DIR / "rules.yaml"
PATTERNS_FILE = KNOWLEDGE_DIR / "patterns.json"
SCORES_FILE   = KNOWLEDGE_DIR / "scores.json"
INFRA_FILE    = KNOWLEDGE_DIR / "infra.yaml"
CONFIG_FILE   = KNOWLEDGE_DIR / "config.yaml"

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO         = os.environ["REPO"]
PR_NUMBER    = os.environ["PR_NUMBER"]
PR_TITLE     = os.environ.get("PR_TITLE", "")
PR_AUTHOR    = os.environ.get("PR_AUTHOR", "")

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

SEVERITY_EMOJI = {
    "critical": "🚨",
    "high":     "⚠️",
    "medium":   "💡",
    "low":      "📝",
    "praise":   "✅",
}

AGENT_EMOJI = {
    "security":       "🔒",
    "code_quality":   "✨",
    "architecture":   "🏗️",
    "simplification": "🔧",
    "test_coverage":  "🧪",
    "performance":    "⚡",
}


# ─── Knowledge Store ─────────────────────────────────────────────────────────

def load_knowledge() -> dict:
    knowledge = {
        "rules":    [],
        "patterns": {},
        "scores":   {},
        "infra":    {},
        "config":   _default_config(),
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

    # Mark boosted rules so agents can prioritize them
    scores = knowledge["scores"]
    min_samples = knowledge["config"]["learning"].get("min_feedback_samples", 5)
    suppressed = []
    for rule in knowledge["rules"]:
        rule_id = rule.get("id", "")
        data = scores.get(f"rule:{rule_id}", {})
        score, samples = data.get("score", 0), data.get("samples", 0)
        if samples >= min_samples:
            if score < -10:
                suppressed.append(rule_id)
            elif score > 10:
                rule["boosted"] = True
    if suppressed:
        knowledge["rules"] = [r for r in knowledge["rules"] if r.get("id") not in suppressed]
        print(f"[ReviewBot] Suppressed {len(suppressed)} low-scoring rules: {suppressed}")

    return knowledge


def _default_config() -> dict:
    return {
        "model": "claude-sonnet-4-20250514",
        "review": {
            "auto_review": True,
            "severity_threshold": "low",
            "max_comments_per_pr": 20,
            "review_tests": True,
        },
        "learning": {
            "enabled": True,
            "min_feedback_samples": 5,
        },
    }


# ─── GitHub Data ──────────────────────────────────────────────────────────────

def get_pr_diff() -> str:
    diff_file = os.environ.get("DIFF_FILE", "/tmp/pr_diff.patch")
    if Path(diff_file).exists():
        return Path(diff_file).read_text()

    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
        headers={**HEADERS, "Accept": "application/vnd.github.v3.diff"},
    )
    resp.raise_for_status()
    return resp.text


def get_changed_files() -> list:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files",
        headers=HEADERS,
    )
    resp.raise_for_status()
    return [
        {
            "filename":  f["filename"],
            "status":    f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch":     f.get("patch", ""),
        }
        for f in resp.json()[:20]  # cap at 20 files
    ]


# ─── Posting Results ─────────────────────────────────────────────────────────

def build_review_body(routing_report: dict) -> str:
    """Build the top-level review summary shown on the PR."""
    selected = routing_report.get("selected", [])
    skipped  = routing_report.get("skipped", [])
    per_agent = routing_report.get("per_agent", {})
    total    = routing_report.get("total_final", 0)

    # Agents section
    agent_lines = []
    for name in selected:
        emoji = AGENT_EMOJI.get(name, "🔍")
        count = per_agent.get(name, 0)
        agent_lines.append(f"  {emoji} **{name}**: {count} finding{'s' if count != 1 else ''}")
    for name in skipped:
        agent_lines.append(f"  ~~{name}~~ _(skipped — not relevant to this PR)_")

    agents_text = "\n".join(agent_lines)

    return (
        f"## 🤖 ReviewBot Multi-Agent Review\n\n"
        f"**{total} finding{'s' if total != 1 else ''} from {len(selected)} specialized agent{'s' if len(selected) != 1 else ''}**\n\n"
        f"{agents_text}\n\n"
        f"_React with 👍/👎 on comments, or resolve/dismiss them to help me learn and improve._"
    )


def post_review(comments: list, routing_report: dict) -> None:
    if not comments and not routing_report.get("selected"):
        print("[ReviewBot] No agents ran — nothing to post.")
        return

    max_comments = 20
    review_comments = []

    for c in comments[:max_comments]:
        if not c.get("file") or not c.get("line"):
            continue

        severity = c.get("severity", "medium")
        agent    = c.get("agent", "")
        emoji    = SEVERITY_EMOJI.get(severity, "💡")
        agent_tag = f" · {AGENT_EMOJI.get(agent, '')} {agent}" if agent else ""

        body = (
            f"{emoji} **[{severity.upper()}]**"
            f" ({c.get('category', 'general')}{agent_tag})\n\n"
            f"{c['comment']}"
        )

        if c.get("suggested_fix"):
            body += f"\n\n```suggestion\n{c['suggested_fix']}\n```"

        rule_id = c.get("rule_id", "unknown")
        comment_hash = hashlib.md5(
            f"{c['file']}:{c.get('line', 0)}:{c['comment'][:50]}".encode()
        ).hexdigest()[:8]
        body += f"\n\n<sub>reviewbot:{rule_id}:{comment_hash}</sub>"

        review_comments.append({
            "path": c["file"],
            "line": c["line"],
            "body": body,
        })

    payload = {
        "body":     build_review_body(routing_report),
        "event":    "COMMENT",
        "comments": review_comments,
    }

    resp = requests.post(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/reviews",
        headers=HEADERS,
        json=payload,
    )

    if resp.status_code in (200, 201):
        print(f"[ReviewBot] Posted review: {len(review_comments)} inline comments.")
    else:
        print(f"[ReviewBot] Failed to post review: {resp.status_code} — {resp.text[:200]}")


# ─── Metadata ─────────────────────────────────────────────────────────────────

def record_metadata(comments: list, routing_report: dict) -> None:
    metadata_file = KNOWLEDGE_DIR / "history" / "reviews.json"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if metadata_file.exists():
        try:
            existing = json.loads(metadata_file.read_text())
        except json.JSONDecodeError:
            existing = []

    import datetime
    existing.append({
        "pr_number":      int(PR_NUMBER),
        "timestamp":      datetime.datetime.utcnow().isoformat(),
        "author":         PR_AUTHOR,
        "agents_used":    routing_report.get("selected", []),
        "agents_skipped": routing_report.get("skipped", []),
        "comments": [
            {
                "file":         c.get("file"),
                "line":         c.get("line"),
                "severity":     c.get("severity"),
                "category":     c.get("category"),
                "agent":        c.get("agent"),
                "rule_id":      c.get("rule_id", "unknown"),
                "comment_hash": hashlib.md5(
                    f"{c['file']}:{c.get('line', 0)}:{c['comment'][:50]}".encode()
                ).hexdigest()[:8],
            }
            for c in comments
        ],
    })

    existing = existing[-500:]
    metadata_file.write_text(json.dumps(existing, indent=2))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[ReviewBot] PR #{PR_NUMBER}: {PR_TITLE}")

    knowledge = load_knowledge()
    print(f"[ReviewBot] Knowledge: {len(knowledge['rules'])} rules loaded")

    diff          = get_pr_diff()
    files_context = get_changed_files()
    print(f"[ReviewBot] {len(files_context)} changed files fetched")

    # Multi-agent orchestrated review
    comments, routing_report = orchestrate(diff, files_context, knowledge)

    # Post to GitHub
    post_review(comments, routing_report)

    # Save for feedback tracking + self-improvement
    record_metadata(comments, routing_report)


if __name__ == "__main__":
    main()
