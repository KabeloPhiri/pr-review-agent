# shellcheck shell=bash
# -----------------------------------------------------------------------------
# load-env.sh - source the single keys file (.devcontainer/.env) into the shell.
#
#   * Sourced automatically by every interactive bash/zsh (Dockerfile hook).
#   * Manual reload after editing .env:   source .devcontainer/secrets/load-env.sh
#   * Each line is KEY=value. Blank values are skipped (never exported as "").
#   * A value of the form  op://<vault>/<item>/<field>  is a 1Password secret
#     reference and is resolved with `op inject` when `op` is signed in
#     (OP_SERVICE_ACCOUNT_TOKEN in the same file, or an interactive `op signin`).
#     Resolved values only ever exist in tmpfs (0600) for milliseconds; nothing
#     resolved is written to the workspace.
#   * Never prints a value. `env-check` shows what is set, masked.
#
# Must be POSIX-ish (bash + zsh). Do not use bash arrays or BASH_SOURCE.
# -----------------------------------------------------------------------------

_dcenv_main() {
  local envFile="${DEVCONTAINER_ENV_FILE:-}"
  if [ -z "$envFile" ]; then
    local w
    for w in /workspaces/*/.devcontainer/.env; do [ -r "$w" ] && envFile="$w" && break; done
  fi
  if [ -z "$envFile" ] || [ ! -r "$envFile" ]; then
    [ -n "${DEVCONTAINER_ENV_QUIET:-}" ] || \
      echo "[devcontainer] no keys file at ${envFile:-.devcontainer/.env} - copy .devcontainer/env.example to .devcontainer/.env (see SECRETS.md)" >&2
    return 0
  fi

  # tmpfs scratch for anything that may hold a secret
  local oldUmask; oldUmask="$(umask)"; umask 077
  local tmpDir="" d
  for d in "${XDG_RUNTIME_DIR:-}" /dev/shm "${TMPDIR:-}" /tmp; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    if ( : > "$d/.dcenv.probe.$$" ) 2>/dev/null; then rm -f "$d/.dcenv.probe.$$"; tmpDir="$d"; break; fi
  done
  if [ -z "$tmpDir" ]; then
    echo "[devcontainer] no writable temp dir for the keys loader" >&2; umask "$oldUmask"; return 0
  fi
  local literals="$tmpDir/.dcenv.lit.$$" refs="$tmpDir/.dcenv.ref.$$" resolved="$tmpDir/.dcenv.res.$$"
  : > "$literals"; : > "$refs"

  # --- 1. parse: split literals from op:// references, drop blanks -------------
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"        # ltrim
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    # trim surrounding whitespace, then matching quotes
    val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    [ -z "$val" ] && continue
    case "$val" in
      op://*) printf '%s=%s\n' "$key" "$val" >> "$refs" ;;
      *)      printf '%s=%s\n' "$key" "$val" >> "$literals" ;;
    esac
  done < "$envFile"

  # --- 2. export literals first (OP_SERVICE_ACCOUNT_TOKEN may be one of them) --
  local n
  while IFS= read -r line; do
    key="${line%%=*}"; val="${line#*=}"
    export "$key=$val"
  done < "$literals"

  # --- 3. resolve 1Password references, if any ---------------------------------
  export DEVCONTAINER_ENV_UNRESOLVED=""
  if [ -s "$refs" ]; then
    n="$(wc -l < "$refs" | tr -d ' ')"
    if ! command -v op >/dev/null 2>&1; then
      echo "[devcontainer] $n op:// reference(s) in .env but the 1Password CLI is not installed" >&2
      DEVCONTAINER_ENV_UNRESOLVED="$n"
    elif ! op whoami >/dev/null 2>&1; then
      echo "[devcontainer] $n op:// reference(s) in .env not resolved - 1Password is not signed in." >&2
      echo "               set OP_SERVICE_ACCOUNT_TOKEN in .env, or run:  eval \$(op signin)  then  source .devcontainer/secrets/load-env.sh" >&2
      DEVCONTAINER_ENV_UNRESOLVED="$n"
    else
      if op inject -i "$refs" -o "$resolved" >/dev/null 2>&1 && [ -s "$resolved" ]; then
        while IFS= read -r line; do
          key="${line%%=*}"; val="${line#*=}"
          case "$val" in op://*) continue ;; esac   # left unresolved by op (bad ref)
          [ -n "$val" ] && export "$key=$val"
        done < "$resolved"
        # anything still unresolved?
        n=0
        while IFS= read -r line; do
          key="${line%%=*}"
          eval "val=\${$key:-}"
          case "$val" in ''|op://*) n=$((n+1)); echo "[devcontainer] could not resolve $key - check the op:// path (vault/item/field)" >&2 ;; esac
        done < "$refs"
        [ "$n" -gt 0 ] && DEVCONTAINER_ENV_UNRESOLVED="$n"
      else
        echo "[devcontainer] op inject failed - run:  op whoami ; op vault list  to diagnose" >&2
        DEVCONTAINER_ENV_UNRESOLVED="$n"
      fi
    fi
  fi

  rm -f "$literals" "$refs" "$resolved"
  umask "$oldUmask"
  export DEVCONTAINER_ENV_LOADED="$envFile"
  return 0
}

_dcenv_main
unset -f _dcenv_main
