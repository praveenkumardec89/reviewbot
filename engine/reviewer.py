"""
ReviewCrew — Entry Point
Loads knowledge, fetches PR data, runs the multi-agent orchestrator, posts results.
"""

import os
import json
import yaml
import hashlib
from pathlib import Path

import requests

from .orchestrator import orchestrate
from .context_builder import build_project_context

# ─── Config ───────────────────────────────────────────────────────────────────

KNOWLEDGE_DIR = Path(".reviewcrew")
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
        print(f"[ReviewCrew] Suppressed {len(suppressed)} low-scoring rules: {suppressed}")

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


def get_changed_files(max_files: int = 50) -> list:
    all_files, page = [], 1
    while True:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files",
            headers=HEADERS,
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_files.extend(batch)
        if len(all_files) >= max_files or len(batch) < 100:
            break
        page += 1

    if len(all_files) > max_files:
        print(f"[ReviewCrew] Large PR: {len(all_files)} files changed — "
              f"reviewing first {max_files}. "
              f"Use /reviewcrew fix to apply suggestions file by file.")
        all_files = all_files[:max_files]

    return [
        {
            "filename":  f["filename"],
            "status":    f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch":     f.get("patch", ""),
        }
        for f in all_files
    ]


# ─── Posting Results ─────────────────────────────────────────────────────────

def build_review_body(routing_report: dict, project_context: dict | None = None) -> str:
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

    body = (
        f"## 🤖 ReviewCrew Multi-Agent Review\n\n"
        f"**{total} finding{'s' if total != 1 else ''} from {len(selected)} specialized agent{'s' if len(selected) != 1 else ''}**\n\n"
        f"{agents_text}\n"
    )

    # Architectural Impact Summary (shown when arch_impact has findings)
    if project_context:
        arch_summary = _build_arch_impact_summary(project_context)
        if arch_summary:
            body += f"\n{arch_summary}\n"

    body += "\n_React with 👍/👎 on comments, or resolve/dismiss them to help me learn and improve._"
    return body


def _build_arch_impact_summary(project_context: dict) -> str:
    """
    Build the Architectural Impact Summary block shown in the PR review body.
    Only renders when there is something meaningful to show.
    """
    arch_impact = project_context.get("arch_impact", {})
    arch_config = project_context.get("arch_config", {})
    module_graph = project_context.get("module_graph", {})
    ts = project_context.get("tech_stack", {})

    lines = []

    # ── Layer summary ──
    layer_assignments = arch_impact.get("layer_assignments", {})
    if layer_assignments:
        layers_touched = sorted(set(v for v in layer_assignments.values() if v != "unknown"))
        if layers_touched:
            lines.append(f"**Layers touched:** {' · '.join(f'`{l}`' for l in layers_touched)}")

    # ── Layer violations ──
    violations = arch_impact.get("layer_violations", [])
    if violations:
        lines.append(f"**⚠️ Layer violations ({len(violations)}):**")
        for v in violations[:5]:
            lines.append(f"  - {v['reason']}")

    # ── Blast radius ──
    high_blast = [(f, info) for f, info in module_graph.items() if info.get("blast_radius", 0) >= 5]
    if high_blast:
        lines.append(f"**Blast radius:**")
        for f, info in sorted(high_blast, key=lambda x: x[1]["blast_radius"], reverse=True)[:3]:
            lines.append(f"  - `{Path(f).name}` — **{info['blast_radius']}** files depend on this")

    # ── Upstream services ──
    upstream_impact = arch_impact.get("upstream_impact", [])
    if upstream_impact:
        lines.append(f"**Upstream services (callers that may be affected):**")
        for u in upstream_impact[:4]:
            sev = u.get("breaking_change_severity", "high").upper()
            lines.append(f"  - [{sev}] **{u['service']}** — {u['reason']}")

    # ── Downstream services ──
    downstream_impact = arch_impact.get("downstream_impact", [])
    if downstream_impact:
        lines.append(f"**Downstream services (dependencies you call):**")
        for d in downstream_impact[:4]:
            lines.append(f"  - **{d['service']}** ({d.get('type','rest')}) — {d['reason']}")

    # ── Events ──
    event_impact = arch_impact.get("event_impact", [])
    if event_impact:
        lines.append(f"**Event contracts touched:**")
        for e in event_impact[:5]:
            if e["type"] == "publishes":
                consumers = e.get("consumers", [])
                sev = e.get("breaking_change_severity", "critical").upper()
                lines.append(
                    f"  - [{sev}] Published `{e['topic']}` schema may have changed "
                    f"— consumers: {', '.join(consumers)}"
                )
            else:
                lines.append(
                    f"  - Consumer for `{e['topic']}` (from **{e.get('from','')}**) changed "
                    f"— verify schema compatibility"
                )

    # ── Sensitive components ──
    sensitive = arch_impact.get("sensitive_components", [])
    if sensitive:
        lines.append(f"**High-sensitivity areas touched:**")
        for s in sensitive[:4]:
            owner_str = f" (owner: {s['owner']})" if s.get("owner") else ""
            sev_icon = "🚨" if s["sensitivity"] == "critical" else "⚠️"
            lines.append(f"  - {sev_icon} `{s['path']}`{owner_str} — {s.get('notes', '')}")

    # ── Custom rule hints ──
    custom_hits = arch_impact.get("custom_rule_hits", [])
    if custom_hits:
        lines.append(f"**Team rules to verify ({len(custom_hits)}):**")
        for r in custom_hits[:5]:
            lines.append(f"  - `{r['rule_id']}` [{r['severity'].upper()}]: {r['description']}")

    if not lines:
        return ""

    # Add service identity header if configured
    svc = arch_config.get("service", {})
    svc_name = svc.get("name", "")
    header = f"### 🏗️ Architectural Impact"
    if svc_name and svc_name != "my-service":
        stack_str = f"{ts.get('language','')}/{ts.get('framework','')}" if ts.get("language") else ""
        header += f" — **{svc_name}**"
        if stack_str:
            header += f" `({stack_str})`"

    return header + "\n\n" + "\n".join(lines)


def post_review(comments: list, routing_report: dict, knowledge: dict) -> None:
    if not comments and not routing_report.get("selected"):
        print("[ReviewCrew] No agents ran — nothing to post.")
        return

    max_comments = knowledge.get("config", {}).get("review", {}).get("max_comments_per_pr", 20)
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
        body += f"\n\n<sub>reviewcrew:{rule_id}:{comment_hash}</sub>"

        review_comments.append({
            "path": c["file"],
            "line": c["line"],
            "body": body,
        })

    payload = {
        "body":     build_review_body(routing_report, knowledge.get("project_context", {})),
        "event":    "COMMENT",
        "comments": review_comments,
    }

    resp = requests.post(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/reviews",
        headers=HEADERS,
        json=payload,
    )

    if resp.status_code in (200, 201):
        print(f"[ReviewCrew] Posted review: {len(review_comments)} inline comments.")
    else:
        print(f"[ReviewCrew] Failed to post review: {resp.status_code} — {resp.text[:200]}")


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
    try:
        metadata_file.write_text(json.dumps(existing, indent=2))
    except OSError as e:
        print(f"[ReviewCrew] Warning: could not write review metadata: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[ReviewCrew] PR #{PR_NUMBER}: {PR_TITLE}")

    knowledge = load_knowledge()
    print(f"[ReviewCrew] Knowledge: {len(knowledge['rules'])} rules loaded")

    diff          = get_pr_diff()
    files_context = get_changed_files()
    print(f"[ReviewCrew] {len(files_context)} changed files fetched")

    # Build deep project context: tech stack, module graph, topology, etc.
    changed_paths = [f["filename"] for f in files_context]
    project_context = build_project_context(Path("."), changed_paths)
    knowledge["project_context"] = project_context

    # Multi-agent orchestrated review
    comments, routing_report = orchestrate(diff, files_context, knowledge)

    # Post to GitHub
    post_review(comments, routing_report, knowledge)

    # Save for feedback tracking + self-improvement
    record_metadata(comments, routing_report)


if __name__ == "__main__":
    main()
