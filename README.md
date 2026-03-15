# ReviewCrew 🤖

**A self-learning AI code review platform powered by multiple specialized agents.**

ReviewCrew uses Claude to review pull requests through a team of focused agents — each an expert in its domain. It learns from your team's feedback to retire noisy rules, boost effective ones, and evolve its knowledge automatically.

---

## How It Works

```
PR Opened
    │
    ▼
┌─────────────────────────────────────────────────────┐
│                   ORCHESTRATOR                       │
│  Analyzes PR (file types, size, patterns)           │
│  Decides which agents are relevant — no wasted calls │
└──────────┬──────────────────────────────────────────┘
           │  runs selected agents in parallel
           ▼
┌──────────────────────────────────────────────────────────────────┐
│  🔒 Security  ✨ Code Quality  🏗️ Architecture  🔧 Simplification │
│  🧪 Test Coverage  ⚡ Performance                                 │
│                                                                  │
│  Each agent: focused system prompt + domain expertise            │
│  All run concurrently via ThreadPoolExecutor                     │
└──────────┬───────────────────────────────────────────────────────┘
           │  results merged + deduplicated
           ▼
    Unified PR Review
    (inline comments tagged by agent + severity)
           │
           │  team reacts (👍/👎, resolve/dismiss)
           ▼
    ┌─────────────────────────────┐
    │   .reviewcrew/ knowledge     │
    │   rules.yaml  scores.json   │◄── weekly self-improvement
    │   patterns.json  infra.yaml │    raises PR with rule changes
    └─────────────────────────────┘
```

---

## The Agent Team

| Agent | Emoji | Triggers When | Focuses On |
|-------|-------|---------------|------------|
| **Security** | 🔒 | Every PR | Secrets, injection, auth, OWASP Top 10, crypto |
| **Code Quality** | ✨ | Code files present | Error handling, null safety, complexity, naming |
| **Architecture** | 🏗️ | Large PRs, new files, API changes | SOLID, coupling, API contracts, dependencies |
| **Simplification** | 🔧 | 30+ lines added | DRY, YAGNI, over-engineering, dead abstractions |
| **Test Coverage** | 🧪 | Production code changed | Missing tests, edge cases, test quality |
| **Performance** | ⚡ | DB/loop/query patterns detected | N+1 queries, complexity, memory, caching |

### Smart Routing — No Wasted API Calls

The orchestrator analyzes the PR before running any agents:

```
Small config change (5 lines)?
  → Only Security runs

New service file (300 lines, API routes)?
  → All 6 agents run in parallel

Pure test-file PR?
  → Security + Code Quality only (others skip)

SQL migration file?
  → Security + Performance + Architecture
```

Each agent's `should_run()` check is instant — no API call. Agents only run when they can add value.

### What the Review Looks Like

```
## 🤖 ReviewCrew Multi-Agent Review

4 findings from 3 specialized agents

  🔒 security:      2 findings
  ✨ code_quality:  1 finding
  ⚡ performance:   1 finding
  ~~architecture~~  (skipped — PR too small)
  ~~simplification~~ (skipped — PR too small)
  ~~test_coverage~~  (skipped — only test files changed)
```

Inline comments are tagged by agent and severity:

```
🔒 [CRITICAL] (security · 🔒 security)
Hardcoded API key on line 12. Move to environment variable.

⚠️ [HIGH] (bug · ✨ code_quality)
getUserById() can return null but is dereferenced without a null check on line 34.

⚡ [MEDIUM] (performance · ⚡ performance)
N+1 query: getUser() called inside a loop. Fetch all users once before the loop.
```

---

## Auto-Fix: One Command to Apply All Review Comments

Once ReviewCrew has reviewed your PR, a developer can comment on the PR to automatically apply fixes:

```
/reviewcrew fix all        → fix every open ReviewCrew comment
/reviewcrew fix critical   → fix only critical issues
/reviewcrew fix high       → fix critical + high severity
/reviewcrew fix security   → fix only security findings
/reviewcrew fix code_quality   → fix by agent name
/reviewcrew fix architecture
/reviewcrew fix performance
```

### How It Works

```
Developer comments "/reviewcrew fix critical"
    │
    ▼
GitHub Actions detects issue_comment event
    │
    ▼
Fixer fetches all open ReviewCrew inline comments
    │  (identified by hidden tag in each comment body)
    ▼
Filters by scope (critical = critical issues only)
    │
    ▼
Groups remaining comments by file
    │
    ▼
Claude applies ALL issues per file in one call
    │  (avoids inconsistent partial fixes)
    ▼
Fixer commits fixed files to the PR branch
    │
    ▼
Posts summary comment:
    ✅ Fixed: src/auth/login.py (2 issues)
    ✅ Fixed: src/db/queries.py (1 issue)
    ⏭️ Skipped: 3 low-severity (use /reviewcrew fix all)
```

### Fix Rules

1. **Minimal changes** — only the flagged lines are touched
2. **Preserves formatting** — indentation, blank lines, comments unchanged
3. **One Claude call per file** — all issues in a file fixed together for consistency
4. **Commit message** includes scope and which files were fixed
5. **Review before merging** — the fixer always posts a summary; you control the merge

---

## The Learning Loop

1. **Agents post comments** tagged with rule IDs and agent names
2. **Your team reacts** — 👍 (useful), 👎 (noise), resolve (fixed it), dismiss (not applicable)
3. **Signals accumulate** — feedback scores per rule, revert patterns, merge velocity
4. **Weekly self-improvement** — ReviewCrew analyzes all signals:
   - 🗑️ **Retires** rules consistently getting 👎 or dismissed
   - ⬆️ **Boosts** rules consistently getting 👍 or resolved
   - ✨ **Creates** new rules from repeated review patterns
   - 🛡️ **Creates guard rules** from reverted PRs
5. **Raises a PR** to `.reviewcrew/` — your team reviews and merges

### Feedback Scoring

| Signal | Score | Meaning |
|--------|-------|---------|
| 👍 on comment | +1 | Agent was right |
| 👎 on comment | -2 | Agent was wrong |
| Comment resolved by author | +2 | Useful — author fixed it |
| Comment dismissed | -3 | Noise |
| PR approved after review | +1 | Review led to improvements |
| PR reverted after merge | -2 per rule | Review missed something critical |

Rules below **-10** (5+ samples) are **retired**. Rules above **+10** are **boosted** to higher severity.

---

## Quick Start (5 minutes)

### Prerequisites

- GitHub repo with Actions enabled
- [Anthropic API key](https://console.anthropic.com/)

### Step 1: Initialize the knowledge store

```bash
bash scripts/setup.sh
```

Or manually:

```bash
mkdir -p .reviewcrew/history
cp templates/default-rules.yaml .reviewcrew/rules.yaml
cp templates/default-config.yaml .reviewcrew/config.yaml
echo '{}' > .reviewcrew/patterns.json
echo '{}' > .reviewcrew/scores.json
echo '{}' > .reviewcrew/infra.yaml
echo '[]' > .reviewcrew/history/improvements.json
```

### Step 2: Add the workflow to your repo

Copy `templates/consumer-workflow.yml` to `.github/workflows/reviewcrew.yml`, replacing `YOUR_ORG` with `praveenkumardec89`:

```yaml
name: ReviewCrew
on:
  pull_request:
    types: [opened, synchronize, reopened, closed]
  pull_request_review:
    types: [submitted]
  issues:
    types: [labeled]
  schedule:
    - cron: '0 2 * * 0'   # Weekly self-improvement

jobs:
  ai-review:
    if: >
      github.event_name == 'pull_request' &&
      (github.event.action == 'opened' || github.event.action == 'synchronize')
    uses: praveenkumardec89/reviewcrew/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

  track-feedback:
    if: github.event_name == 'pull_request_review'
    uses: praveenkumardec89/reviewcrew/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

  self-improve:
    if: github.event_name == 'schedule'
    uses: praveenkumardec89/reviewcrew/.github/workflows/self-improve.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

### Step 3: Add your API key

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `ANTHROPIC_API_KEY`
- Value: Your key from [console.anthropic.com](https://console.anthropic.com/)

### Step 4: Push and go

```bash
git add .reviewcrew/ .github/workflows/reviewcrew.yml
git commit -m "Setup ReviewCrew multi-agent code review"
git push
```

ReviewCrew activates on your next PR.

---

## Customization

### Disable specific agents

In `.reviewcrew/config.yaml`:

```yaml
agents:
  security: true          # Strongly recommend keeping this on
  code_quality: true
  architecture: false     # Disable if your team handles design reviews manually
  simplification: true
  test_coverage: false    # Disable if you have a dedicated QA process
  performance: true
```

### Tune routing thresholds

```yaml
routing:
  architecture_min_additions: 100   # Only run on PRs > 100 lines added
  simplification_min_additions: 50
  performance_require_patterns: true
```

### Add custom rules (used by all agents)

Edit `.reviewcrew/rules.yaml`:

```yaml
- id: use_structured_logging
  description: Always use our logger module, never print() or console.log()
  severity: medium
  category: style
  scope: "**/*.{py,js,ts}"
  example: "print('error') → logger.error('payment_failed', user_id=uid)"
```

### Add component knowledge

Edit `.reviewcrew/infra.yaml` — agents use this for context-aware reviews:

```yaml
src/payments/:
  owner: payments-team
  notes: PCI-DSS zone — extra security scrutiny required
  dependencies: [stripe-sdk, vault]

src/auth/:
  owner: security-team
  notes: OAuth2 + JWT — any changes need security team review
```

---

## Architecture

```
engine/
├── reviewer.py             # Entry point: loads knowledge, fetches PR, posts review
├── orchestrator.py         # Routes PR to agents, runs in parallel, merges results
├── fixer.py                # Auto-fix: applies review comments on /reviewcrew fix command
└── agents/
    ├── base.py             # BaseAgent: should_run() + review() contract
    ├── security.py         # 🔒 OWASP, secrets, injection, auth
    ├── code_quality.py     # ✨ Error handling, null safety, complexity
    ├── architecture.py     # 🏗️ SOLID, API design, coupling
    ├── simplification.py   # 🔧 DRY, YAGNI, dead abstractions
    ├── test_coverage.py    # 🧪 Missing tests, edge cases
    └── performance.py      # ⚡ N+1, complexity, memory, caching

engine/
├── feedback_collector.py   # Tracks 👍/👎 reactions → rule scores
├── revert_tracker.py       # Detects PR reverts → guard rules
├── signal_aggregator.py    # Weekly: collects all learning signals
├── self_improver.py        # Weekly: proposes rule changes via PR
└── build_fixer.py          # Analyzes CI failures → fix PRs
```

### Orchestration Flow (simplified)

```python
# 1. Characterize the PR — no API calls
pr_context = analyze_pr_context(diff, files_context)
# → {extensions, file_count, total_additions, has_code, has_sql, ...}

# 2. Route — each agent's should_run() inspects pr_context
selected, skipped = route_agents(pr_context)

# 3. Run all selected agents concurrently
results = run_agents_parallel(selected, diff, files_context, knowledge)

# 4. Merge: (file, line) dedup keeps highest severity; sort critical→praise
comments = deduplicate_and_sort(results)
```

---

## Knowledge Store

```
.reviewcrew/
├── config.yaml          ← Edit: model, agents, routing thresholds
├── rules.yaml           ← Edit: review rules (auto-evolved + manual)
├── infra.yaml           ← Edit: component/team knowledge
├── patterns.json        ← Auto: learned code patterns
├── scores.json          ← Auto: feedback scores per rule
└── history/
    ├── reviews.json     ← Auto: full review audit log (with agent breakdown)
    ├── feedback.json    ← Auto: feedback signals
    ├── reverts.json     ← Auto: revert tracking
    └── improvements.json ← Auto: self-improvement history
```

**Edit freely:** `config.yaml`, `rules.yaml`, `infra.yaml`

**Auto-managed:** `patterns.json`, `scores.json`, `history/*`

---

## Self-Improvement PR Example

Every Sunday (or on manual trigger), ReviewCrew raises:

> ### 🤖 ReviewCrew: Self-improvement update (2026-03-15)
>
> **Analysis period:** Last 14 days | **PRs analyzed:** 23
>
> #### 🗑️ Rules Retired
> - `style_semicolons` (code_quality agent): Score -14 over 8 samples
>
> #### ⬆️ Rules Boosted
> - `error_handling` (code_quality agent): Score +18 over 12 samples
>
> #### ✨ New Rules Created
> - `auto_src_api_security`: Security agent flagged recurring issues in `src/api/` (5×)
> - `revert_guard_src_payments`: Multiple reverts in `src/payments/` — guard rule added
>
> *Auto-generated. Review before merging.*

---

## FAQ

**Q: Does ReviewCrew review its own self-improvement PRs?**
No — PRs from `reviewcrew/*` branches are skipped automatically.

**Q: What if I want every agent to always run?**
Set all `routing.*` thresholds to `0` in `.reviewcrew/config.yaml`.

**Q: Can I manually trigger a review or self-improvement?**
Yes — Actions → ReviewCrew → Run workflow → select the action.

**Q: What does it cost?**
With smart routing, a typical PR triggers 3–4 agents at ~1K–3K tokens each (Claude Sonnet). For a team doing 20 PRs/week, expect ~$8–20/month.

**Q: Can I use a different Claude model?**
Yes — change `model` in `config.yaml`. Use `claude-opus-4-6` for deeper analysis on critical paths.

**Q: Why multiple focused agents instead of one large prompt?**
Focused agents are more accurate — a security specialist catches subtle auth issues that a general reviewer misses. Focused prompts also reduce hallucinations. Parallel execution means no added latency vs. a single call.

---

## Roadmap

- [ ] Agent-level feedback scoring (which agent's comments are most useful)
- [ ] Slack/Discord notifications for improvement PRs
- [ ] Web dashboard for per-agent effectiveness
- [ ] GitLab CI/CD support
- [ ] Multi-repo shared knowledge (org-level rules)
- [ ] Custom agent definitions via `.reviewcrew/agents/`
- [ ] PR complexity scoring and auto-assignment

---

## Contributing

PRs welcome. See the roadmap for priority areas.

To add a new agent:

```bash
# 1. Copy an existing agent as template
cp engine/agents/performance.py engine/agents/my_agent.py

# 2. Implement should_run() and build_system_prompt()

# 3. Register it
echo "from .my_agent import MyAgent" >> engine/agents/__init__.py
# Add MyAgent() to ALL_AGENTS in engine/agents/__init__.py
```

---

## License

MIT — use it, fork it, make it yours.

Built by [@praveenkumardec89](https://github.com/praveenkumardec89)
