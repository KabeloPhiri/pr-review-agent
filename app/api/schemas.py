"""Request and response bodies for the REST API."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import EffectiveConfig
from app.core.jobs import Job, JobStatus
from app.core.models import PullRequestRef, ReviewResult


class ReviewMode(str, Enum):
    #: Wait for the review, but hand off to the background if it runs long.
    AUTO = "auto"
    #: Block until the review is done. Can exceed the app's connection timeout.
    SYNC = "sync"
    #: Return a job id immediately; comments are posted when the review ends.
    ASYNC = "async"


class ReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    scm: str = "azure_devops"
    organization: str | None = None
    project: str | None = None
    repository: str
    pull_request_id: str
    base_url: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)

    mode: ReviewMode = ReviewMode.AUTO
    publish: bool = Field(default=True, description="Post comments to the pull request")
    config: dict[str, Any] = Field(
        default_factory=dict, description="Per-request overrides of .prreview/config.yaml"
    )
    scm_token: str | None = Field(
        default=None,
        description="SCM token. Prefer the X-SCM-Token header; this field is a fallback.",
    )

    def to_ref(self) -> PullRequestRef:
        return PullRequestRef(
            scm=self.scm,
            organization=self.organization,
            project=self.project,
            repository=self.repository,
            pull_request_id=str(self.pull_request_id),
            base_url=self.base_url,
            extra=self.extra,
        )


class ReviewResponse(BaseModel):
    status: str = Field(description="completed | queued | running | failed")
    job_id: str | None = None
    result: ReviewResult | None = None
    error: str | None = None

    @classmethod
    def from_job(cls, job: Job) -> "ReviewResponse":
        return cls(
            status=job.status.value,
            job_id=job.job_id,
            result=job.result,
            error=job.error,
        )

    @classmethod
    def completed(cls, result: ReviewResult) -> "ReviewResponse":
        return cls(status=JobStatus.COMPLETED.value, result=result)


class ConfigRequest(ReviewRequest):
    """Same addressing as a review; resolves configuration without running one."""


class ConfigResponse(BaseModel):
    config: EffectiveConfig
    fingerprint: str
    repo_config_path: str
    repo_config_found: bool
    standards_sources: list[str] = Field(default_factory=list)
    available_plugins: dict[str, list[str]] = Field(default_factory=dict)
