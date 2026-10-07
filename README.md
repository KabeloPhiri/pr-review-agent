# PR Review Agent

An AI pull request reviewer that runs as a **Databricks App**. A GitHub
Actions workflow or an Azure DevOps pipeline calls it on every pull request; it
reads the diff, reviews the changed lines against your team's coding standards,
and posts the findings back as inline comments plus a summary — with a
pass/fail status a branch policy can gate on.

Code focus: **PySpark, SQL and Python**.

```
PR opened/updated
   └─> pr-review.yml (GitHub Actions / ADO build validation)
         └─> POST /review  ──>  Databricks App
                                  ├─ fetch PR + diff        (GitHub or Azure DevOps REST)
                                  ├─ load .prreview/ config + standards
                                  ├─ review changed lines   (Databricks model serving)
                                  ├─ gate on severity
                                  └─ post comments + PR status
```

## What makes it extensible

Everything except the pipeline itself is a plugin registered by name, and
configuration decides which ones load. A plugin nobody selected is never
imported, so features can be added or removed without touching the core.

| Family | Interface | Ships with | Add one by |
|---|---|---|---|
| SCM | `ScmConnector` | `github`, `azure_devops`, `fake` | new module in `app/services/scm/` + a line in `_MODULES` |
| Quality tools | `QualityConnector` | `noop` (SonarQube adapter is next) | new module in `app/services/quality/` |
| Reviewer | `Reviewer` | `llm` (one structured-output call per chunk) | new module in `app/services/review/` |
| Applier | `Applier` | `noop` (default, inert), `llm` | new module in `app/services/apply/` |
| Analyzers | standards fragments | `python`, `pyspark`, `sql`, `naming` | drop markdown in `app/defaults/standards/` — no code change |

## Configuration

Four layers, each overriding the one before:

1. `app/defaults/config.yaml` — shipped defaults
2. `PRREVIEW_<FIELD>` environment variables — set per deployment in `databricks.yml`
3. `.prreview/config.yaml` — committed to the **reviewed** repo, read from the PR's source branch
4. the `config` object in the request body

`POST /config/effective` returns the resolved configuration for a given pull
request, so a misconfigured repo is one call away from being explained.

### In the reviewed repository

```
.prreview/
  config.yaml     # which analyzers, which model, what blocks a merge
  standards.md    # your standards; these override the bundled defaults
  naming.md
```

```yaml
# .prreview/config.yaml
analyzers: [python, pyspark, sql]
model_endpoint: databricks-claude-sonnet-5-5
severity_gate: error          # error | warning | none
max_findings_per_file: 10
exclude_paths: ["**/*.ipynb", "tests/fixtures/**"]
```

Standards are plain markdown. The repo's text is marked authoritative in the
prompt, so where it disagrees with the bundled defaults, yours wins. Changing
how the bot reviews is a normal pull request against `.prreview/`.

## API

All endpoints require a Databricks OAuth bearer token (Apps reject PATs). The
token for the *reviewed* repo — a GitHub token or an Azure DevOps PAT — travels
in `X-SCM-Token` and is never stored; it lives only for the duration of that
review.

| Endpoint | Purpose |
|---|---|
| `POST /review` | Run a review. `mode`: `auto` (default), `sync`, `async` |
| `GET /review/{job_id}` | Poll a background review |
| `POST /apply` | Accept one bot suggestion and push it as a commit (opt-in; see below) |
| `POST /apply/all` | Accept every open bot suggestion on the PR (one commit per file) |
| `GET /apply/{job_id}` | Poll a background apply (single or all) |
| `POST /feedback` | Flag one of the bot's comments as a false positive (`/fp`) |
| `POST /config/effective` | Show the configuration a review would use |
| `POST /invocations` | MLflow agent entry point; always synchronous |
| `GET /health` | Health check (from the MLflow agent server) |

```bash
curl -X POST "$APP_URL/review" \
  -H "Authorization: Bearer $(databricks auth token | jq -r .access_token)" \
  -H "X-SCM-Token: $ADO_PAT" \
  -H 'Content-Type: application/json' \
  -d '{"organization":"contoso","project":"data","repository":"platform","pull_request_id":"42"}'
```

### sync, async, and `auto`

Databricks Apps cut HTTP connections at about 120 seconds, which a large pull
request will exceed.

- `sync` — blocks until the review finishes. Simple; can time out.
- `async` — returns `202 {job_id}` immediately and posts comments when done.
- `auto` — waits up to `sync_wait_seconds` (default 90) and returns the result
  if it is ready, otherwise hands back a job id. One execution either way.

**Job state is per-process.** It is lost on restart and is not shared across
replicas, so `GET /review/{job_id}` can legitimately 404 while the review is
still running. Comments and the PR status land regardless — that path does not
depend on the job store. If you need polling to be reliable for gating, run the
app single-replica, or gate on the Azure DevOps status check instead, or
implement a shared `JobStore` (see `app/core/jobs.py`).

## Accepting a suggestion

GitHub only, and opt-in. The PR author replies `/apply` on one of the bot's
inline suggestions; `pipelines/github/pr-apply.yml` (copy it alongside
`pr-review.yml`) calls `POST /apply`, and the bot pushes the suggested fix as
a commit on the PR's own branch.

Two flags have to be set together in `.prreview/config.yaml` — `allow_apply_fixes`
alone changes nothing, since the shipped `applier: noop` never produces a
change. For `/apply` the file is read from the PR's **target** branch, not its
source branch, so the opt-in has to be merged first — a pull request cannot
turn on code-pushing for itself:

```yaml
# .prreview/config.yaml
allow_apply_fixes: true
applier: llm
```

Only the **PR's own author** may trigger an apply, only on a comment written
by one of `apply_trusted_authors` (default `github-actions[bot]`, the identity
`pr-review.yml` posts as — anyone can paste a lookalike comment), and never on
a PR from a fork. Each of these, a second `/apply` on the same comment, and a
file that changed on the branch after the bot read it are quiet no-ops with a
`reason`, not errors. The bot reconstructs the finding from its own
comment text (severity, rule, message, suggestion — the same trick that keeps
`/review` idempotent, see "Re-runs are safe" below), regenerates the file with
one model call, and pushes a single-file commit via the Contents API.

**Testing the fix is not this app's job.** Pushing the commit re-triggers the
existing `synchronize`-triggered `pr-review.yml`, and whatever test job lives
in the reviewed repo's own CI — **provided the push token is not the
workflow's `GITHUB_TOKEN`**. GitHub does not start workflows for pushes made
with `GITHUB_TOKEN`, so `pr-apply.yml` pushes with a separate
`PR_REVIEW_APPLY_TOKEN` secret (a machine-user fine-grained PAT, or a GitHub
App token). Nothing here polls a build or reports pass/fail back.

**Several suggestions.** Replying `/apply` on several comments works: if two
land on the same file at once, the second is retried against the new branch
state (up to 3 times) rather than refused. To take everything at once,
comment **`/apply all`** on the pull request, or reply it in any bot thread.
Open bot suggestions are grouped by file, each file gets one model call with
all of its suggestions, and each file becomes one commit. The bot posts one
summary of what was applied and what was skipped and why, and resolves the
applied threads. Skipped without comment: lookalikes not written by
`apply_trusted_authors`, findings with no suggestion, and outdated comments
(GitHub drops the line once the code moved on). The PR-level comment form
needs `pr-apply.yml` on the default branch — GitHub runs `issue_comment`
workflows from there — while the in-thread reply works from the PR's copy.

Whenever a verified `/apply` is not applied (no change needed, the file kept
changing) or fails, the bot says so in the suggestion's thread. Fixes keep
the file's line endings and final newline.

Azure DevOps is not wired up for this yet — it has no clean YAML-only trigger
for a comment reply, unlike GitHub's `pull_request_review_comment` event.

## Admin console

`/console` on the deployed app (sign in through Databricks as usual):
token usage and cost per model, reviews and pass rate, applies, and one row
per repository — every page filters by repository and date range — plus the
settings an admin can change without a redeploy:

| Page | Change |
|---|---|
| Model | The review model, globally or for one repository; test it on the sample pull request first; prices per model for cost |
| Config | Any `EffectiveConfig` key, globally or per repository |
| Standards | The standards set (copy the bundled set in, then edit), or extra standards for one repository |
| Prompts | The guidance part of the review and apply prompts (the output format stays fixed) |
| Audit log / History | Who changed what and when; compare versions and restore |

**False positives.** Reply **`/fp`**, optionally followed by a reason, on any
bot comment to flag it as wrong. The PR author, and anyone with write access
to the repository (GitHub `OWNER`, `MEMBER` or `COLLABORATOR`), may flag; the
bot confirms in the thread and resolves it. The console's **False
positives** page turns flags into rates (flags divided by findings posted)
by rule, rule family, model and repository, next to how often each rule's
fixes were accepted with `/apply`. It also lists suspected duplicates: a new
comment on a line that already had one under a different rule id, usually
the model renaming the rule between runs.

Console settings beat a repository's own `.prreview/config.yaml`. Access is
two email allowlists in `databricks.yml` (`PRREVIEW_CONSOLE_ADMINS` edit,
`PRREVIEW_CONSOLE_VIEWERS` read). Models must be listed in
`PRREVIEW_ALLOWED_MODELS` with a `CAN_QUERY` resource. Settings are stored in
the bundle's volume, `dia_ai_agent_solution.pr_review.console` (the dev
target prefixes the schema).

## Local development

```bash
uv sync
uv run quickstart --profile <databricks-profile>   # experiment + .env + databricks.yml
uv run start-server                                # http://localhost:8000
```

Review a fixture pull request with no Azure DevOps access at all:

```bash
curl -X POST localhost:8000/review -H 'Content-Type: application/json' -d '{
  "scm": "fake",
  "repository": "platform",
  "pull_request_id": "42",
  "mode": "sync",
  "extra": {"fixture_dir": "tests/fixtures/sample-pr"}
}'
```

That exercises the whole pipeline — config layering, chunking, the model call,
gating, comment rendering — and prints the comments it would have posted.

```bash
pytest              # unit + fixture-driven pipeline tests, no network
```

## Deploying

```bash
databricks bundle deploy --target dev
databricks bundle run pr_review_agent --target dev   # deploy alone does not restart the app
```

`databricks.yml` grants the app two resources: the MLflow experiment for traces
and `CAN_QUERY` on the review model's serving endpoint. Change
`PRREVIEW_MODEL_ENDPOINT` and the `serving_endpoint` resource together.

`pipelines/azure-devops/deploy-app.yml` does the same from a pipeline.

## Wiring up GitHub

1. Copy `pipelines/github/pr-review.yml` to `.github/workflows/pr-review.yml`
   in the repository you want reviewed.
2. Add the repository secrets listed in that file's header
   (`DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`,
   `PR_REVIEW_APP_URL`).
3. Optionally make `pr-review-agent/ai-code-review` a required status check in
   branch protection so the review itself blocks the merge.
4. Optionally copy `pipelines/github/pr-apply.yml` too, to enable "Accepting a
   suggestion" (above). It reuses the same secrets, plus
   `PR_REVIEW_APPLY_TOKEN` — a fine-grained PAT (Contents and Pull requests,
   Read & write) or GitHub App token, so the pushed fix triggers CI.

The workflow's own `GITHUB_TOKEN` is the SCM token — there is no PAT to
manage — but it needs `pull-requests: write` (to comment) and
`statuses: write` (to publish the gate). Running the agent against a repo from *outside*
Actions needs a classic PAT with `repo`, or a fine-grained token with Pull
requests (Read & write), Contents (Read) and Commit statuses (Read & write) —
Contents (Read **& write**) if `allow_apply_fixes` is enabled.

### The apply token

`pr-apply.yml` pushes with `PR_REVIEW_APPLY_TOKEN` rather than the job's
`GITHUB_TOKEN`, because GitHub does not start workflows for pushes made with
`GITHUB_TOKEN` — the fix would land without CI or a re-review.

**Today: a fine-grained personal access token.**

1. As a machine user (e.g. `yourorg-bot`), not a person — every fix commit
   and confirmation comment is attributed to the token's owner: profile →
   **Settings** → **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → **Generate new token**.
2. Resource owner: the repo's owner. Repository access: *Only select
   repositories*. Repository permissions: **Contents** and **Pull requests**,
   both Read and write. Set an expiry and a reminder — an expired token makes
   every apply fail with a 401. Organisations may need to approve the token.
3. In the reviewed repo: **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret** named `PR_REVIEW_APPLY_TOKEN`, or
   `gh secret set PR_REVIEW_APPLY_TOKEN --repo <owner>/<repo>`.

**Planned: a GitHub App token (not yet applied).** Preferable for an
organisation: no long-lived token to rotate, no machine-user seat, and
commits show as `<app-name>[bot]`. Nothing in the app changes — only the
workflow and its secrets. To switch:

1. Create a GitHub App (**Settings** → **Developer settings** → **GitHub
   Apps** → **New GitHub App**; webhook off). Repository permissions:
   **Contents** and **Pull requests**, both Read and write.
2. Generate a private key, then install the App on the reviewed repo(s).
3. Add repo (or org) secrets `PR_REVIEW_APP_ID` and
   `PR_REVIEW_APP_PRIVATE_KEY` (the whole `.pem` contents).
4. In `pr-apply.yml`, mint a token before the "Apply suggestion" step and use
   it in place of the PAT secret:

   ```yaml
       steps:
         - name: Mint GitHub App token
           id: app-token
           uses: actions/create-github-app-token@v2
           with:
             app-id: ${{ secrets.PR_REVIEW_APP_ID }}
             private-key: ${{ secrets.PR_REVIEW_APP_PRIVATE_KEY }}

         - name: Apply suggestion
           env:
             # was: ${{ secrets.PR_REVIEW_APPLY_TOKEN }}
             GH_TOKEN: ${{ steps.app-token.outputs.token }}
   ```

   The token lasts one hour and is revoked when the job ends, which covers
   the workflow's 10-minute polling window.
5. Update the "PR_REVIEW_APPLY_TOKEN is not set" message and the header
   comment in `pr-apply.yml`, then delete the `PR_REVIEW_APPLY_TOKEN` secret
   and revoke the PAT.

Either way `apply_trusted_authors` is unaffected: it names who wrote the
*review* comments (`github-actions[bot]`, via `pr-review.yml`), not who pushes
the fix.

## Wiring up Azure DevOps

1. Create the `pr-review-agent` variable group (see the header of
   `pipelines/azure-devops/pr-review.yml` for the exact variables).
2. Add `pr-review.yml` as a **Build Validation** branch policy on the branches
   you want reviewed.
3. Optionally add `pr-review-agent/ai-code-review` as a required **Status
   Check** policy so the review itself blocks the merge.

The PAT needs Code (Read), Pull Request Threads (Read & Write) and Code Status
(Read & Write).

## Re-runs are safe

Every comment carries a hidden marker derived from file, line and rule — not
the wording. Re-running on a new push posts only genuinely new findings,
resolves threads whose finding is gone, and stays silent when the outcome has
not changed.

## Tracing

Every review is an MLflow trace tagged with the PR slug, source commit and the
configuration fingerprint, in the experiment created by `quickstart`. The trace
id is included in the summary comment.
