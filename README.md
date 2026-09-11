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
model_endpoint: databricks-llama-4-maverick
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

The workflow's own `GITHUB_TOKEN` is the SCM token — there is no PAT to
manage — but it needs `pull-requests: write` (to comment) and
`statuses: write` (to publish the gate). Running the agent against a repo from
*outside* Actions needs a classic PAT with `repo`, or a fine-grained token with
Pull requests (Read & write), Contents (Read) and Commit statuses (Read &
write).

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
