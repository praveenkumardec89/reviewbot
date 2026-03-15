# ReviewCrew Knowledge Store
# This directory is managed by ReviewCrew's self-learning engine.
# Feel free to edit rules.yaml and config.yaml manually.
# scores.json and patterns.json are auto-maintained.

## Files

| File | Purpose | Edit manually? |
|------|---------|---------------|
| `rules.yaml` | Review rules (auto + manual) | ✅ Yes |
| `config.yaml` | Bot configuration | ✅ Yes |
| `patterns.json` | Learned code patterns | ⚠️ Carefully |
| `scores.json` | Feedback scores per rule | ❌ Auto-managed |
| `infra.yaml` | Component/infra knowledge | ✅ Yes |
| `history/` | Improvement logs | ❌ Auto-managed |

## How Rules Evolve

1. **Starter rules** come from the default template
2. **Team feedback** (👍/👎, resolve/dismiss) adjusts rule scores
3. **Self-improvement engine** (weekly) analyzes scores and:
   - Retires rules with score < -10
   - Boosts rules with score > +10
   - Creates new rules from repeated patterns
   - Creates guard rules from reverted PRs
4. **All changes** come via PR for human review

## Adding Custom Rules

Add rules to `rules.yaml`:

```yaml
- id: my_custom_rule
  description: Always use structured logging, never fmt.Printf
  severity: medium
  category: style
  scope: "**/*.go"
  example: "fmt.Printf('user logged in') → log.Info('user_login', ...)"
```

## Adding Component Knowledge

Add to `infra.yaml`:

```yaml
src/payments/:
  owner: payments-team
  notes: PCI-DSS compliant zone, extra security review needed
  dependencies: [stripe-sdk, vault]

src/auth/:
  owner: security-team
  notes: OAuth2 + JWT implementation, changes need security review
```
