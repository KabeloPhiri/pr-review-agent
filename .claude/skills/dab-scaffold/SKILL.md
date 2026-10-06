---
name: dab-scaffold
description: Initialise or extend a Databricks Asset Bundle project with the mandated folder structure, pinned requirements, jobs/pipelines as YAML, and a CI/CD workflow. Use when starting a new pipeline project, adding a job/pipeline/cluster, or when any resource would otherwise be created by hand in the workspace UI.
---

# Databricks Asset Bundle scaffold

CLAUDE.md: "Preference on Databricks asset bundles ... is not negotiable. Jobs, computes, scripts etc should be deployed via CI/CD pipelines." Anything you would click in the UI must instead be YAML under `resources/` and deployed by the pipeline.

## Target structure

```
my_data_pipeline/
├── databricks.yml              # bundle + targets (dev/uat/prod); hosts from CI variables
├── resources/
│   ├── pipeline_job.yml        # jobs, job clusters, schedules, notifications, tags
│   └── dlt_pipeline.yml        # Lakeflow (DLT) pipelines
├── src/
│   ├── __init__.py
│   ├── ingest.py               # bronze ingestion (Auto Loader / batch)
│   ├── transform.py            # silver/gold transforms, SCD2 via utils.scd2
│   └── utils/ {__init__.py, helpers.py, scd2.py}
├── monitoring/*.sql            # system-table health queries run as sql_task
├── tests/ {test_transform.py, test_integration.py}
├── setup.py                    # wheel packaging (python_wheel_task)
├── requirements.txt            # PINNED versions
└── .github/workflows/dab-deploy.yml
```

## Procedure

1. **Init** - prefer the official generator, then overlay the project conventions:
   ```bash
   databricks bundle init default-python --output-dir <project>   # or: mlops-stacks / sql
   ```
   If the CLI is unavailable, copy `templates/dab/` as the skeleton (same layout).
2. **Overlay templates** from this toolbox (`templates/dab/*`): `databricks.yml` targets & RBAC permissions, `resources/pipeline_job.yml`, `resources/dlt_pipeline.yml`, `requirements.txt`, `setup.py`, `monitoring/job_run_health.sql`, `src/utils/scd2.py`, `tests/test_scd2.py`, `.github/workflows/dab-deploy.yml`. Rename `my_data_pipeline` everywhere (bundle name, package, entry points).
3. **Pin libraries**: every line in `requirements.txt` has `==`. Cluster-level libs go in an init script under `init_scripts/` referenced from the job cluster - also pinned. Unpinned = blocking finding.
4. **Security defaults in YAML**: `data_security_mode: USER_ISOLATION`, `run_as` service principal for uat/prod, `permissions` by **group** (never individual users), tags (`costCenter`, `environment`), no `spark_conf` containing credentials (use `{{secrets/scope/key}}`). No public IP is a workspace/network property - note it in the design, don't try to set it in the bundle.
5. **Validate locally, deploy only via CI**:
   ```bash
   databricks bundle validate -t dev
   databricks bundle deploy   -t dev     # dev only, and only if the user asks; uat/prod are CI-only
   databricks bundle run      -t dev <jobKey>
   ```
6. **CI/CD**: `dab-deploy.yml` runs secret scan -> pytest -> `bundle validate` on PRs; deploys dev on `main`, uat on `v*` tags, prod after environment approval. Adapt to Azure DevOps / GitLab if the repo uses them (same stages). Credentials are pipeline secrets; hosts are pipeline variables passed with `--var`.
7. **Branching**: scaffold on a `feature/<name>` branch, open a PR; never commit straight to `main` (see `cicd-devops`).

## Review checklist for an existing bundle

- [ ] Every job/pipeline/cluster in the workspace has a YAML twin (`databricks jobs list --output json` vs `resources/`).
- [ ] `mode: production` + `run_as` for uat/prod; `mode: development` for dev.
- [ ] No hard-coded `host:` - variables only.
- [ ] Wheel built by `artifacts`, not uploaded manually.
- [ ] Monitoring `sql_task` or job present (see `workspace-monitoring`).
- [ ] `requirements.txt` pinned; runtime `spark_version` pinned (no `-latest`).
- [ ] Secret scan clean (`secret-scan`).
