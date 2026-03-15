# ReviewBot 🤖

**A self-learning AI code review platform that gets smarter with every PR.**

ReviewBot uses Claude to review pull requests, fix build failures, and — most importantly — **learns from your team's feedback to improve itself automatically**. It raises PRs to update its own rules based on what your team finds helpful vs. noisy.

---

## Why ReviewBot?

Most AI code review tools are static — they apply the same rules forever. ReviewBot is different:

| Feature | Static Tools | ReviewBot |
|---------|-------------|-----------|
| Reviews PRs | ✅ | ✅ |
| Learns from feedback | ❌ | ✅ 👍/👎 reactions, resolved/dismissed comments |
| Detects reverted PRs | ❌ | ✅ Creates "never miss" rules automatically |
| Fixes build failures | ❌ | ✅ Analyzes CI logs and suggests/creates fix PRs |
| Evolves its own rules | ❌ | ✅ Raises PRs to `.reviewbot/` with rule changes |
| Repo-specific knowledge | ❌ | ✅ Learns your infra, components, and patterns |
| Human-in-the-loop | N/A | ✅ All rule changes require team approval |

---

## How It Works

```
  PR Opened ──────► Review Engine (Claude API) ──────► Inline Comments
                          │                                   │
                          │ reads                    team reacts (👍/👎)
                          ▼                                   │
                   .reviewbot/                                │
                   ├── rules.yaml ◄───── Self-Improvement ◄──┘
                   ├── patterns.json         Engine
                   ├── scores.json        (weekly cron)
                   └── infra.yaml              │
                                               ▼
                                     Raises PR with updated rules
                                     (human reviews & merges)
```

### The Learning Loop

1. **ReviewBot posts comments** on your PR, tagged with rule IDs for tracking
2. **Your team reacts** — 👍 (useful), 👎 (noise), resolve (fixed it), dismiss (not applicable)
3. **Signals accumulate** — reactions, PR reverts, build failures, merge velocity
4. **Weekly self-improvement** — ReviewBot analyzes all signals and:
   - 🗑️ **Retires** rules that consistently get 👎 or dismissed
   - ⬆️ **Boosts** rules that consistently get 👍 or resolved
   - ✨ **Creates** new rules from repeated review patterns
   - 🛡️ **Creates guard rules** from reverted PRs (so it never misses them again)
5. **Raises a PR** to `.reviewbot/` — your team reviews and merges the rule changes

---

## Quick Start (5 minutes)

### Prerequisites

- GitHub repo with Actions enabled
- [Anthropic API key](https://console.anthropic.com/) (Claude API)

### Step 1: Create the knowledge store

Create a `.reviewbot/` directory in your repo root:

```bash
mkdir -p .reviewbot/history
```

Create `.reviewbot/config.yaml`:

```yaml
model: claude-sonnet-4-20250514

review:
  auto_review: true
  severity_threshold: low     # low | medium | high | critical
  max_comments_per_pr: 15
  review_tests: true
  include_praise: true
  skip_paths:
    - "**/*.lock"
    - "**/node_modules/**"
    - "**/vendor/**"

learning:
  enabled: true
  min_feedback_samples: 5
  auto_create_rules: true
  auto_retire_rules: true
  require_approval: true      # Rule change PRs need human approval

build_fixer:
  enabled: true
  auto_fix_pr: true
  confidence_threshold: medium
```

Create `.reviewbot/rules.yaml` (starter rules — these will evolve):

```yaml
- id: security_secrets
  description: Flag hardcoded secrets, API keys, tokens, or passwords in code
  severity: critical
  category: security
  scope: "**/*"

- id: error_handling
  description: Functions that can throw should have proper error handling
  severity: high
  category: bug
  scope: "**/*.{ts,js,py,java,go}"

- id: null_checks
  description: Dereferences of potentially null/undefined values without null checks
  severity: high
  category: bug
  scope: "**/*.{ts,js,java}"

- id: sql_injection
  description: String concatenation in SQL queries instead of parameterized queries
  severity: critical
  category: security
  scope: "**/*.{py,js,ts,java}"

- id: missing_tests
  description: New public functions should have corresponding test coverage
  severity: medium
  category: test
  scope: "src/**/*"

- id: large_function
  description: Functions longer than 80 lines should be broken into smaller units
  severity: medium
  category: architecture
  scope: "**/*"

- id: breaking_api
  description: Changes to public API signatures should be backward compatible or versioned
  severity: high
  category: architecture
  scope: "src/api/**/*"

- id: logging_sensitive
  description: Never log PII, tokens, passwords, or other sensitive data
  severity: critical
  category: security
  scope: "**/*"
```

Create empty tracking files:

```bash
echo '{}' > .reviewbot/patterns.json
echo '{}' > .reviewbot/scores.json
echo '{}' > .reviewbot/infra.yaml
echo '[]' > .reviewbot/history/improvements.json
```

### Step 2: Add the GitHub Actions workflow

Create `.github/workflows/reviewbot.yml`:

```yaml
name: ReviewBot
on:
  pull_request:
    types: [opened, synchronize, reopened, closed]
  pull_request_review:
    types: [submitted]
  pull_request_review_comment:
    types: [created]
  issues:
    types: [labeled]
  schedule:
    - cron: '0 2 * * 0'  # Weekly self-improvement (Sunday 2 AM)
  workflow_dispatch:
    inputs:
      action:
        description: 'Manual trigger'
        type: choice
        options:
          - self-improve
          - re-review

jobs:
  ai-review:
    if: >
      github.event_name == 'pull_request' &&
      (github.event.action == 'opened' || github.event.action == 'synchronize')
    uses: praveenkumardec89/reviewbot/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

  track-feedback:
    if: github.event_name == 'pull_request_review'
    uses: praveenkumardec89/reviewbot/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

  detect-revert:
    if: >
      github.event_name == 'pull_request' &&
      github.event.action == 'closed' &&
      github.event.pull_request.merged == true &&
      contains(github.event.pull_request.title, 'Revert')
    uses: praveenkumardec89/reviewbot/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

  self-improve:
    if: >
      github.event_name == 'schedule' ||
      (github.event_name == 'workflow_dispatch' && github.event.inputs.action == 'self-improve')
    uses: praveenkumardec89/reviewbot/.github/workflows/self-improve.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

  build-fixer:
    if: >
      github.event_name == 'issues' &&
      contains(github.event.issue.labels.*.name, 'build-failure')
    uses: praveenkumardec89/reviewbot/.github/workflows/self-improve.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

### Step 3: Add your API key

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name: `ANTHROPIC_API_KEY`
- Value: Your Anthropic API key from [console.anthropic.com](https://console.anthropic.com/)

### Step 4: Push and go

```bash
git add .reviewbot/ .github/workflows/reviewbot.yml
git commit -m "Setup ReviewBot self-learning code review"
git push
```

ReviewBot will activate on your next PR.

---

## Learning Signals & Scoring

ReviewBot tracks these signals to decide which rules are working:

| Signal | Score | Meaning |
|--------|-------|---------|
| 👍 reaction on comment | +1 | Team agrees with the review |
| 👎 reaction on comment | -2 | Team disagrees |
| Comment resolved by author | +2 | Comment was useful, author fixed it |
| Comment dismissed | -3 | Comment was noise or wrong |
| PR approved after review changes | +1 | Review led to improvements |
| PR reverted after merge | -5 | Review missed something critical |
| Same pattern flagged 3+ times | — | Triggers new rule creation |

Rules with a score below **-10** (with 5+ samples) get **retired**.
Rules with a score above **+10** (with 5+ samples) get **boosted** to higher severity.

---

## Customization

### Adding custom rules

Edit `.reviewbot/rules.yaml`:

```yaml
- id: my_team_rule
  description: Always use structured logging via our logger module, never print()
  severity: medium
  category: style
  scope: "**/*.py"
  example: "print('error') → logger.error('payment_failed', user_id=uid)"
```

### Adding component knowledge

Edit `.reviewbot/infra.yaml` to give ReviewBot context about your architecture:

```yaml
src/payments/:
  owner: payments-team
  notes: PCI-DSS compliant zone — extra security scrutiny needed
  dependencies: [stripe-sdk, vault]

src/auth/:
  owner: security-team
  notes: OAuth2 + JWT — any changes need security review

src/data-pipeline/:
  owner: data-eng
  notes: Runs on Spark, processes 10M+ events/day — watch for performance
```

### Adjusting learning sensitivity

In `.reviewbot/config.yaml`:

```yaml
learning:
  min_feedback_samples: 10    # More samples before adjusting (conservative)
  auto_create_rules: false    # Don't auto-create, only retire/boost
  require_approval: true      # Always require human approval for changes
```

---

## Knowledge Store Structure

```
.reviewbot/
├── config.yaml              # Your configuration (edit freely)
├── rules.yaml               # Review rules — auto-evolved + manual (edit freely)
├── patterns.json             # Learned code patterns (auto-managed)
├── scores.json               # Feedback scores per rule (auto-managed)
├── infra.yaml                # Component/infra knowledge (edit freely)
└── history/
    ├── reviews.json          # Review audit log
    ├── feedback.json         # Feedback signal log
    ├── reverts.json          # Revert tracking log
    ├── build_fixes.json      # Build fix log
    └── improvements.json     # Self-improvement history
```

**Files you should edit:** `config.yaml`, `rules.yaml`, `infra.yaml`

**Files managed by ReviewBot:** `patterns.json`, `scores.json`, `history/*`

---

## Architecture

The engine consists of 5 Python modules:

| Module | Trigger | What it does |
|--------|---------|-------------|
| `reviewer.py` | PR opened/updated | Reads knowledge store, analyzes diff with Claude, posts review comments |
| `feedback_collector.py` | Review submitted | Tracks 👍/👎 reactions, resolved/dismissed comments, updates scores |
| `revert_tracker.py` | Revert PR merged | Detects reverts, applies strong penalties, records patterns |
| `signal_aggregator.py` | Weekly cron | Collects all signals — merge velocity, patterns, component quality |
| `self_improver.py` | Weekly cron | Analyzes signals, generates rule changes, creates improvement PRs |
| `build_fixer.py` | Issue labeled `build-failure` | Analyzes CI logs, suggests fixes, creates fix PRs |

All modules share the `.reviewbot/` knowledge store. The review engine reads it for context; the feedback/revert/signal modules write to it; the self-improver reads everything and proposes changes via PR.

---

## How the Self-Improvement PR Looks

Every week (or on manual trigger), ReviewBot raises a PR like this:

> ### 🤖 ReviewBot: Self-improvement update (2026-03-15)
>
> **Analysis period:** Last 14 days | **PRs analyzed:** 23
>
> #### 🗑️ Rules Retired
> - `style_semicolons`: Score -14 over 8 samples — team consistently dismissed
>
> #### ⬆️ Rules Boosted
> - `error_handling`: Score +18 over 12 samples — consistently useful
>
> #### ✨ New Rules Created
> - `auto_src_api_security`: Recurring security issues in `src/api/` (flagged 5 times)
> - `revert_guard_src_payments`: Reverts detected in `src/payments/` — guard rule added
>
> *This PR was auto-generated. Please review before merging.*

---

## FAQ

**Q: Does ReviewBot review its own self-improvement PRs?**
No — PRs from the `reviewbot/*` branch are skipped to avoid infinite loops.

**Q: Can I manually trigger a self-improvement cycle?**
Yes — go to Actions → ReviewBot → Run workflow → select "self-improve".

**Q: What if I don't want auto-generated rules?**
Set `learning.auto_create_rules: false` in config. ReviewBot will still retire/boost existing rules.

**Q: How much does it cost?**
Depends on PR volume. Each review uses ~2K-5K tokens (Claude Sonnet). For a team doing 20 PRs/week, expect ~$5-15/month.

**Q: Can I use a different Claude model?**
Yes — change `model` in config.yaml to any supported Claude model (e.g., `claude-opus-4-6` for deeper analysis).

---

## Roadmap

- [ ] Slack/Discord notifications for improvement PRs
- [ ] Web dashboard for rule effectiveness visualization
- [ ] GitLab CI/CD support
- [ ] Multi-repo shared knowledge (org-level rules)
- [ ] Per-directory rule scoping
- [ ] PR complexity scoring and auto-assignment

---

## Contributing

PRs welcome! See the roadmap above for areas that need work.

```bash
git clone https://github.com/praveenkumardec89/reviewbot.git
cd reviewbot
# Make your changes
# Test locally by running the engine modules with mock data
```

---

## License

MIT — use it, fork it, make it yours.

Built by [@praveenkumardec89](https://github.com/praveenkumardec89)
