"""
ReviewCrew Orchestrator — Routes PRs to specialized agents and runs them in parallel.

Flow:
  1. Analyze PR context (file types, size, patterns) — no API call
  2. Each agent's should_run() decides relevance — no API call
  3. Selected agents run in parallel via ThreadPoolExecutor
  4. Results are deduplicated and sorted by severity
  5. Routing summary is returned for transparency in the review body
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from .agents import ALL_AGENTS

# Code file extensions that warrant code-focused agents
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb",
    ".php", ".cs", ".cpp", ".c", ".rs", ".kt", ".swift", ".scala",
}

SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "praise": 1}


# ─── PR Context Analysis ──────────────────────────────────────────────────────

def analyze_pr_context(diff: str, files_context: list) -> dict:
    """
    Extract PR characteristics from the diff and file list.
    This is purely local — no API calls. Used by agents' should_run().
    """
    extensions = set()
    directories = set()
    total_additions = 0
    total_deletions = 0
    file_names = []
    new_files = []

    for f in files_context:
        path = f["filename"]
        file_names.append(path)
        ext = Path(path).suffix.lower()
        extensions.add(ext)

        parts = path.split("/")
        if len(parts) > 1:
            directories.add(parts[0])

        total_additions += f.get("additions", 0)
        total_deletions += f.get("deletions", 0)

        if f.get("status") == "added":
            new_files.append(path)

    diff_lower = diff.lower()

    return {
        "extensions": extensions,
        "directories": directories,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "file_count": len(files_context),
        "file_names": file_names,
        "new_files": new_files,
        "has_new_files": len(new_files) > 0,
        "has_code": bool(extensions & CODE_EXTENSIONS),
        "has_tests": any(
            "test" in f.lower() or "spec" in f.lower() for f in file_names
        ),
        "has_config": any(
            ext in {".yaml", ".yml", ".json", ".toml", ".env", ".ini", ".cfg"}
            for ext in extensions
        ),
        "has_sql": any(
            ".sql" in f.lower() or "migration" in f.lower() for f in file_names
        ),
        "is_large_pr": total_additions > 200 or len(files_context) > 10,
        "diff": diff_lower,  # lowercased for pattern matching in should_run()
    }


# ─── Agent Routing ────────────────────────────────────────────────────────────

def route_agents(pr_context: dict) -> tuple[list, list]:
    """
    Ask each agent whether it should run on this PR.
    Returns (selected_agents, skipped_names).
    No API calls — pure local routing logic.
    """
    selected = []
    skipped = []

    for agent in ALL_AGENTS:
        should_run, reason = agent.should_run(pr_context)
        if should_run:
            selected.append(agent)
            print(f"[Orchestrator] {agent.emoji} {agent.name}: SELECTED — {reason}")
        else:
            skipped.append(agent.name)
            print(f"[Orchestrator]    {agent.name}: skipped — {reason}")

    return selected, skipped


# ─── Parallel Execution ───────────────────────────────────────────────────────

def _run_agent(agent, diff: str, files_context: list, knowledge: dict) -> tuple[str, list]:
    """Run one agent, return (name, comments). Safe — catches exceptions."""
    try:
        print(f"[{agent.name}] Starting...")
        comments = agent.review(diff, files_context, knowledge)
        print(f"[{agent.name}] Done — {len(comments)} findings")
        return agent.name, comments
    except Exception as exc:
        print(f"[{agent.name}] ERROR: {exc}")
        return agent.name, []


def run_agents_parallel(
    agents: list, diff: str, files_context: list, knowledge: dict
) -> dict[str, list]:
    """Run all selected agents concurrently. Returns {agent_name: [comments]}."""
    results = {}
    max_workers = min(len(agents), 6)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_agent, agent, diff, files_context, knowledge): agent
            for agent in agents
        }
        for future in as_completed(futures):
            name, comments = future.result()
            results[name] = comments

    return results


# ─── Result Aggregation ───────────────────────────────────────────────────────

def deduplicate_and_sort(all_comments: list) -> list:
    """
    Merge comments from all agents.
    - Deduplicate by (file, line): keep highest severity at each location
    - Sort: critical → high → medium → low → praise
    """
    best: dict[tuple, dict] = {}

    for comment in all_comments:
        file = comment.get("file", "")
        line = comment.get("line", 0)
        key = (file, line)

        if key not in best:
            best[key] = comment
        else:
            existing_rank = SEVERITY_RANK.get(best[key].get("severity", "low"), 2)
            new_rank = SEVERITY_RANK.get(comment.get("severity", "low"), 2)
            if new_rank > existing_rank:
                best[key] = comment

    sorted_comments = sorted(
        best.values(),
        key=lambda c: SEVERITY_RANK.get(c.get("severity", "low"), 2),
        reverse=True,
    )
    return sorted_comments


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def orchestrate(
    diff: str, files_context: list, knowledge: dict
) -> tuple[list, dict]:
    """
    Full orchestration pipeline.

    Returns:
        comments       — deduplicated, sorted list of review comments
        routing_report — summary dict for inclusion in the review body
    """
    print(f"[Orchestrator] Analyzing PR: {len(files_context)} files, "
          f"{sum(f.get('additions', 0) for f in files_context)} additions")

    # 1. Characterize the PR (merge in project context if available)
    pr_context = analyze_pr_context(diff, files_context)
    if "project_context" in knowledge:
        pr_context["project_context"] = knowledge["project_context"]

    # 2. Route agents
    selected_agents, skipped_agents = route_agents(pr_context)

    if not selected_agents:
        print("[Orchestrator] No agents selected — nothing to review")
        return [], {"selected": [], "skipped": skipped_agents, "per_agent": {}}

    print(f"[Orchestrator] Running {len(selected_agents)} agents in parallel: "
          f"{[a.name for a in selected_agents]}")

    # 3. Run in parallel
    agent_results = run_agents_parallel(selected_agents, diff, files_context, knowledge)

    # 4. Merge
    all_comments = [c for comments in agent_results.values() for c in comments]
    final_comments = deduplicate_and_sort(all_comments)

    routing_report = {
        "selected": [a.name for a in selected_agents],
        "skipped": skipped_agents,
        "per_agent": {name: len(comments) for name, comments in agent_results.items()},
        "total_raw": len(all_comments),
        "total_final": len(final_comments),
    }

    print(
        f"[Orchestrator] {len(all_comments)} raw → {len(final_comments)} after dedup. "
        f"Breakdown: {routing_report['per_agent']}"
    )

    return final_comments, routing_report
