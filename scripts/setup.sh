#!/usr/bin/env bash
# ReviewCrew — One-time setup script
# Run from the root of YOUR repository:
#   curl -s https://raw.githubusercontent.com/praveenkumardec89/reviewcrew/main/scripts/setup.sh | bash

set -e

REVIEWCREW_REPO="praveenkumardec89/reviewcrew"
TEMPLATE_BASE="https://raw.githubusercontent.com/${REVIEWCREW_REPO}/main/templates"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║           ReviewCrew Setup                           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

mkdir -p .reviewcrew/history

download() {
  local url="$1"
  local dest="$2"
  if command -v curl &>/dev/null; then
    curl -fsSL "$url" -o "$dest"
  elif command -v wget &>/dev/null; then
    wget -q "$url" -O "$dest"
  else
    echo "Error: curl or wget required"; exit 1
  fi
}

[ ! -f ".reviewcrew/rules.yaml" ]    && download "${TEMPLATE_BASE}/default-rules.yaml" ".reviewcrew/rules.yaml"           && echo "  -> Created .reviewcrew/rules.yaml"
[ ! -f ".reviewcrew/config.yaml" ]   && download "${TEMPLATE_BASE}/default-config.yaml" ".reviewcrew/config.yaml"         && echo "  -> Created .reviewcrew/config.yaml"
[ ! -f ".reviewcrew/architecture.yaml" ] && download "${TEMPLATE_BASE}/default-architecture.yaml" ".reviewcrew/architecture.yaml" && echo "  -> Created .reviewcrew/architecture.yaml"
[ ! -f ".reviewcrew/patterns.json" ] && echo '{}' > .reviewcrew/patterns.json
[ ! -f ".reviewcrew/scores.json" ]   && echo '{}' > .reviewcrew/scores.json
[ ! -f ".reviewcrew/infra.yaml" ]    && echo '{}' > .reviewcrew/infra.yaml
[ ! -f ".reviewcrew/history/improvements.json" ] && echo '[]' > .reviewcrew/history/improvements.json

mkdir -p .github/workflows
[ ! -f ".github/workflows/reviewcrew.yml" ] && download "${TEMPLATE_BASE}/consumer-workflow.yml" ".github/workflows/reviewcrew.yml" && echo "  -> Created .github/workflows/reviewcrew.yml"

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Add ANTHROPIC_API_KEY to repo secrets (Settings -> Secrets -> Actions)"
echo "  2. Edit .reviewcrew/architecture.yaml to describe your layers and services"
echo "  3. git add .reviewcrew/ .github/workflows/reviewcrew.yml && git commit -m 'Setup ReviewCrew' && git push"
echo "  4. Open a PR — ReviewCrew activates automatically"
echo ""
