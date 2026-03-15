"""
ReviewBot — Feedback Collector
Tracks how the team responds to review comments to build feedback scores.

Signals tracked:
  - Comment resolved by author    → +2 (useful comment)
  - Comment dismissed              → -3 (noise/wrong)
  - 👍 reaction on comment         → +1 (team agrees)
  - 👎 reaction on comment         → -2 (team disagrees)
  - Review approved after changes  → +1 (review prompted good changes)
  - Review changes_requested       → neutral (expected flow)
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime

import requests

KNOWLEDGE_DIR = Path(".reviewbot")
SCORES_FILE = KNOWLEDGE_DIR / "scores.json"
FEEDBACK_LOG = KNOWLEDGE_DIR / "history" / "feedback.json"

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
PR_NUMBER = os.environ["PR_NUMBER"]
REVIEW_STATE = os.environ.get("REVIEW_STATE", "")
REVIEWER = os.environ.get("REVIEWER", "")

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

BOT_TAG_PATTERN = re.compile(r"<sub>reviewbot:([^:]+):([^<]+)</sub>")


def load_scores():
    if SCORES_FILE.exists():
        return json.loads(SCORES_FILE.read_text())
    return {}


def save_scores(scores):
    SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCORES_FILE.write_text(json.dumps(scores, indent=2))


def load_feedback_log():
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    if FEEDBACK_LOG.exists():
        try:
            return json.loads(FEEDBACK_LOG.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_feedback_log(log):
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = log[-2000:]  # Keep last 2000 entries
    FEEDBACK_LOG.write_text(json.dumps(log, indent=2))


def update_score(scores, rule_id, delta, reason):
    """Update the score for a specific rule."""
    key = f"rule:{rule_id}"
    if key not in scores:
        scores[key] = {"score": 0, "samples": 0, "history": []}

    scores[key]["score"] += delta
    scores[key]["samples"] += 1
    scores[key]["history"].append({
        "delta": delta,
        "reason": reason,
        "pr": int(PR_NUMBER),
        "timestamp": datetime.utcnow().isoformat(),
    })
    # Keep last 50 history entries per rule
    scores[key]["history"] = scores[key]["history"][-50:]


def get_review_comments():
    """Fetch all review comments on this PR that were made by ReviewBot."""
    comments = []
    page = 1
    while True:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/comments",
            headers=HEADERS,
            params={"per_page": 100, "page": page},
        )
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        comments.extend(batch)
        page += 1

    # Filter to ReviewBot comments
    bot_comments = []
    for c in comments:
        match = BOT_TAG_PATTERN.search(c.get("body", ""))
        if match:
            bot_comments.append({
                "id": c["id"],
                "body": c["body"],
                "rule_id": match.group(1),
                "comment_hash": match.group(2),
                "created_at": c["created_at"],
                "path": c.get("path", ""),
                "line": c.get("line", 0),
                "in_reply_to_id": c.get("in_reply_to_id"),
            })

    return bot_comments


def get_comment_reactions(comment_id):
    """Fetch reactions on a specific comment."""
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/comments/{comment_id}/reactions",
        headers={**HEADERS, "Accept": "application/vnd.github.squirrel-girl-preview+json"},
    )
    if resp.status_code != 200:
        return {"thumbs_up": 0, "thumbs_down": 0}

    reactions = resp.json()
    return {
        "thumbs_up": sum(1 for r in reactions if r["content"] == "+1"),
        "thumbs_down": sum(1 for r in reactions if r["content"] == "-1"),
    }


def check_comment_resolution(comment_id):
    """Check if a review comment thread was resolved or dismissed.

    GitHub doesn't have a direct API for this in all cases,
    so we check if there are replies indicating resolution.
    """
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/comments",
        headers=HEADERS,
        params={"per_page": 100},
    )
    if resp.status_code != 200:
        return "unknown"

    comments = resp.json()
    replies = [c for c in comments if c.get("in_reply_to_id") == comment_id]

    for reply in replies:
        body_lower = reply.get("body", "").lower()
        if any(w in body_lower for w in ["fixed", "resolved", "done", "addressed", "updated"]):
            return "resolved"
        if any(w in body_lower for w in ["dismiss", "ignore", "not applicable", "nit", "won't fix"]):
            return "dismissed"

    return "no_reply"


def collect_feedback():
    """Main feedback collection logic."""
    scores = load_scores()
    feedback_log = load_feedback_log()

    bot_comments = get_review_comments()
    print(f"[Feedback] Found {len(bot_comments)} ReviewBot comments on PR #{PR_NUMBER}")

    for comment in bot_comments:
        rule_id = comment["rule_id"]
        comment_hash = comment["comment_hash"]
        feedback_key = f"pr{PR_NUMBER}:{comment_hash}"

        # Skip if we already processed this comment
        if any(f.get("feedback_key") == feedback_key for f in feedback_log):
            continue

        # Check reactions
        reactions = get_comment_reactions(comment["id"])
        if reactions["thumbs_up"] > 0:
            delta = reactions["thumbs_up"]
            update_score(scores, rule_id, delta, "thumbs_up")
            print(f"  [+{delta}] Rule '{rule_id}' got {reactions['thumbs_up']} 👍")

        if reactions["thumbs_down"] > 0:
            delta = -2 * reactions["thumbs_down"]
            update_score(scores, rule_id, delta, "thumbs_down")
            print(f"  [{delta}] Rule '{rule_id}' got {reactions['thumbs_down']} 👎")

        # Check resolution status
        resolution = check_comment_resolution(comment["id"])
        if resolution == "resolved":
            update_score(scores, rule_id, 2, "comment_resolved")
            print(f"  [+2] Rule '{rule_id}' comment was resolved (useful!)")
        elif resolution == "dismissed":
            update_score(scores, rule_id, -3, "comment_dismissed")
            print(f"  [-3] Rule '{rule_id}' comment was dismissed (noise)")

        # Log this feedback
        feedback_log.append({
            "feedback_key": feedback_key,
            "pr": int(PR_NUMBER),
            "rule_id": rule_id,
            "comment_hash": comment_hash,
            "reactions": reactions,
            "resolution": resolution,
            "timestamp": datetime.utcnow().isoformat(),
        })

    # Track review-level signals
    if REVIEW_STATE == "approved":
        # The PR was approved — our review led to good changes
        for comment in bot_comments:
            update_score(scores, comment["rule_id"], 1, "pr_approved_after_review")

    save_scores(scores)
    save_feedback_log(feedback_log)
    print(f"[Feedback] Updated scores for {len(bot_comments)} comments")


if __name__ == "__main__":
    collect_feedback()
