#!/usr/bin/env bash
# Runs once after the container is built (devcontainer.json postCreateCommand).
# Installs the pieces that need Node/az (features are layered after the Dockerfile)
# and prints a version report. All versions pinned - bump in its own branch.
set -euo pipefail

CLAUDE_CODE_VERSION="2.1.278"
GEMINI_CLI_VERSION="0.60.0"
AZ_DEVOPS_EXT_VERSION="1.0.8"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "Azure DevOps extension for az (${AZ_DEVOPS_EXT_VERSION})"
az extension add --name azure-devops --version "${AZ_DEVOPS_EXT_VERSION}" --upgrade --yes >/dev/null
az config set extension.use_dynamic_install=no >/dev/null 2>&1 || true

step "AI CLIs: Claude Code ${CLAUDE_CODE_VERSION}, Gemini CLI ${GEMINI_CLI_VERSION}"
npm install -g --no-fund --no-audit \
  "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
  "@google/gemini-cli@${GEMINI_CLI_VERSION}"

step "Python dev requirements (.devcontainer/requirements-dev.txt)"
pip install --no-cache-dir -r "$here/requirements-dev.txt"
python -m ipykernel install --user --name databricks-connect --display-name "Python (databricks-connect)" >/dev/null

step "Helper scripts on PATH (.devcontainer/bin)"
chmod +x "$here"/bin/* "$here"/secrets/load-env.sh "$here"/post-start.sh || true

step "Smoke test: PowerShell tools run under Linux"
if pwsh -NoProfile -File "$root/tools/Find-Secrets.ps1" -Path "$root/templates" -Quiet; then
  echo "tools/Find-Secrets.ps1 OK"
else
  echo "tools/Find-Secrets.ps1 reported findings or failed (exit $?)" >&2
fi

step "Version report"
v() { printf '  %-18s %s\n' "$1" "$(eval "$2" 2>/dev/null | head -1 || echo 'MISSING')"; }
v "python"      "python --version"
v "databricks"  "databricks --version"
v "az"          "az version --query '\"azure-cli\"' -o tsv"
v "az devops"   "az extension show --name azure-devops --query version -o tsv"
v "aws"         "aws --version"
v "gcloud"      "gcloud --version | head -1"
v "gh"          "gh --version | head -1"
v "glab"        "glab --version | head -1"
v "op"          "op --version"
v "terraform"   "terraform version | head -1"
v "pwsh"        "pwsh --version"
v "node"        "node --version"
v "claude"      "claude --version"
v "gemini"      "gemini --version"
v "db-connect"  "pip show databricks-connect | sed -n 's/^Version: //p'"

cat <<'EOF'

Next: fill .devcontainer/.env (see .devcontainer/SECRETS.md), open a new terminal,
run `env-check`, then log in per tool as listed in .devcontainer/README.md.
EOF
