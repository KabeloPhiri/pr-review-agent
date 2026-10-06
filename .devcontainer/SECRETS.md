# Keys, tokens and environment variables

Everything the container needs lives in **one file**: `.devcontainer/.env`.
Each line is `KEY=value`, where the value is either typed in directly or is a
**1Password secret reference** (`op://<vault>/<item>/<field>`). The two styles mix
freely, so you can start with plain values today and move keys into 1Password one
at a time.

```
.devcontainer/
├── env.example              # committed template - every supported key, documented
├── .env                     # YOUR copy - gitignored, Claude Code cannot read it
├── secrets/load-env.sh      # sourced by every terminal: exports literals, resolves op:// refs
└── bin/env-check            # shows what is set (secrets masked) and what is still unresolved
```

How it flows:

```
.devcontainer/.env ──> load-env.sh ──┬── literal values ─────────────────┐
                                     └── op:// refs ──> op inject ──┐    │  exported into
                          (needs OP_SERVICE_ACCOUNT_TOKEN           │    │  the terminal and
                           or an interactive `op signin`)  <────────┘    │  every CLI you run
                                                                         ▼  from it
                                            databricks / az / aws / gcloud / gh / glab / bb / claude / gemini / terraform
```

Guardrails already in place:

- `.env` is matched by `.gitignore` (`.env`) - it can never be committed.
- `.claude/settings.json` denies the agent `Read/Write/Grep` on `**/.env`, `env`/`printenv`
  dumps, and every `op read` / `op item` / `op inject` / `op run` command.
- Resolved secrets are written only to tmpfs (`$XDG_RUNTIME_DIR` or `/dev/shm`, mode 0600)
  for the milliseconds `op inject` needs, then deleted.
- Blank keys are skipped, never exported as empty strings (an empty `DATABRICKS_HOST`
  would otherwise break the Databricks CLI).

---

## Part A - the keys file (day one, no 1Password required)

1. **Get the file.** The first `Reopen in Container` runs `host-prep.ps1`, which copies
   `env.example` to `.devcontainer/.env`. If it is missing, copy it yourself:

   ```powershell
   Copy-Item .devcontainer\env.example .devcontainer\.env      # Windows
   cp .devcontainer/env.example .devcontainer/.env             # mac / linux / inside the container
   ```

2. **Fill in only what you use.** Open `.devcontainer/.env` in VS Code and set values.
   Leave everything else blank. Quote values that contain spaces or `#`.

   ```
   DATABRICKS_CONFIG_PROFILE=dev
   DATABRICKS_HOST=https://adb-1234567890.12.azuredatabricks.net
   DATABRICKS_TOKEN=dapi0123456789abcdef
   AWS_PROFILE=dev
   ```

   Tip: for Databricks, Azure, AWS, GCP, GitHub and GitLab you often need **no key at
   all** - log in once with the CLI (see `README.md`) and the login is persisted in the
   host config directory that is mounted into the container.

3. **Load it.** Open a new terminal in VS Code (every terminal sources the loader), or in
   the current one:

   ```bash
   source .devcontainer/secrets/load-env.sh
   ```

4. **Check it.**

   ```bash
   env-check
   ```

   ```
   KEY                          SOURCE     EXPORTED  VALUE
   ---                          ------     --------  -----
   OP_SERVICE_ACCOUNT_TOKEN     unset      no        -
   DATABRICKS_CONFIG_PROFILE    literal    yes       dev
   DATABRICKS_HOST              literal    yes       https://adb-1234567890.12.azuredatabricks.net
   DATABRICKS_TOKEN             literal    yes       ****  (38 chars)
   ...
   exported  : 4 of 22 keys
   ```

5. **Prove the guardrails.** `git status` must not list `.env`, and asking Claude Code to
   `cat .devcontainer/.env` must be refused.

6. **Changing a value later:** edit `.env`, then `source .devcontainer/secrets/load-env.sh`
   (or open a new terminal). No rebuild needed.

---

## Part B - moving keys into 1Password

Goal: `.env` contains references, not secrets. The only literal secret left is the
1Password service-account token (or none at all if you sign in interactively).

### B1. Put the secrets in 1Password

1. Create a vault for this work, e.g. **`Databricks-Dev`**. Keep it separate from
   personal vaults so a service account can be scoped to it alone.
2. Create one item per system. Use the *API Credential* or *Password* category; the
   field name becomes the last part of the reference.

   | Item (example)     | Field       | Reference                                    |
   |--------------------|-------------|----------------------------------------------|
   | `databricks-dev`   | `token`     | `op://Databricks-Dev/databricks-dev/token`   |
   | `anthropic`        | `api-key`   | `op://Databricks-Dev/anthropic/api-key`      |
   | `gemini`           | `api-key`   | `op://Databricks-Dev/gemini/api-key`         |
   | `bitbucket`        | `app-password` | `op://Databricks-Dev/bitbucket/app-password` |
   | `azure-devops`     | `pat`       | `op://Databricks-Dev/azure-devops/pat`       |

3. Copy each reference. In the 1Password app: hover the field -> **⋮** -> **Copy Secret
   Reference**. From a terminal where you are signed in:
   `op item get databricks-dev --vault Databricks-Dev --format json | jq '.fields[] | {label, reference}'`.

### B2. Point `.env` at them

Replace literals with references, one key at a time:

```
DATABRICKS_TOKEN=op://Databricks-Dev/databricks-dev/token
ANTHROPIC_API_KEY=op://Databricks-Dev/anthropic/api-key
BITBUCKET_APP_PASSWORD=op://Databricks-Dev/bitbucket/app-password
```

### B3. Let the container sign in to 1Password - pick ONE

**Option 1 - service account (recommended: non-interactive, scoped, revocable)**

1. Sign in at `https://<your-team>.1password.com` -> **Developer Tools** -> **Service Accounts**
   -> **New Service Account**.
2. Name it (e.g. `devcontainer-databricks-toolbox`), grant **Read** on the
   `Databricks-Dev` vault only, create it.
3. Copy the token (shown once, starts with `ops_`) into `.env`:

   ```
   OP_SERVICE_ACCOUNT_TOKEN=ops_eyJ...
   ```

   This is now the only literal secret in the file. Everything else resolves through it.

**Option 2 - interactive sign-in (personal account, no service account needed)**

1. Inside the container, once:

   ```bash
   op account add        # prompts for sign-in address, email, secret key, password
   ```

   The account registration is persisted through the `~/.config/op` mount.
2. At the start of each session:

   ```bash
   eval $(op signin)
   source .devcontainer/secrets/load-env.sh
   ```

   Until you do, `env-check` lists the referenced keys as `1Password / no` and the
   literal ones still work.

### B4. Verify

```bash
source .devcontainer/secrets/load-env.sh   # or open a new terminal
env-check
```

```
DATABRICKS_TOKEN             1Password  yes       ****  (38 chars)   <- op://Databricks-Dev/databricks-dev/…
...
1Password : signed in (https://your-team.1password.com)
```

Then use a real CLI with the resolved value: `databricks current-user me`.

### B5. Rotate, revoke, share

- **Rotate a secret:** change it in the 1Password item. Open a new terminal. Done -
  nothing in `.env` or the container changes.
- **Revoke access:** delete the service account (or its vault grant) in 1Password. The
  next terminal reports the keys as unresolved.
- **Share with a teammate:** they clone the repo, copy `env.example` -> `.env`, paste the
  same `op://` lines, and either get their own service-account token or use their own
  1Password account with access to the vault. No secrets are ever exchanged over chat.

### B6. Troubleshooting

| Symptom (from `env-check` / terminal)                        | Fix                                                                 |
|--------------------------------------------------------------|---------------------------------------------------------------------|
| `1Password : not signed in`                                   | set `OP_SERVICE_ACCOUNT_TOKEN`, or `eval $(op signin)`              |
| `could not resolve KEY - check the op:// path`                | `op vault list`, `op item list --vault <vault>`, `op item get <item>` - vault/item/field names must match exactly (labels are case-sensitive) |
| `op inject failed`                                            | `op whoami` - a service account only sees vaults it was granted     |
| service account works locally but not for a colleague         | each person needs their own token or account; tokens are per-person |
| key shows `literal` but you expected `1Password`              | the value does not start with `op://` - check for quotes or spaces  |

Run these diagnostics yourself in a terminal. Claude Code is deliberately denied
`op read`, `op item` and `op inject`, so it can never pull a secret on your behalf.
