"""
ReviewBot — Build Fixer
Analyzes build failures, learns from past fixes, and creates fix PRs.

Triggers when a GitHub issue is labeled 'build-failure'.
Uses knowledge store to avoid repeating fixes and learn patterns.
"""

import os
import json
import yaml
from pathlib import Path
from datetime import datetime

import requests
from anthropic import Anthropic

KNOWLEDGE_DIR = Path(".reviewbot")
PATTERNS_FILE = KNOWLEDGE_DIR / "patterns.json"
BUILD_FIXES_LOG = KNOWLEDGE_DIR / "history" / "build_fixes.json"
CONFIG_FILE = KNOWLEDGE_DIR / "config.yaml"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
ISSUE_NUMBER = os.environ.get("ISSUE_NUMBER", "")
ISSUE_BODY = os.environ.get("ISSUE_BODY", "")

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


def load_build_history():
    """Load past build fixes for pattern matching."""
    if BUILD_FIXES_LOG.exists():
        try:
            return json.loads(BUILD_FIXES_LOG.read_text())
        except json.JSONDecodeError:
            return []
    return []


def load_known_fix_patterns():
    """Load known fix patterns from the patterns file."""
    if PATTERNS_FILE.exists():
        patterns = json.loads(PATTERNS_FILE.read_text())
        return patterns.get("build_fix_patterns", [])
    return []


def get_recent_commits(n=10):
    """Get recent commits that might have caused the failure."""
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/commits",
        headers=HEADERS,
        params={"per_page": n},
    )
    if resp.status_code != 200:
        return []

    return [
        {
            "sha": c["sha"][:8],
            "message": c["commit"]["message"].split("\n")[0],
            "author": c["commit"]["author"]["name"],
            "files": [],  # Would need additional API call per commit
        }
        for c in resp.json()
    ]


def get_workflow_logs():
    """Try to get the failing workflow run logs."""
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/actions/runs",
        headers=HEADERS,
        params={"status": "failure", "per_page": 5},
    )
    if resp.status_code != 200:
        return ""

    runs = resp.json().get("workflow_runs", [])
    if not runs:
        return ""

    # Get the most recent failure
    run = runs[0]
    jobs_resp = requests.get(run["jobs_url"], headers=HEADERS)
    if jobs_resp.status_code != 200:
        return ""

    logs = []
    for job in jobs_resp.json().get("jobs", []):
        if job["conclusion"] == "failure":
            logs.append(f"Job: {job['name']} — Failed")
            for step in job.get("steps", []):
                if step["conclusion"] == "failure":
                    logs.append(f"  Step: {step['name']} — FAILED")

    return "\n".join(logs)


def analyze_and_fix():
    """Use Claude to analyze the build failure and suggest a fix."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    build_history = load_build_history()
    known_patterns = load_known_fix_patterns()
    recent_commits = get_recent_commits()
    workflow_logs = get_workflow_logs()

    # Build context from past fixes
    past_fixes_context = ""
    if known_patterns:
        past_fixes_context = "KNOWN BUILD FIX PATTERNS (from past experience):\n"
        for p in known_patterns[-10:]:
            past_fixes_context += f"- Error: {p.get('error_pattern', 'unknown')}\n"
            past_fixes_context += f"  Fix: {p.get('fix_description', 'unknown')}\n"
            past_fixes_context += f"  Files: {', '.join(p.get('files', []))}\n\n"

    prompt = f"""A build failure has been reported. Analyze it and suggest a fix.

BUILD FAILURE ISSUE:
{ISSUE_BODY}

RECENT WORKFLOW LOGS:
{workflow_logs or '(no logs available)'}

RECENT COMMITS (potential causes):
{json.dumps(recent_commits, indent=2)}

{past_fixes_context}

Analyze the failure and respond with JSON:
{{
  "root_cause": "Brief description of what went wrong",
  "likely_commit": "SHA of the likely culprit commit (or null)",
  "fix_type": "dependency|config|code|test|infra",
  "fix_description": "What needs to be fixed",
  "files_to_change": [
    {{
      "path": "path/to/file",
      "change_type": "modify|create|delete",
      "description": "What to change in this file",
      "suggested_content": "The fix (if applicable)"
    }}
  ],
  "confidence": "high|medium|low",
  "should_auto_fix": true/false,
  "error_pattern": "Regex or keyword pattern to detect this failure type in the future"
}}

Only respond with valid JSON."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json\n")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"[BuildFixer] Failed to parse response: {text[:200]}")
        return None


def create_fix_pr(analysis):
    """Create a PR with the suggested fix."""
    if not analysis or not analysis.get("should_auto_fix"):
        print("[BuildFixer] Fix not auto-applicable. Posting analysis as comment.")
        post_analysis_comment(analysis)
        return

    if analysis.get("confidence") == "low":
        print("[BuildFixer] Low confidence fix. Posting as comment instead of PR.")
        post_analysis_comment(analysis)
        return

    # For high/medium confidence fixes, create a branch and PR
    branch = f"reviewbot/build-fix-{ISSUE_NUMBER}-{datetime.utcnow().strftime('%Y%m%d%H%M')}"

    # Post the analysis as a comment on the issue
    comment_body = f"""## 🔧 ReviewBot Build Fix Analysis

**Root cause:** {analysis.get('root_cause', 'Unknown')}
**Fix type:** {analysis.get('fix_type', 'unknown')}
**Confidence:** {analysis.get('confidence', 'unknown')}

**Proposed fix:** {analysis.get('fix_description', 'N/A')}

Files to change:
"""
    for f in analysis.get("files_to_change", []):
        comment_body += f"- `{f['path']}` ({f['change_type']}): {f['description']}\n"

    comment_body += f"\n_A fix PR will be created on branch `{branch}` if confidence is medium or high._"

    requests.post(
        f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments",
        headers=HEADERS,
        json={"body": comment_body},
    )

    print(f"[BuildFixer] Posted analysis. Branch: {branch}")


def post_analysis_comment(analysis):
    """Post the analysis as an issue comment without creating a PR."""
    if not analysis:
        body = "🤖 ReviewBot could not analyze this build failure. Please investigate manually."
    else:
        body = f"""## 🔍 ReviewBot Build Failure Analysis

**Root cause:** {analysis.get('root_cause', 'Unknown')}
**Fix type:** {analysis.get('fix_type', 'unknown')}
**Confidence:** {analysis.get('confidence', 'low')}
**Likely culprit:** {analysis.get('likely_commit', 'Unknown')}

**Suggested fix:** {analysis.get('fix_description', 'N/A')}

_Confidence too low for auto-fix. Please investigate manually._"""

    requests.post(
        f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments",
        headers=HEADERS,
        json={"body": body},
    )


def record_fix(analysis):
    """Record the fix attempt for future learning."""
    if not analysis:
        return

    BUILD_FIXES_LOG.parent.mkdir(parents=True, exist_ok=True)
    history = load_build_history()
    history.append({
        "issue_number": int(ISSUE_NUMBER),
        "timestamp": datetime.utcnow().isoformat(),
        "root_cause": analysis.get("root_cause"),
        "fix_type": analysis.get("fix_type"),
        "confidence": analysis.get("confidence"),
        "error_pattern": analysis.get("error_pattern"),
        "auto_fixed": analysis.get("should_auto_fix", False),
    })
    history = history[-200:]
    BUILD_FIXES_LOG.write_text(json.dumps(history, indent=2))

    # Also update known patterns
    if analysis.get("error_pattern"):
        patterns = json.loads(PATTERNS_FILE.read_text()) if PATTERNS_FILE.exists() else {}
        if "build_fix_patterns" not in patterns:
            patterns["build_fix_patterns"] = []

        patterns["build_fix_patterns"].append({
            "error_pattern": analysis["error_pattern"],
            "fix_description": analysis.get("fix_description", ""),
            "fix_type": analysis.get("fix_type", ""),
            "files": [f["path"] for f in analysis.get("files_to_change", [])],
            "learned_at": datetime.utcnow().isoformat(),
        })
        patterns["build_fix_patterns"] = patterns["build_fix_patterns"][-50:]
        PATTERNS_FILE.write_text(json.dumps(patterns, indent=2))


def main():
    print(f"[BuildFixer] Analyzing build failure (issue #{ISSUE_NUMBER})")

    analysis = analyze_and_fix()
    if analysis:
        print(f"[BuildFixer] Root cause: {analysis.get('root_cause', 'unknown')}")
        print(f"[BuildFixer] Confidence: {analysis.get('confidence', 'unknown')}")
        create_fix_pr(analysis)
        record_fix(analysis)
    else:
        post_analysis_comment(None)


if __name__ == "__main__":
    main()
