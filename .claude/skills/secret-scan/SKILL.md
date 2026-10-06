---
name: secret-scan
description: Find and strip hard-coded tokens, keys and passwords from Python scripts, notebooks (.ipynb / Databricks source) and SQL. Use before any audit, commit, PR or bundle deploy, and whenever a user asks to check scripts for secrets.
---

# Secret scan & strip

Two PowerShell tools share one rule library (`tools/SecretRules.psd1`):

| Tool | Purpose |
|---|---|
| `tools/Find-Secrets.ps1` | Detect and report (masked). Exit 1 with `-FailOnFindings`. |
| `tools/Strip-Secrets.ps1` | Rewrite files: Python literal -> `dbutils.secrets.get(scope, key)`, YAML -> `{{secrets/scope/key}}`, everything else -> `<REDACTED:Rule>`. Writes `.bak` backups. |

## Procedure

1. **Scan first, always masked**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools/Find-Secrets.ps1 -Path <dir|file> -OutputJson <job-tmp>/secrets.json -Quiet
   ```
   Read the JSON, not the raw files. Never pass `-ShowSecret`; never echo, quote or paste a detected value in your reply - the masked form (`dapi****cdef`) is the most you may show.
2. **Preview the strip**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools/Strip-Secrets.ps1 -Path <dir> -Scope <secretScope> -WhatIf
   ```
3. **Apply** (drop `-WhatIf`). Confirm with the user before applying to more than one file, and tell them `.bak` files exist and must not be committed.
4. **Rescan** to prove zero findings; delete `.bak` files only when the user confirms.
5. **Report**: counts by severity, files touched, replacement strategy used, and which secret scope / key names the code now expects so the user can create them (`databricks secrets put-secret`).

## Interpreting findings

- `High` = concrete credential format (Databricks PAT `dapi...`, AWS `AKIA...`, storage account key, private key block, GitHub/Slack/OpenAI tokens, `password=` in JDBC URLs, `IDENTIFIED BY`).
- `Medium` = JWTs, webhooks, high-entropy literal assigned to a sensitive-looking variable.
- `Low` = long hex strings on `key|hash|salt` variables - often false positives (checksums).
- Lines using `dbutils.secrets.get`, `os.environ`, `{{secrets/...}}`, Key Vault / Secrets Manager calls, or containing `example`, `changeme`, `<your-token>` are allow-listed. If a real credential hides behind one of those words, it will be missed - say so when relevant.
- Notebook findings carry a cell index; `cell N (output)` means the secret is in a *saved output*, which needs clearing (`jupyter nbconvert --clear-output` or re-run without printing) in addition to fixing the code.

## Adding rules

Edit `tools/SecretRules.psd1` only. Each rule needs `Name`, `Severity`, `Pattern` (.NET regex) and `Group` (capture group holding the secret; count nested groups carefully). Re-run both tools on a fixture with a fake token before relying on the rule.

## Rules that bind you

- Key Rule 5: do not `Read`/`Grep`/`cat` `.databrickscfg`, `.env`, or any environment-variable dump. The tools skip `.databrickscfg` by extension; you skip it by policy.
- Never commit `.bak` files or the JSON/CSV report; keep reports in the job tmp dir unless the user asks for them in-repo.
