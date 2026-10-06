#!/usr/bin/env bash
# Runs on every container start (devcontainer.json postStartCommand).
# Prints an auth checklist. Every probe reports identity only - never a token.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEVCONTAINER_ENV_QUIET=1
# shellcheck source=secrets/load-env.sh
. "$here/secrets/load-env.sh"

ok()   { printf '  \033[32m[ ok ]\033[0m %-12s %s\n' "$1" "$2"; }
todo() { printf '  \033[33m[ -- ]\033[0m %-12s not logged in -> %s\n' "$1" "$2"; }
probe() { # name, login-hint, command...
  local name="$1" hint="$2"; shift 2
  local out
  if out="$("$@" 2>/dev/null)" && [[ -n "$out" ]]; then ok "$name" "$(head -1 <<<"$out")"; else todo "$name" "$hint"; fi
}

echo
echo "databricks-toolbox dev container - auth status"
echo "----------------------------------------------"

# keys file
if [[ -z "${DEVCONTAINER_ENV_LOADED:-}" ]]; then
  todo "keys file" "copy .devcontainer/env.example to .devcontainer/.env (SECRETS.md)"
else
  if grep -qE '^[A-Za-z_][A-Za-z0-9_]*=[[:space:]]*[^[:space:]]' "$DEVCONTAINER_ENV_LOADED"; then
    if [[ -n "${DEVCONTAINER_ENV_UNRESOLVED:-}" ]]; then
      todo "keys file" "${DEVCONTAINER_ENV_UNRESOLVED} 1Password ref(s) unresolved: set OP_SERVICE_ACCOUNT_TOKEN or 'eval \$(op signin)'"
    else
      ok "keys file" ".devcontainer/.env loaded (run env-check for detail)"
    fi
  else
    todo "keys file" ".devcontainer/.env is empty - fill the keys you use (SECRETS.md)"
  fi
fi

probe "1Password"  "op account add  /  OP_SERVICE_ACCOUNT_TOKEN in .env" op whoami
probe "databricks" "databricks auth login --host https://<workspace> --profile dev" \
      bash -c 'databricks current-user me -o json | jq -r .userName'
probe "azure"      "az login --use-device-code" az account show --query "join(' / ', [user.name, name])" -o tsv
probe "aws"        "aws configure sso && aws sso login" aws sts get-caller-identity --query Arn --output text
probe "gcloud"     "gcloud auth login --no-launch-browser" gcloud auth list --filter=status:ACTIVE --format="value(account)"
probe "github"     "gh auth login --web" gh api user --jq .login
probe "gitlab"     "glab auth login" bash -c 'glab auth status 2>&1 | grep -m1 "Logged in"'
if [[ -n "${BITBUCKET_APP_PASSWORD:-}" ]]; then ok "bitbucket" "app password set (bb repos)"; else todo "bitbucket" "BITBUCKET_* in .env"; fi
probe "terraform"  "terraform login (only if using Terraform Cloud)" terraform version
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then ok "claude" "API key from .env"; else probe "claude" "claude  (OAuth login)" claude --version; fi
if [[ -n "${GEMINI_API_KEY:-}" ]]; then ok "gemini" "API key from .env"; else probe "gemini" "gemini  (Google login)" gemini --version; fi
echo
