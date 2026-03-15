#!/bin/bash
# ReviewBot Quick Setup Script
# Run this in your repo root to set up ReviewBot

set -e

echo "🤖 ReviewBot Setup"
echo "=================="
echo ""

# Check if .reviewbot already exists
if [ -d ".reviewbot" ]; then
    echo "⚠️  .reviewbot/ directory already exists. Skipping initialization."
    echo "   Delete it first if you want a fresh start."
else
    echo "📁 Creating .reviewbot/ knowledge store..."
    mkdir -p .reviewbot/history

    # Copy templates (or create defaults)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    TEMPLATE_DIR="$SCRIPT_DIR/../templates"

    if [ -f "$TEMPLATE_DIR/default-rules.yaml" ]; then
        cp "$TEMPLATE_DIR/default-rules.yaml" .reviewbot/rules.yaml
        cp "$TEMPLATE_DIR/default-config.yaml" .reviewbot/config.yaml
    else
        # Inline defaults if templates aren't available
        cat > .reviewbot/rules.yaml << 'RULES'
- id: security_secrets
  description: Flag hardcoded secrets, API keys, tokens, or passwords
  severity: critical
  category: security
  scope: "**/*"

- id: error_handling
  description: Functions that can throw should have proper error handling
  severity: high
  category: bug
  scope: "**/*"

- id: missing_tests
  description: New public functions should have test coverage
  severity: medium
  category: test
  scope: "src/**/*"
RULES

        cat > .reviewbot/config.yaml << 'CONFIG'
model: claude-sonnet-4-20250514
review:
  auto_review: true
  severity_threshold: low
  max_comments_per_pr: 15
learning:
  enabled: true
  min_feedback_samples: 5
  auto_create_rules: true
  require_approval: true
build_fixer:
  enabled: true
  auto_fix_pr: true
CONFIG
    fi

    echo '{}' > .reviewbot/patterns.json
    echo '{}' > .reviewbot/scores.json
    echo '{}' > .reviewbot/infra.yaml
    echo '[]' > .reviewbot/history/improvements.json

    echo "✅ Knowledge store created"
fi

# Setup GitHub Actions workflow
echo ""
echo "📋 Setting up GitHub Actions workflow..."
mkdir -p .github/workflows

REVIEWBOT_ORG="${REVIEWBOT_ORG:-your-org}"

cat > .github/workflows/reviewbot.yml << WORKFLOW
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
    - cron: '0 2 * * 0'  # Weekly self-improvement
  workflow_dispatch:
    inputs:
      action:
        description: 'Manual action'
        type: choice
        options:
          - self-improve
          - re-review
          - reset-scores

jobs:
  ai-review:
    if: >
      github.event_name == 'pull_request' &&
      (github.event.action == 'opened' || github.event.action == 'synchronize')
    uses: ${REVIEWBOT_ORG}/reviewbot/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}

  track-feedback:
    if: github.event_name == 'pull_request_review'
    uses: ${REVIEWBOT_ORG}/reviewbot/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}

  self-improve:
    if: >
      github.event_name == 'schedule' ||
      (github.event_name == 'workflow_dispatch' && github.event.inputs.action == 'self-improve')
    uses: ${REVIEWBOT_ORG}/reviewbot/.github/workflows/self-improve.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}

  build-fixer:
    if: >
      github.event_name == 'issues' &&
      contains(github.event.issue.labels.*.name, 'build-failure')
    uses: ${REVIEWBOT_ORG}/reviewbot/.github/workflows/self-improve.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}
WORKFLOW

echo "✅ Workflow created at .github/workflows/reviewbot.yml"

echo ""
echo "🔑 Next steps:"
echo "   1. Add ANTHROPIC_API_KEY to your repo secrets"
echo "      → Settings > Secrets and variables > Actions > New repository secret"
echo "   2. Replace 'your-org' in the workflow with your actual org/user"
echo "      → Edit .github/workflows/reviewbot.yml"
echo "   3. Commit and push:"
echo "      git add .reviewbot/ .github/workflows/reviewbot.yml"
echo "      git commit -m 'Setup ReviewBot self-learning code review'"
echo "      git push"
echo ""
echo "   4. (Optional) Customize .reviewbot/config.yaml and rules.yaml"
echo ""
echo "🎉 ReviewBot will activate on your next PR!"
