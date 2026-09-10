"""Azure DevOps connector tests against a mocked REST API.

These assert the request shapes the 7.1 API actually expects — URLs, query
parameters and JSON bodies — so a wrong field name fails here instead of on a
real pull request.
"""

import json

import httpx
import pytest

from app.core.errors import ScmAuthError
from app.core.models import ChangeType, CommentDraft, GateLevel, PullRequestRef, Verdict
from app.services.scm.azure_devops import AzureDevOpsConnector

OLD_FILE = "def load(spark):\n    return spark.read.table('t')\n"
NEW_FILE = "def load(spark):\n    return spark.read.table('t')\n\ndef summarise(df):\n    return df.collect()\n"


def _ref():
    return PullRequestRef(
        scm="azure_devops",
        organization="contoso",
        project="data",
        repository="platform",
        pull_request_id="42",
    )


class FakeApi:
    """Minimal Azure DevOps stand-in that records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path.endswith("/pullrequests/42"):
            return httpx.Response(
                200,
                json={
                    "title": "Add summary",
                    "description": "",
                    "createdBy": {"uniqueName": "dev@contoso.com"},
                    "sourceRefName": "refs/heads/feature",
                    "targetRefName": "refs/heads/main",
                    "lastMergeSourceCommit": {"commitId": "src111"},
                    "lastMergeTargetCommit": {"commitId": "tgt222"},
                    "isDraft": False,
                    "_links": {"web": {"href": "https://dev.azure.com/contoso/pr/42"}},
                },
            )

        if path.endswith("/diffs/commits"):
            return httpx.Response(
                200,
                json={
                    "allChangesIncluded": True,
                    "commonCommit": "base000",
                    "changes": [
                        {
                            "item": {"path": "/jobs/etl.py", "gitObjectType": "blob"},
                            "changeType": "edit",
                        },
                        {
                            "item": {"path": "/jobs", "gitObjectType": "tree", "isFolder": True},
                            "changeType": "edit",
                        },
                    ],
                },
            )

        if path.endswith("/items"):
            version = request.url.params.get("versionDescriptor.version")
            content = OLD_FILE if version == "tgt222" else NEW_FILE
            if request.url.params.get("path") == "/.prreview/config.yaml":
                content = "severity_gate: warning\n"
            return httpx.Response(200, json={"content": content})

        if path.endswith("/threads") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": 7,
                            "status": "active",
                            "comments": [{"content": "stale\n<!-- prreview:abc123 -->"}],
                            "threadContext": {
                                "filePath": "/jobs/etl.py",
                                "rightFileStart": {"line": 4},
                            },
                        },
                        {"id": 8, "status": "fixed", "comments": [{"content": "human comment"}]},
                    ]
                },
            )

        if path.endswith("/threads") and request.method == "POST":
            return httpx.Response(200, json={"id": 99})

        if "/threads/" in path and request.method == "PATCH":
            return httpx.Response(200, json={"id": 7, "status": "fixed"})

        if path.endswith("/statuses"):
            return httpx.Response(200, json={"id": 1})

        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})

    def find(self, needle: str, method: str = "GET") -> httpx.Request:
        for request in self.requests:
            if needle in str(request.url) and request.method == method:
                return request
        raise AssertionError(f"no {method} request matching {needle!r} in {self.requests}")


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def connector(api):
    return AzureDevOpsConnector(token="pat-token", transport=httpx.MockTransport(api.handler))


async def test_missing_token_is_rejected_before_any_call():
    connector = AzureDevOpsConnector(token=None)
    with pytest.raises(ScmAuthError):
        await connector.get_pull_request(_ref())


async def test_get_pull_request_reads_merge_commits(connector, api):
    pr = await connector.get_pull_request(_ref())
    assert pr.source_commit == "src111"
    assert pr.target_commit == "tgt222"
    assert pr.author == "dev@contoso.com"
    assert api.find("/pullrequests/42").url.params["api-version"] == "7.1"
    await connector.aclose()


async def test_diff_uses_the_merge_base_and_builds_hunks(connector, api):
    pr = await connector.get_pull_request(_ref())
    diff = await connector.get_diff(pr)

    params = api.find("/diffs/commits").url.params
    assert params["diffCommonCommit"] == "true"
    assert params["baseVersion"] == "tgt222"
    assert params["targetVersion"] == "src111"
    assert params["baseVersionType"] == "commit"

    # The folder entry is dropped; only the blob is diffed.
    assert [f.path for f in diff.files] == ["jobs/etl.py"]
    file = diff.files[0]
    assert file.change_type is ChangeType.EDIT
    assert file.language == "python"
    assert file.changed_line_numbers() == {3, 4, 5}
    await connector.aclose()


async def test_get_file_text_reads_the_source_commit(connector, api):
    pr = await connector.get_pull_request(_ref())
    content = await connector.get_file_text(pr, ".prreview/config.yaml")

    assert content == "severity_gate: warning\n"
    params = api.find("path=%2F.prreview").url.params
    assert params["versionDescriptor.version"] == "src111"
    assert params["versionDescriptor.versionType"] == "commit"
    await connector.aclose()


async def test_list_comments_extracts_markers(connector):
    pr = await connector.get_pull_request(_ref())
    comments = await connector.list_comments(pr)

    assert comments[0].marker == "prreview:abc123"
    assert comments[0].file == "jobs/etl.py"
    assert comments[0].line == 4
    assert comments[0].is_closed is False
    assert comments[1].marker is None
    assert comments[1].is_closed is True
    await connector.aclose()


async def test_post_comment_sends_thread_context(connector, api):
    pr = await connector.get_pull_request(_ref())
    thread_id = await connector.post_comment(
        pr, CommentDraft(body="body", marker="prreview:x", file="jobs/etl.py", line=4)
    )

    assert thread_id == "99"
    body = json.loads(api.find("/threads", method="POST").content)
    assert body["comments"][0]["commentType"] == "text"
    assert body["comments"][0]["parentCommentId"] == 0
    assert body["status"] == "active"
    assert body["threadContext"]["filePath"] == "/jobs/etl.py"
    assert body["threadContext"]["rightFileStart"] == {"line": 4, "offset": 1}
    await connector.aclose()


async def test_summary_comment_has_no_thread_context(connector, api):
    pr = await connector.get_pull_request(_ref())
    await connector.post_comment(pr, CommentDraft(body="summary", marker="prreview:s", is_summary=True))
    body = json.loads(api.find("/threads", method="POST").content)
    assert "threadContext" not in body
    await connector.aclose()


async def test_set_status_maps_the_verdict(connector, api):
    pr = await connector.get_pull_request(_ref())
    await connector.set_status(
        pr,
        Verdict(passed=False, gate=GateLevel.ERROR, reason="1 error"),
        "1 error, 0 warning",
        name="ai-code-review",
        genre="pr-review-agent",
    )
    body = json.loads(api.find("/statuses", method="POST").content)
    assert body["state"] == "failed"
    assert body["context"] == {"name": "ai-code-review", "genre": "pr-review-agent"}
    assert body["targetUrl"] == "https://dev.azure.com/contoso/pr/42"
    await connector.aclose()


async def test_close_comment_marks_the_thread_fixed(connector, api):
    pr = await connector.get_pull_request(_ref())
    await connector.close_comment(pr, "7")
    body = json.loads(api.find("/threads/7", method="PATCH").content)
    assert body == {"status": "fixed"}
    await connector.aclose()


async def test_html_response_is_treated_as_an_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(203, text="<html>sign in</html>", headers={"content-type": "text/html"})

    connector = AzureDevOpsConnector(token="bad", transport=httpx.MockTransport(handler))
    with pytest.raises(ScmAuthError):
        await connector.get_pull_request(_ref())
    await connector.aclose()
