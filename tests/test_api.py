"""HTTP-level tests for the REST routes.

The routes are mounted on a bare FastAPI app here rather than on the MLflow
`AgentServer`, so the API contract is testable without Databricks packages or
credentials. `app/server.py` mounts the same router.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import build_router
from app.core.errors import PrReviewError
from app.core.jobs import InMemoryJobStore, JobRunner
from app.core.models import Finding, Severity
from app.services.review.base import Reviewer, reviewer_registry


@reviewer_registry.register("api-stub")
class ApiStubReviewer(Reviewer):
    async def review(self, chunks, context):
        return [
            Finding(
                file=chunk.path,
                line=min(chunk.changed_lines) if chunk.changed_lines else None,
                severity=Severity.ERROR,
                rule_id="stub.rule",
                message="stub finding",
            )
            for chunk in chunks
        ]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(build_router(JobRunner(InMemoryJobStore())))

    @app.exception_handler(PrReviewError)
    async def _handler(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    return TestClient(app)


def _body(fixture_dir, **overrides):
    body = {
        "scm": "fake",
        "repository": "platform",
        "pull_request_id": "42",
        "extra": {"fixture_dir": str(fixture_dir)},
        "config": {"reviewer": "api-stub"},
    }
    body.update(overrides)
    return body


def test_sync_review_returns_the_result(client, fixture_dir):
    response = client.post("/review", json=_body(fixture_dir, mode="sync"))
    assert response.status_code == 200

    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["job_id"] is None
    result = payload["result"]
    assert result["files_reviewed"] == 2
    assert result["verdict"]["passed"] is False
    assert {f["file"] for f in result["findings"]} == {
        "jobs/sales_etl.py",
        "sql/daily_totals.sql",
    }


def test_auto_mode_completes_within_the_wait(client, fixture_dir):
    response = client.post("/review", json=_body(fixture_dir, mode="auto"))
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_async_mode_returns_a_job_that_can_be_polled(client, fixture_dir):
    response = client.post("/review", json=_body(fixture_dir, mode="async"))
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert response.json()["status"] in {"queued", "running"}

    for _ in range(50):
        polled = client.get(f"/review/{job_id}")
        if polled.json()["status"] == "completed":
            break
    assert polled.status_code == 200
    assert polled.json()["result"]["findings"]


def test_unknown_job_is_a_404_with_an_explanation(client):
    response = client.get("/review/does-not-exist")
    assert response.status_code == 404
    assert "replica" in response.json()["error"]


def test_effective_config_reports_repo_overrides_and_plugins(client, fixture_dir):
    response = client.post("/config/effective", json=_body(fixture_dir))
    assert response.status_code == 200

    payload = response.json()
    # The fixture repo sets severity_gate: warning in .prreview/config.yaml.
    assert payload["config"]["severity_gate"] == "warning"
    assert payload["repo_config_found"] is True
    assert payload["standards_sources"] == [".prreview/standards.md"]
    assert "azure_devops" in payload["available_plugins"]["scm"]
    assert payload["fingerprint"]


def test_unknown_plugin_is_a_400_listing_valid_names(client, fixture_dir):
    response = client.post(
        "/review", json=_body(fixture_dir, mode="sync", config={"reviewer": "nope"})
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Unknown reviewer: 'nope'"


def test_unknown_config_key_is_rejected(client, fixture_dir):
    response = client.post(
        "/review", json=_body(fixture_dir, mode="sync", config={"not_a_key": 1})
    )
    assert response.status_code == 400


def test_scm_token_header_reaches_the_connector(client, fixture_dir, monkeypatch):
    seen = {}

    from app.services.scm import fake

    original = fake.FakeScmConnector.__init__

    def spy(self, token=None, **options):
        seen["token"] = token
        original(self, token, **options)

    monkeypatch.setattr(fake.FakeScmConnector, "__init__", spy)
    client.post(
        "/review",
        json=_body(fixture_dir, mode="sync"),
        headers={"X-SCM-Token": "pat-from-pipeline"},
    )
    assert seen["token"] == "pat-from-pipeline"
