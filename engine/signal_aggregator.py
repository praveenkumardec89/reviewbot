"""
ReviewCrew — Signal Aggregator
Collects all learning signals from the past week for the self-improvement engine.

Signals:
1. Comment feedback scores (reactions + resolutions)
2. PR revert patterns
3. Build failure patterns
4. Repeated fix patterns across PRs
5. Merge velocity (time from review to merge)
6. Component-level quality trends
"""

import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

import requests

KNOWLEDGE_DIR = Path(".reviewcrew")
SCORES_FILE = KNOWLEDGE_DIR / "scores.json"
FEEDBACK_LOG = KNOWLEDGE_DIR / "history" / "feedback.json"
REVERT_LOG = KNOWLEDGE_DIR / "history" / "reverts.json"
REVIEWS_LOG = KNOWLEDGE_DIR / "history" / "reviews.json"
SIGNALS_OUTPUT = Path("/tmp/aggregated_signals.json")

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

LOOKBACK_DAYS = 14  # Analyze last 2 weeks


def get_recent_merged_prs():
    """Fetch recently merged PRs."""
    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat() + "Z"
    prs = []
    page = 1
    while page <= 5:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO}/pulls",
            headers=HEADERS,
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 50,
                "page": page,
            },
        )
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        for pr in batch:
            if pr.get("merged_at") and pr["merged_at"] >= since:
                prs.append(pr)
        page += 1
    return prs


def compute_merge_velocity(prs):
    """Calculate average time from first review to merge."""
    velocities = []
    for pr in prs:
        created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
        merged = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
        hours = (merged - created).total_seconds() / 3600
        velocities.append({
            "pr": pr["number"],
            "hours_to_merge": round(hours, 1),
            "files_changed": pr.get("changed_files", 0),
        })
    return velocities


def analyze_repeated_patterns():
    """Find patterns that appear repeatedly in reviews — candidates for new rules."""
    reviews_log = []
    if REVIEWS_LOG.exists():
        try:
            reviews_log = json.loads(REVIEWS_LOG.read_text())
        except json.JSONDecodeError:
            reviews_log = []

    cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent = [r for r in reviews_log if r.get("timestamp", "") >= cutoff]

    # Count categories per file pattern
    category_counts = defaultdict(lambda: defaultdict(int))
    for review in recent:
        for comment in review.get("comments", []):
            # Group by directory
            file_path = comment.get("file", "")
            directory = "/".join(file_path.split("/")[:2]) if "/" in file_path else file_path
            category = comment.get("category", "general")
            category_counts[directory][category] += 1

    # Find repeated patterns (same category in same directory 3+ times)
    repeated = []
    for directory, cats in category_counts.items():
        for category, count in cats.items():
            if count >= 3:
                repeated.append({
                    "directory": directory,
                    "category": category,
                    "count": count,
                    "suggestion": f"Recurring {category} issues in {directory} — consider a targeted rule",
                })

    return repeated


def analyze_rule_effectiveness():
    """Rank rules by their feedback effectiveness."""
    scores = {}
    if SCORES_FILE.exists():
        try:
            scores = json.loads(SCORES_FILE.read_text())
        except json.JSONDecodeError:
            scores = {}

    rule_rankings = []
    for key, data in scores.items():
        if not key.startswith("rule:"):
            continue

        rule_id = key[5:]
        score = data.get("score", 0)
        samples = data.get("samples", 0)

        if samples < 3:
            effectiveness = "insufficient_data"
        elif score > 10:
            effectiveness = "highly_effective"
        elif score > 0:
            effectiveness = "effective"
        elif score > -5:
            effectiveness = "neutral"
        elif score > -10:
            effectiveness = "underperforming"
        else:
            effectiveness = "harmful"

        rule_rankings.append({
            "rule_id": rule_id,
            "score": score,
            "samples": samples,
            "effectiveness": effectiveness,
        })

    rule_rankings.sort(key=lambda x: x["score"], reverse=True)
    return rule_rankings


def analyze_component_quality():
    """Analyze quality trends per component/directory."""
    reviews_log = []
    if REVIEWS_LOG.exists():
        try:
            reviews_log = json.loads(REVIEWS_LOG.read_text())
        except json.JSONDecodeError:
            reviews_log = []

    component_stats = defaultdict(lambda: {
        "total_comments": 0,
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "praise": 0,
    })

    cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    for review in reviews_log:
        if review.get("timestamp", "") < cutoff:
            continue
        for comment in review.get("comments", []):
            file_path = comment.get("file", "")
            component = "/".join(file_path.split("/")[:2]) if "/" in file_path else "root"
            severity = comment.get("severity", "medium")
            component_stats[component]["total_comments"] += 1
            component_stats[component][severity] = component_stats[component].get(severity, 0) + 1

    return dict(component_stats)


def aggregate_all_signals():
    """Main aggregation — collect everything for the self-improvement engine."""
    print("[Aggregator] Collecting learning signals...")

    # 1. Recent PRs and merge velocity
    recent_prs = get_recent_merged_prs()
    velocity = compute_merge_velocity(recent_prs)
    print(f"  Found {len(recent_prs)} merged PRs in last {LOOKBACK_DAYS} days")

    # 2. Rule effectiveness
    rule_rankings = analyze_rule_effectiveness()
    effective = [r for r in rule_rankings if r["effectiveness"] == "highly_effective"]
    harmful = [r for r in rule_rankings if r["effectiveness"] == "harmful"]
    print(f"  Rule effectiveness: {len(effective)} highly effective, {len(harmful)} harmful")

    # 3. Repeated patterns
    repeated_patterns = analyze_repeated_patterns()
    print(f"  Found {len(repeated_patterns)} repeated patterns (candidates for new rules)")

    # 4. Revert analysis
    reverts = []
    if REVERT_LOG.exists():
        try:
            all_reverts = json.loads(REVERT_LOG.read_text())
            cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat()
            reverts = [r for r in all_reverts if r.get("timestamp", "") >= cutoff]
        except json.JSONDecodeError:
            pass
    print(f"  Found {len(reverts)} reverts in the lookback window")

    # 5. Component quality
    component_quality = analyze_component_quality()
    hotspots = {
        k: v for k, v in component_quality.items()
        if v.get("critical", 0) + v.get("high", 0) >= 3
    }
    print(f"  Quality hotspots: {list(hotspots.keys())}")

    # Assemble signals
    signals = {
        "generated_at": datetime.utcnow().isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "summary": {
            "total_prs_analyzed": len(recent_prs),
            "avg_merge_hours": round(
                sum(v["hours_to_merge"] for v in velocity) / max(len(velocity), 1), 1
            ),
            "total_reverts": len(reverts),
            "harmful_rules": len(harmful),
            "effective_rules": len(effective),
            "new_rule_candidates": len(repeated_patterns),
        },
        "rule_rankings": rule_rankings,
        "repeated_patterns": repeated_patterns,
        "reverts": reverts,
        "component_quality": component_quality,
        "merge_velocity": velocity,
    }

    SIGNALS_OUTPUT.write_text(json.dumps(signals, indent=2))
    print(f"[Aggregator] Signals written to {SIGNALS_OUTPUT}")
    return signals


if __name__ == "__main__":
    aggregate_all_signals()
