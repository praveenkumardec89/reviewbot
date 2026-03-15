"""
ReviewCrew — Self-Improvement Engine
Analyzes aggregated signals and generates rule improvements.

Actions it can take:
1. RETIRE harmful rules (score < -10 with enough samples)
2. BOOST effective rules (score > 10 with enough samples)
3. CREATE new rules from repeated patterns
4. CREATE "never miss" rules from revert analysis
5. UPDATE infra knowledge from component quality data
6. REFINE existing rules based on feedback patterns

All changes are proposed via PR — human-in-the-loop for safety.
"""

import os
import json
import yaml
from pathlib import Path
from datetime import datetime
from copy import deepcopy

from anthropic import Anthropic

KNOWLEDGE_DIR = Path(".reviewcrew")
RULES_FILE = KNOWLEDGE_DIR / "rules.yaml"
PATTERNS_FILE = KNOWLEDGE_DIR / "patterns.json"
SCORES_FILE = KNOWLEDGE_DIR / "scores.json"
INFRA_FILE = KNOWLEDGE_DIR / "infra.yaml"
CONFIG_FILE = KNOWLEDGE_DIR / "config.yaml"
IMPROVEMENTS_LOG = KNOWLEDGE_DIR / "history" / "improvements.json"

SIGNALS_FILE = Path("/tmp/aggregated_signals.json")
SUMMARY_FILE = Path("/tmp/improvement_summary.txt")
PR_BODY_FILE = Path("/tmp/improvement_pr_body.md")
REVIEWERS_FILE = Path("/tmp/reviewers.txt")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

MIN_SAMPLES = 5
HARMFUL_THRESHOLD = -10
BOOST_THRESHOLD = 10
REPEATED_PATTERN_THRESHOLD = 3


def load_signals():
    if not SIGNALS_FILE.exists():
        print("[SelfImprove] No signals file found. Nothing to improve.")
        return None
    return json.loads(SIGNALS_FILE.read_text())


def load_current_state():
    """Load all current knowledge store files."""
    state = {
        "rules": [],
        "patterns": {},
        "scores": {},
        "infra": {},
    }
    if RULES_FILE.exists():
        state["rules"] = yaml.safe_load(RULES_FILE.read_text()) or []
    if PATTERNS_FILE.exists():
        state["patterns"] = json.loads(PATTERNS_FILE.read_text())
    if SCORES_FILE.exists():
        state["scores"] = json.loads(SCORES_FILE.read_text())
    if INFRA_FILE.exists():
        state["infra"] = yaml.safe_load(INFRA_FILE.read_text()) or {}
    return state


def retire_harmful_rules(state, signals):
    """Remove rules that consistently get negative feedback."""
    changes = []
    harmful = [
        r for r in signals.get("rule_rankings", [])
        if r["effectiveness"] == "harmful" and r["samples"] >= MIN_SAMPLES
    ]

    if not harmful:
        return changes

    original_rules = state["rules"][:]
    remaining_rules = []
    for rule in state["rules"]:
        rule_id = rule.get("id", "")
        is_harmful = any(h["rule_id"] == rule_id for h in harmful)
        if is_harmful:
            score_data = next((h for h in harmful if h["rule_id"] == rule_id), {})
            changes.append({
                "action": "RETIRE",
                "rule_id": rule_id,
                "description": rule.get("description", ""),
                "reason": f"Score {score_data.get('score', 0)} over {score_data.get('samples', 0)} samples — team consistently disagreed",
            })
        else:
            remaining_rules.append(rule)

    state["rules"] = remaining_rules
    return changes


def boost_effective_rules(state, signals):
    """Mark highly effective rules with higher priority."""
    changes = []
    effective = [
        r for r in signals.get("rule_rankings", [])
        if r["effectiveness"] == "highly_effective" and r["samples"] >= MIN_SAMPLES
    ]

    for eff in effective:
        for rule in state["rules"]:
            if rule.get("id") == eff["rule_id"] and not rule.get("boosted"):
                rule["boosted"] = True
                rule["severity"] = max_severity(rule.get("severity", "medium"))
                changes.append({
                    "action": "BOOST",
                    "rule_id": eff["rule_id"],
                    "description": rule.get("description", ""),
                    "reason": f"Score {eff['score']} over {eff['samples']} samples — consistently helpful",
                })

    return changes


def max_severity(current):
    order = ["low", "medium", "high", "critical"]
    idx = order.index(current) if current in order else 1
    return order[min(idx + 1, len(order) - 1)]


def create_rules_from_patterns(state, signals):
    """Generate new rules from repeated review patterns."""
    changes = []
    repeated = signals.get("repeated_patterns", [])

    if not repeated:
        return changes

    existing_rule_ids = {r.get("id", "") for r in state["rules"]}

    for pattern in repeated:
        if pattern["count"] < REPEATED_PATTERN_THRESHOLD:
            continue

        # Generate a rule ID
        rule_id = f"auto_{pattern['directory'].replace('/', '_')}_{pattern['category']}"
        if rule_id in existing_rule_ids:
            continue

        # Use Claude to generate a well-written rule
        new_rule = generate_rule_with_ai(pattern)
        if new_rule:
            new_rule["id"] = rule_id
            new_rule["auto_generated"] = True
            new_rule["generated_from"] = "repeated_pattern"
            new_rule["created_at"] = datetime.utcnow().isoformat()
            state["rules"].append(new_rule)
            changes.append({
                "action": "CREATE",
                "rule_id": rule_id,
                "description": new_rule.get("description", ""),
                "reason": f"Pattern '{pattern['category']}' appeared {pattern['count']} times in {pattern['directory']}",
            })

    return changes


def create_rules_from_reverts(state, signals):
    """Generate 'never miss' rules from PR reverts."""
    changes = []
    reverts = signals.get("reverts", [])

    if not reverts:
        return changes

    patterns = state.get("patterns", {})
    revert_patterns = patterns.get("revert_patterns", [])

    if not revert_patterns:
        return changes

    # Group reverts by file directory
    from collections import defaultdict
    dir_reverts = defaultdict(list)
    for rp in revert_patterns:
        directory = "/".join(rp["file"].split("/")[:2])
        dir_reverts[directory].append(rp)

    existing_rule_ids = {r.get("id", "") for r in state["rules"]}

    for directory, dir_patterns in dir_reverts.items():
        if len(dir_patterns) < 2:
            continue

        rule_id = f"revert_guard_{directory.replace('/', '_')}"
        if rule_id in existing_rule_ids:
            continue

        # Use Claude to analyze the revert pattern and create a rule
        new_rule = generate_revert_rule_with_ai(directory, dir_patterns)
        if new_rule:
            new_rule["id"] = rule_id
            new_rule["auto_generated"] = True
            new_rule["generated_from"] = "revert_analysis"
            new_rule["severity"] = "high"  # Revert-based rules are always high severity
            new_rule["created_at"] = datetime.utcnow().isoformat()
            state["rules"].append(new_rule)
            changes.append({
                "action": "CREATE",
                "rule_id": rule_id,
                "description": new_rule.get("description", ""),
                "reason": f"Reverts detected in {directory} — creating guard rule",
            })

    return changes


def update_infra_knowledge(state, signals):
    """Update infrastructure knowledge from component quality data."""
    changes = []
    component_quality = signals.get("component_quality", {})

    for component, stats in component_quality.items():
        critical_high = stats.get("critical", 0) + stats.get("high", 0)
        if critical_high >= 3:
            if component not in state["infra"]:
                state["infra"][component] = {}

            state["infra"][component]["quality_hotspot"] = True
            state["infra"][component]["recent_critical_issues"] = critical_high
            state["infra"][component]["last_analyzed"] = datetime.utcnow().isoformat()

            changes.append({
                "action": "UPDATE_INFRA",
                "component": component,
                "reason": f"{critical_high} critical/high issues in last analysis period",
            })

    return changes


def generate_rule_with_ai(pattern):
    """Use Claude to generate a well-written review rule from a pattern."""
    client = Anthropic()

    prompt = f"""Based on this recurring code review pattern, generate a review rule.

Pattern detected:
- Directory: {pattern['directory']}
- Category: {pattern['category']}
- Occurrences: {pattern['count']}
- Suggestion: {pattern['suggestion']}

Generate a YAML-compatible review rule with these fields:
- description: Clear, actionable rule description (1-2 sentences)
- severity: low/medium/high
- category: {pattern['category']}
- scope: file glob pattern for where this applies
- example: optional bad code example

Respond with ONLY valid JSON (no markdown):
{{"description": "...", "severity": "...", "category": "...", "scope": "...", "example": "..."}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json\n")
        return json.loads(text)
    except Exception as e:
        print(f"[SelfImprove] Failed to generate rule: {e}")
        return None


def generate_revert_rule_with_ai(directory, revert_patterns):
    """Use Claude to analyze revert patterns and generate a guard rule."""
    client = Anthropic()

    patches_preview = "\n---\n".join(
        f"File: {rp['file']}\nPatch preview:\n{rp['patch_preview'][:200]}"
        for rp in revert_patterns[:3]
    )

    prompt = f"""PRs touching the "{directory}" directory have been reverted multiple times.
Here are snippets of the reverted changes:

{patches_preview}

Analyze these revert patterns and generate a review rule that would catch similar issues BEFORE they get merged.

Generate a YAML-compatible rule with:
- description: What to watch for (be specific about the pattern that keeps causing reverts)
- severity: high (always high for revert-based rules)
- category: the type of issue (bug, architecture, test, etc.)
- scope: file glob pattern
- example: optional example of what NOT to do

Respond with ONLY valid JSON:
{{"description": "...", "severity": "high", "category": "...", "scope": "...", "example": "..."}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json\n")
        return json.loads(text)
    except Exception as e:
        print(f"[SelfImprove] Failed to generate revert rule: {e}")
        return None


def generate_pr_body(changes, signals):
    """Generate a detailed PR body explaining all improvements."""
    summary = signals.get("summary", {})

    sections = []
    sections.append("## 🤖 ReviewCrew Self-Improvement Report\n")
    sections.append(f"**Analysis period:** Last {signals.get('lookback_days', 14)} days")
    sections.append(f"**PRs analyzed:** {summary.get('total_prs_analyzed', 0)}")
    sections.append(f"**Avg merge time:** {summary.get('avg_merge_hours', 0)} hours")
    sections.append(f"**Reverts detected:** {summary.get('total_reverts', 0)}\n")

    # Group changes by action
    by_action = {}
    for c in changes:
        action = c["action"]
        if action not in by_action:
            by_action[action] = []
        by_action[action].append(c)

    if "RETIRE" in by_action:
        sections.append("### 🗑️ Rules Retired (consistently unhelpful)")
        for c in by_action["RETIRE"]:
            sections.append(f"- **{c['rule_id']}**: {c['description']}")
            sections.append(f"  - Reason: {c['reason']}")

    if "BOOST" in by_action:
        sections.append("\n### ⬆️ Rules Boosted (proven effective)")
        for c in by_action["BOOST"]:
            sections.append(f"- **{c['rule_id']}**: {c['description']}")
            sections.append(f"  - Reason: {c['reason']}")

    if "CREATE" in by_action:
        sections.append("\n### ✨ New Rules Created")
        for c in by_action["CREATE"]:
            sections.append(f"- **{c['rule_id']}**: {c['description']}")
            sections.append(f"  - Reason: {c['reason']}")

    if "UPDATE_INFRA" in by_action:
        sections.append("\n### 🏗️ Infrastructure Knowledge Updated")
        for c in by_action["UPDATE_INFRA"]:
            sections.append(f"- **{c['component']}**: {c['reason']}")

    sections.append("\n---")
    sections.append("*This PR was auto-generated by ReviewCrew. Please review the changes carefully.*")
    sections.append("*React with 👍 to approve or suggest edits if rules need adjustment.*")

    return "\n".join(sections)


def run_self_improvement():
    """Main self-improvement loop."""
    print("[SelfImprove] Starting self-improvement cycle...")

    signals = load_signals()
    if not signals:
        return

    state = load_current_state()
    original_state = deepcopy(state)

    all_changes = []

    # 1. Retire harmful rules
    changes = retire_harmful_rules(state, signals)
    all_changes.extend(changes)
    if changes:
        print(f"[SelfImprove] Retiring {len(changes)} harmful rules")

    # 2. Boost effective rules
    changes = boost_effective_rules(state, signals)
    all_changes.extend(changes)
    if changes:
        print(f"[SelfImprove] Boosting {len(changes)} effective rules")

    # 3. Create rules from repeated patterns
    changes = create_rules_from_patterns(state, signals)
    all_changes.extend(changes)
    if changes:
        print(f"[SelfImprove] Created {len(changes)} rules from patterns")

    # 4. Create rules from reverts
    changes = create_rules_from_reverts(state, signals)
    all_changes.extend(changes)
    if changes:
        print(f"[SelfImprove] Created {len(changes)} rules from reverts")

    # 5. Update infra knowledge
    changes = update_infra_knowledge(state, signals)
    all_changes.extend(changes)
    if changes:
        print(f"[SelfImprove] Updated infra knowledge for {len(changes)} components")

    if not all_changes:
        print("[SelfImprove] No improvements needed. Knowledge store is up to date.")
        os.environ["HAS_IMPROVEMENTS"] = "false"
        # Write to GITHUB_ENV for the workflow
        env_file = os.environ.get("GITHUB_ENV")
        if env_file:
            with open(env_file, "a") as f:
                f.write("HAS_IMPROVEMENTS=false\n")
        return

    print(f"[SelfImprove] Total changes: {len(all_changes)}")

    if DRY_RUN:
        print("[SelfImprove] DRY RUN — not writing changes")
        for c in all_changes:
            print(f"  [{c['action']}] {c.get('rule_id', c.get('component', ''))}: {c.get('reason', '')}")
        return

    # Write updated state
    RULES_FILE.write_text(yaml.dump(state["rules"], default_flow_style=False, sort_keys=False))
    PATTERNS_FILE.write_text(json.dumps(state["patterns"], indent=2))
    SCORES_FILE.write_text(json.dumps(state["scores"], indent=2))
    INFRA_FILE.write_text(yaml.dump(state["infra"], default_flow_style=False, sort_keys=False))

    # Log improvements
    IMPROVEMENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    improvements = []
    if IMPROVEMENTS_LOG.exists():
        try:
            improvements = json.loads(IMPROVEMENTS_LOG.read_text())
        except json.JSONDecodeError:
            improvements = []

    improvements.append({
        "timestamp": datetime.utcnow().isoformat(),
        "changes": all_changes,
        "signals_summary": signals.get("summary", {}),
    })
    improvements = improvements[-100:]
    IMPROVEMENTS_LOG.write_text(json.dumps(improvements, indent=2))

    # Generate PR body
    pr_body = generate_pr_body(all_changes, signals)
    PR_BODY_FILE.write_text(pr_body)

    # Generate summary for commit message
    summary_lines = [f"- {c['action']}: {c.get('rule_id', c.get('component', ''))}" for c in all_changes]
    SUMMARY_FILE.write_text("\n".join(summary_lines))

    # Set environment variable for workflow
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write("HAS_IMPROVEMENTS=true\n")
    os.environ["HAS_IMPROVEMENTS"] = "true"

    print(f"[SelfImprove] Wrote {len(all_changes)} improvements. PR will be created.")


if __name__ == "__main__":
    run_self_improvement()
