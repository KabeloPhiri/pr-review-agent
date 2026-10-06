# Dev container - Databricks toolbox

One reproducible, fully pinned Linux environment with every CLI this toolbox needs,
plus Claude Code and Gemini CLI, that reuses the logins already on your machine.

| Area            | Tooling (pinned)                                                       |
|-----------------|-------------------------------------------------------------------------|
| Databricks      | `databricks` CLI 1.17.0, `databricks-connect` 17.3.14, VS Code extension |
| Clouds          | `az` 2.90.0, `aws` 2.36.50, `gcloud` 585.0.0                             |
| DevOps          | `gh` 2.101.0, `az devops` ext 1.0.8, `glab` 1.118.0, `bb` (Bitbucket REST helper) |
| IaC             | `terraform` 1.16.3                                                       |
| AI              | Claude Code 2.1.278, Gemini CLI 0.60.0 (Node 22)                          |
| Secrets         | `op` (1Password CLI) 2.39.0 - see `SECRETS.md`                            |
| Repo tools      | PowerShell 7.6.6 so `tools/*.ps1` run unchanged; Python 3.12 (what DBR 17.3 runs), JupyterLab |

## 1. Prerequisites (host)

- Docker Desktop (WSL 2 backend on Windows) and VS Code with the **Dev Containers** extension.
- Windows: nothing else - `host-prep.ps1` runs with the built-in PowerShell 5.1.
  macOS/Linux: install `pwsh` and run `pwsh -File .devcontainer/host-prep.ps1` once before the first build.
- **OneDrive warning.** Bind-mounting a repo that lives under OneDrive is slow and OneDrive
  can lock files mid-build. Clone to a plain path (`C:\dev\...`) or inside WSL (`\\wsl$\...`)
  for the container workflow.

## 2. First start

1. `F1` -> **Dev Containers: Reopen in Container**. The first build takes several minutes
   (CLIs are downloaded once; later builds hit the cache).
2. `host-prep.ps1` creates any missing host config folders and copies
   `.devcontainer/env.example` -> `.devcontainer/.env`.
3. `post-create.sh` installs the AI CLIs, `az devops`, Python deps, and prints a version report.
4. `post-start.sh` prints the **auth status** checklist on every start - each line tells you
   the exact login command if something is missing.
5. Fill `.devcontainer/.env` (`SECRETS.md`), open a new terminal, run `env-check`.

## 3. Logging in - one command per tool

Logins persist on the **host** because the config directories are bind-mounted:

| Tool          | Command inside the container                                              | Persists in (host)         |
|---------------|----------------------------------------------------------------------------|----------------------------|
| Databricks    | `databricks auth login --host https://<workspace> --profile dev` (OAuth U2M)<br>or `DATABRICKS_HOST`/`DATABRICKS_TOKEN` in `.env` | `~/.databrickscfg`         |
| Azure         | `az login --use-device-code`<br>`az devops configure --defaults organization=https://dev.azure.com/<org> project=<proj>` | `~/.azure`                 |
| AWS           | `aws configure sso` (once) then `aws sso login --profile <p>`; set `AWS_PROFILE` in `.env` | `~/.aws`                   |
| GCP           | `gcloud auth login --no-launch-browser`<br>`gcloud auth application-default login --no-launch-browser` | `~/.config/gcloud`         |
| GitHub        | `gh auth login --web`                                                       | `~/.config/gh`             |
| GitLab        | `glab auth login` (or `GITLAB_TOKEN`/`GITLAB_HOST` in `.env`)               | `~/.config/glab-cli`       |
| Bitbucket     | `BITBUCKET_WORKSPACE/USERNAME/APP_PASSWORD` in `.env`, then `bb repos`      | `.env`                     |
| 1Password     | `OP_SERVICE_ACCOUNT_TOKEN` in `.env`, or `op account add` + `eval $(op signin)` | `~/.config/op`             |
| Claude Code   | `claude` (OAuth in browser) or `ANTHROPIC_API_KEY` in `.env`                | `~/.claude`                |
| Gemini CLI    | `gemini` (Google login) or `GEMINI_API_KEY` in `.env`                        | `~/.gemini`                |
| Terraform     | `terraform login` only if you use Terraform Cloud remote state              | `.env` (`TF_TOKEN_*`)      |

Not mounted on purpose: `~/.ssh` (Windows file permissions break ssh's 0600 check - VS Code
forwards your host SSH agent instead) and `~/.claude.json` (contains Windows project paths).

## 4. Everyday commands

```bash
env-check                                   # what is configured, secrets masked
databricks bundle validate -t dev           # DAB checks (uat/prod deploys are denied to the agent)
pwsh -File tools/Find-Secrets.ps1 -Path .   # the repo's PowerShell tools work as-is
pwsh -File tools/Get-DataSample.ps1 -Table cat.schema.tbl -Profile dev
jupyter lab --ip 0.0.0.0 --port 8888 --no-browser   # port 8888 is forwarded
claude                                      # picks up .claude/agents, skills and the deny list
gemini
bb prs <repo>                               # Bitbucket open PRs
```

### Databricks Connect

The container's global Python has `databricks-connect` (not `pyspark`). It uses the same
profile / `DATABRICKS_*` variables as the CLI plus `DATABRICKS_CLUSTER_ID`:

```python
from databricks.connect import DatabricksSession
spark = DatabricksSession.builder.profile("dev").getOrCreate()
spark.range(3).count()
```

- The pin **must match the dev cluster's runtime** (17.3.x <-> DBR 17.3 LTS). Change
  `.devcontainer/requirements-dev.txt` when `${var.sparkVersion}` changes, in its own branch.
- Bundle projects that unit-test with local `pyspark` (`templates/dab/requirements.txt`)
  cannot share this environment; create a project venv:
  `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`.
- Select the **Python (databricks-connect)** kernel for the notebooks in `Data Discovery/`.

## 5. Bumping a version

Every version is an explicit pin: `ARG`s at the top of `Dockerfile`, `features` in
`devcontainer.json`, the three constants at the top of `post-create.sh`, and
`requirements-dev.txt`. Change one, in its own branch, rebuild
(**Dev Containers: Rebuild Container**), confirm the version report, open a PR.
Never use `latest`.

## 6. Troubleshooting

| Problem                                                   | Fix                                                                                         |
|-----------------------------------------------------------|---------------------------------------------------------------------------------------------|
| Build fails on `initializeCommand`                        | Windows: run `powershell -File .devcontainer\host-prep.ps1` and read the error. mac/linux: install `pwsh`. |
| `host-prep` says both USERPROFILE and HOME are set / mount path looks like `C:\Users\meC:\Users\me` | You launched VS Code (or the devcontainer CLI) from Git Bash/MSYS. Start VS Code from the Start menu or PowerShell, or `env -u HOME code .`. |
| `~/.databrickscfg` inside the container is a directory     | An earlier mount created it. On the host delete the empty folder `%USERPROFILE%\.databrickscfg`, re-run `host-prep.ps1`, rebuild. |
| `databricks` says host not configured                      | `env-check` - either `DATABRICKS_HOST` in `.env` or a `[dev]` profile in `~/.databrickscfg`. |
| Slow file access / OneDrive sync errors                    | Move the clone outside OneDrive (see prerequisites).                                        |
| `op://` keys show `EXPORTED no`                            | `SECRETS.md` §B3 / §B6.                                                                     |
| Claude Code refuses to read `.env` or run `op read`        | Working as designed - Key Rule 5. Run those commands yourself.                              |
