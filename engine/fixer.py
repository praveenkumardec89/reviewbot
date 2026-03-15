"""
ReviewCrew — Auto Fixer
Triggered when a developer comments /reviewcrew fix [scope] on a PR.

Scope options:
  all           Fix every open ReviewCrew comment
  critical      Fix only critical issues
  high          Fix critical + high issues
  security      Fix only security category issues
  <category>    Fix by agent name: code_quality, architecture, performance, etc.

Flow:
  1. Fetch all open ReviewCrew review comments on the PR
  2. Filter by scope
  3. Group by file — fix each file in one Claude call (all issues at once)
  4. Commit fixed files back to the PR branch
  5. Post a summary comment on the PR
"""

import os
import re
import json
import base64
import subprocess
from pathlib import Path
from collections import defaultdict

import requests
from anthropic import Anthropic
from .config import load_model

# ─── Config ───────────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO         = os.environ["REPO"]
PR_NUMBER    = os.environ["PR_NUMBER"]
FIX_SCOPE    = os.environ.get("FIX_SCOPE", "all").strip().lower()

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

BOT_TAG = re.compile(r"<sub>reviewcrew:([^:]+):([^<]+)</sub>")
SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "praise": 0}

AGENT_NAMES = {
    "security", "code_quality", "architecture",
    "simplification", "test_coverage", "performance",
}


# ─── Fetch Comments ───────────────────────────────────────────────────────────

def get_reviewcrew_comments() -> list[dict]:
    """Fetch all ReviewCrew inline review comments on this PR."""
    comments, page = [], 1
    while True:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/comments",
            headers=HEADERS,
            params={"per_page": 100, "page": page},
        )
        if resp.status_code != 200 or not resp.json():
            break
        for c in resp.json():
            match = BOT_TAG.search(c.get("body", ""))
            if not match:
                continue
            comments.append({
                "id":           c["id"],
                "path":         c["path"],
                "line":         c.get("line") or c.get("original_line") or 0,
                "body":         c["body"],
                "rule_id":      match.group(1),
                "severity":     _extract_severity(c["body"]),
                "category":     _extract_category(c["body"]),
                "agent":        _extract_agent(c["body"]),
                "issue_text":   _extract_issue_text(c["body"]),
                "suggested_fix": _extract_suggested_fix(c["body"]),
            })
        page += 1

    print(f"[Fixer] Found {len(comments)} ReviewCrew comments on PR #{PR_NUMBER}")
    return comments


def _extract_severity(body: str) -> str:
    m = re.search(r"\*\*\[(CRITICAL|HIGH|MEDIUM|LOW|PRAISE)\]\*\*", body)
    return m.group(1).lower() if m else "medium"


def _extract_category(body: str) -> str:
    m = re.search(r"\(([^·)]+?)(?:\s*·|\))", body)
    return m.group(1).strip() if m else "general"


def _extract_agent(body: str) -> str:
    for name in AGENT_NAMES:
        if name in body:
            return name
    return ""


def _extract_issue_text(body: str) -> str:
    """Strip the reviewcrew tag and suggestion block — leave the human-readable issue."""
    text = re.sub(r"<sub>reviewcrew:.*?</sub>", "", body, flags=re.DOTALL)
    text = re.sub(r"```suggestion\n.*?\n```", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*\[.*?\]\*\*.*?\n", "", text)  # remove severity header line
    return text.strip()


def _extract_suggested_fix(body: str) -> str:
    m = re.search(r"```suggestion\n(.*?)\n```", body, re.DOTALL)
    return m.group(1).strip() if m else ""


# ─── Scope Filtering ─────────────────────────────────────────────────────────

def filter_by_scope(comments: list[dict], scope: str) -> tuple[list[dict], list[dict]]:
    """
    Returns (to_fix, skipped).
    Scope can be: all, critical, high, medium, or any agent name.
    """
    to_fix, skipped = [], []
    for c in comments:
        if c["severity"] == "praise":
            skipped.append(c)
            continue

        if scope == "all":
            to_fix.append(c)
        elif scope in SEVERITY_RANK:
            # Fix this severity level and above
            if SEVERITY_RANK.get(c["severity"], 0) >= SEVERITY_RANK[scope]:
                to_fix.append(c)
            else:
                skipped.append(c)
        elif scope in AGENT_NAMES or scope in {"security", "performance", "style", "bug", "architecture", "test"}:
            # Match by agent name or category
            if scope in c.get("agent", "") or scope in c.get("category", ""):
                to_fix.append(c)
            else:
                skipped.append(c)
        else:
            to_fix.append(c)  # unknown scope → fix all

    print(f"[Fixer] Scope '{scope}': fixing {len(to_fix)}, skipping {len(skipped)}")
    return to_fix, skipped


# ─── File Operations ─────────────────────────────────────────────────────────

def get_pr_head_ref() -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
        headers=HEADERS,
    )
    resp.raise_for_status()
    return resp.json()["head"]["ref"]


def read_file(path: str) -> str | None:
    """Read current file content from disk (already checked out)."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"[Fixer] File not found on disk: {path}")
        return None


def write_file(path: str, content: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content, encoding="utf-8")


# ─── Claude Fix Engine ────────────────────────────────────────────────────────

def fix_file(file_path: str, file_content: str, file_comments: list[dict]) -> str | None:
    """
    Call Claude once per file with all issues — returns fixed file content.
    One call covers all issues to avoid inconsistent partial fixes.
    """
    client = Anthropic()

    issues_text = ""
    for i, c in enumerate(file_comments, 1):
        issues_text += f"\nISSUE {i} — Line {c['line']} [{c['severity'].upper()}] ({c['category']})\n"
        issues_text += f"{c['issue_text']}\n"
        if c.get("suggested_fix"):
            issues_text += f"Suggested replacement:\n{c['suggested_fix']}\n"

    prompt = f"""You are an expert software engineer applying code review fixes.

FILE: {file_path}

CURRENT FILE CONTENT:
```
{file_content}
```

ISSUES TO FIX (apply ALL of them):
{issues_text}

STRICT RULES:
1. Fix every issue listed — do not skip any
2. Make MINIMAL changes — only touch the lines that need fixing
3. Preserve all formatting, indentation, blank lines, and comments
4. Do NOT refactor, rename, or improve anything not mentioned
5. Return ONLY the complete corrected file content
6. No markdown, no code fences, no explanation — raw file content only"""

    response = client.messages.create(
        model=load_model(),
        max_tokens=8096,
        messages=[{"role": "user", "content": prompt}],
    )

    fixed = response.content[0].text.strip()

    # Strip accidental markdown fences
    if fixed.startswith("```"):
        lines = fixed.split("\n")
        fixed = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    return fixed if fixed != file_content else None


# ─── Git Commit ───────────────────────────────────────────────────────────────

def commit_and_push(fixed_files: list[str], scope: str) -> None:
    subprocess.run(["git", "config", "user.name", "ReviewCrew"], check=True)
    subprocess.run(["git", "config", "user.email", "reviewcrew@noreply.github.com"], check=True)
    subprocess.run(["git", "add"] + fixed_files, check=True)

    n = len(fixed_files)
    scope_label = f"fix {scope}" if scope != "all" else "fix all issues"
    msg = (
        f"fix: apply ReviewCrew suggestions ({scope_label})\n\n"
        f"Auto-fixed {n} file(s) based on ReviewCrew review comments.\n"
        f"Triggered by: /reviewcrew fix {scope}"
    )
    subprocess.run(["git", "commit", "-m", msg], check=True)
    subprocess.run(["git", "push"], check=True)
    print(f"[Fixer] Committed and pushed fixes for {n} file(s)")


# ─── Summary Comment ─────────────────────────────────────────────────────────

def post_summary(fixed: list[dict], skipped: list[dict], scope: str) -> None:
    lines = [
        f"## 🔧 ReviewCrew Auto-Fix Complete\n",
        f"Applied **{len(fixed)}** fix(es) matching scope `{scope}`\n",
    ]

    if fixed:
        # Group by file for cleaner display
        by_file: dict[str, list] = defaultdict(list)
        for c in fixed:
            by_file[c["path"]].append(c)

        lines.append("### ✅ Fixed\n")
        for path, cs in by_file.items():
            lines.append(f"**`{path}`**")
            for c in cs:
                lines.append(f"  - Line {c['line']} `[{c['severity'].upper()}]` {c['category']}")
        lines.append("")

    if skipped:
        severity_skipped = [c for c in skipped if c["severity"] != "praise"]
        if severity_skipped:
            lines.append(f"### ⏭️ Skipped ({len(severity_skipped)} out of scope)\n")
            for c in severity_skipped[:5]:
                lines.append(f"  - `{c['path']}` line {c['line']} `[{c['severity'].upper()}]` — use `/reviewcrew fix all` to include")
            if len(severity_skipped) > 5:
                lines.append(f"  - ...and {len(severity_skipped) - 5} more")
            lines.append("")

    lines.append("---")
    lines.append("_Review the committed changes above before merging._")

    requests.post(
        f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments",
        headers=HEADERS,
        json={"body": "\n".join(lines)},
    )


def post_nothing_to_fix(scope: str) -> None:
    requests.post(
        f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments",
        headers=HEADERS,
        json={"body": f"🔧 **ReviewCrew**: No open issues match scope `{scope}`. Nothing to fix."},
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[Fixer] PR #{PR_NUMBER} — scope: '{FIX_SCOPE}'")

    all_comments = get_reviewcrew_comments()
    if not all_comments:
        post_nothing_to_fix(FIX_SCOPE)
        return

    to_fix, skipped = filter_by_scope(all_comments, FIX_SCOPE)
    if not to_fix:
        post_nothing_to_fix(FIX_SCOPE)
        return

    # Group by file
    by_file: dict[str, list] = defaultdict(list)
    for c in to_fix:
        by_file[c["path"]].append(c)

    # Fix each file
    fixed_files, fixed_comments = [], []
    for file_path, file_comments in by_file.items():
        print(f"[Fixer] {file_path}: applying {len(file_comments)} fix(es)...")
        content = read_file(file_path)
        if content is None:
            continue

        fixed_content = fix_file(file_path, content, file_comments)
        if fixed_content:
            write_file(file_path, fixed_content)
            fixed_files.append(file_path)
            fixed_comments.extend(file_comments)
            print(f"[Fixer] {file_path}: fixed")
        else:
            print(f"[Fixer] {file_path}: no changes produced")

    if not fixed_files:
        post_nothing_to_fix(FIX_SCOPE)
        return

    commit_and_push(fixed_files, FIX_SCOPE)
    post_summary(fixed_comments, skipped, FIX_SCOPE)
    print(f"[Fixer] Done — {len(fixed_comments)} issue(s) fixed across {len(fixed_files)} file(s)")


if __name__ == "__main__":
    main()
