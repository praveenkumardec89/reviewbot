#!/bin/bash
# ReviewCrew Quick Setup Script
# Run this in your repo root to set up ReviewCrew

set -e

echo "🤖 ReviewCrew Setup"
echo "=================="
echo ""

# Check if .reviewcrew already exists
if [ -d ".reviewcrew" ]; then
    echo "⚠️  .reviewcrew/ directory already exists. Skipping initialization."
    echo "   Delete it first if you want a fresh start."
else
    echo "📁 Creating .reviewcrew/ knowledge store..."
    mkdir -p .reviewcrew/history

    # Copy templates (or create defaults)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    TEMPLATE_DIR="$SCRIPT_DIR/../templates"

    if [ -f "$TEMPLATE_DIR/default-rules.yaml" ]; then
        cp "$TEMPLATE_DIR/default-rules.yaml" .reviewcrew/rules.yaml
        cp "$TEMPLATE_DIR/default-config.yaml" .reviewcrew/config.yaml
    else
        # Inline defaults if templates aren't available
        cat > .reviewcrew/rules.yaml << 'RULES'
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

        cat > .reviewcrew/config.yaml << 'CONFIG'
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

    echo '{}' > .reviewcrew/patterns.json
    echo '{}' > .reviewcrew/scores.json
    echo '{}' > .reviewcrew/infra.yaml
    echo '[]' > .reviewcrew/history/improvements.json

    echo "✅ Knowledge store created"
fi

# Setup GitHub Actions workflow
echo ""
echo "📋 Setting up GitHub Actions workflow..."
mkdir -p .github/workflows

REVIEWBOT_ORG="${REVIEWBOT_ORG:-your-org}"

cat > .github/workflows/reviewcrew.yml << WORKFLOW
name: ReviewCrew
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
    uses: ${REVIEWBOT_ORG}/reviewcrew/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}

  track-feedback:
    if: github.event_name == 'pull_request_review'
    uses: ${REVIEWBOT_ORG}/reviewcrew/.github/workflows/review.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}

  self-improve:
    if: >
      github.event_name == 'schedule' ||
      (github.event_name == 'workflow_dispatch' && github.event.inputs.action == 'self-improve')
    uses: ${REVIEWBOT_ORG}/reviewcrew/.github/workflows/self-improve.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}

  build-fixer:
    if: >
      github.event_name == 'issues' &&
      contains(github.event.issue.labels.*.name, 'build-failure')
    uses: ${REVIEWBOT_ORG}/reviewcrew/.github/workflows/self-improve.yml@main
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}
WORKFLOW

echo "✅ Workflow created at .github/workflows/reviewcrew.yml"

echo ""
echo "🔑 Next steps:"
echo "   1. Add ANTHROPIC_API_KEY to your repo secrets"
echo "      → Settings > Secrets and variables > Actions > New repository secret"
echo "   2. Replace 'your-org' in the workflow with your actual org/user"
echo "      → Edit .github/workflows/reviewcrew.yml"
echo "   3. Commit and push:"
echo "      git add .reviewcrew/ .github/workflows/reviewcrew.yml"
echo "      git commit -m 'Setup ReviewCrew self-learning code review'"
echo "      git push"
echo ""
echo "   4. (Optional) Customize .reviewcrew/config.yaml and rules.yaml"
echo ""
echo "🎉 ReviewCrew will activate on your next PR!"
