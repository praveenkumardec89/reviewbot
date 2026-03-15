"""
ReviewCrew — Revert Tracker
Detects when a PR is reverted and creates strong negative signals.

A revert means the review process MISSED something critical.
This is the strongest learning signal we have.
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime

import requests

KNOWLEDGE_DIR = Path(".reviewcrew")
SCORES_FILE = KNOWLEDGE_DIR / "scores.json"
REVERT_LOG = KNOWLEDGE_DIR / "history" / "reverts.json"
PATTERNS_FILE = KNOWLEDGE_DIR / "patterns.json"

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
PR_NUMBER = os.environ["PR_NUMBER"]
PR_TITLE = os.environ.get("PR_TITLE", "")

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

REVERT_PATTERN = re.compile(r'Revert\s+"?(.+?)"?\s*(?:\(#(\d+)\))?', re.IGNORECASE)


def find_original_pr():
    """Extract the original PR number from the revert PR title."""
    match = REVERT_PATTERN.search(PR_TITLE)
    if match and match.group(2):
        return int(match.group(2))

    # Fallback: search recent merged PRs by title
    original_title = match.group(1) if match else PR_TITLE.replace("Revert ", "").strip('"')
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls",
        headers=HEADERS,
        params={"state": "closed", "per_page": 50, "sort": "updated", "direction": "desc"},
    )
    if resp.status_code == 200:
        for pr in resp.json():
            if pr.get("merged_at") and original_title.lower() in pr["title"].lower():
                return pr["number"]

    return None


def get_original_pr_diff(pr_number):
    """Get the diff of the original PR that was reverted."""
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/files",
        headers=HEADERS,
    )
    if resp.status_code != 200:
        return []

    return [
        {
            "filename": f["filename"],
            "patch": f.get("patch", ""),
            "status": f["status"],
        }
        for f in resp.json()
    ]


def get_review_history(pr_number):
    """Get ReviewCrew's review comments on the original PR."""
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/comments",
        headers=HEADERS,
        params={"per_page": 100},
    )
    if resp.status_code != 200:
        return []

    bot_tag = re.compile(r"<sub>reviewcrew:([^:]+):([^<]+)</sub>")
    bot_comments = []
    for c in resp.json():
        match = bot_tag.search(c.get("body", ""))
        if match:
            bot_comments.append({
                "rule_id": match.group(1),
                "comment_hash": match.group(2),
                "path": c.get("path", ""),
                "body": c.get("body", ""),
            })

    return bot_comments


def record_revert(original_pr, files, review_history):
    """Record the revert event and update scores/patterns."""
    # Load existing data
    scores = json.loads(SCORES_FILE.read_text()) if SCORES_FILE.exists() else {}
    patterns = json.loads(PATTERNS_FILE.read_text()) if PATTERNS_FILE.exists() else {}

    REVERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    revert_log = []
    if REVERT_LOG.exists():
        try:
            revert_log = json.loads(REVERT_LOG.read_text())
        except json.JSONDecodeError:
            revert_log = []

    # Record the revert
    revert_record = {
        "revert_pr": int(PR_NUMBER),
        "original_pr": original_pr,
        "timestamp": datetime.utcnow().isoformat(),
        "files_affected": [f["filename"] for f in files],
        "had_review": len(review_history) > 0,
        "review_comment_count": len(review_history),
    }
    revert_log.append(revert_record)
    REVERT_LOG.write_text(json.dumps(revert_log[-200:], indent=2))

    # STRONG negative signal: if we reviewed and still missed it
    if review_history:
        reviewed_files = {c["path"] for c in review_history}
        reverted_files = {f["filename"] for f in files}
        missed_files = reverted_files - reviewed_files

        # Penalize all rules that were used but didn't catch the issue
        for comment in review_history:
            rule_id = comment["rule_id"]
            key = f"rule:{rule_id}"
            if key not in scores:
                scores[key] = {"score": 0, "samples": 0, "history": []}
            # Mild penalty — the rule fired but on the wrong thing
            scores[key]["score"] -= 2
            scores[key]["samples"] += 1
            scores[key]["history"].append({
                "delta": -2,
                "reason": "pr_reverted_after_review",
                "pr": original_pr,
                "timestamp": datetime.utcnow().isoformat(),
            })

        print(f"[Revert] Applied -2 penalty to {len(review_history)} rules that reviewed but missed the issue")

        if missed_files:
            print(f"[Revert] Files that were reverted but NOT reviewed: {missed_files}")

    # Add to known bad patterns for learning
    if "revert_patterns" not in patterns:
        patterns["revert_patterns"] = []

    for f in files:
        if f.get("patch"):
            patterns["revert_patterns"].append({
                "file": f["filename"],
                "original_pr": original_pr,
                "revert_pr": int(PR_NUMBER),
                "patch_preview": f["patch"][:500],
                "timestamp": datetime.utcnow().isoformat(),
            })

    # Keep last 100 revert patterns
    patterns["revert_patterns"] = patterns["revert_patterns"][-100:]

    # Save
    SCORES_FILE.write_text(json.dumps(scores, indent=2))
    PATTERNS_FILE.write_text(json.dumps(patterns, indent=2))

    print(f"[Revert] Recorded revert of PR #{original_pr} (revert PR #{PR_NUMBER})")
    print(f"[Revert] {len(files)} files affected, {len(review_history)} review comments existed")


def main():
    print(f"[Revert] Detected revert PR #{PR_NUMBER}: {PR_TITLE}")

    original_pr = find_original_pr()
    if not original_pr:
        print("[Revert] Could not identify original PR. Recording raw revert.")
        record_revert(None, [], [])
        return

    print(f"[Revert] Original PR: #{original_pr}")

    files = get_original_pr_diff(original_pr)
    review_history = get_review_history(original_pr)

    record_revert(original_pr, files, review_history)


if __name__ == "__main__":
    main()
