"""GitHub connector tests against a mocked REST API.

These assert the request shapes the 2022-11-28 API actually expects — URLs,
query parameters and JSON bodies — so a wrong field name fails here instead of
on a real pull request.
"""

import json

import httpx
import pytest

from app.core.errors import ScmAuthError, ScmError
from app.core.models import ChangeType, CommentDraft, GateLevel, PullRequestRef, Verdict
from app.services.scm.github import GitHubConnector

PATCH = (
    "@@ -1,2 +1,5 @@\n"
    " def load(spark):\n"
    "     return spark.read.table('t')\n"
    "+\n"
    "+def summarise(df):\n"
    "+    return df.collect()\n"
)


def _ref(repository="KabeloPhiri/platform", organization=None):
    return PullRequestRef(
        scm="github",
        organization=organization,
        repository=repository,
        pull_request_id="42",
    )


class FakeApi:
    """Minimal GitHub stand-in that records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.inline_comment_status = 201

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method

        if path == "/repos/KabeloPhiri/platform/pulls/42" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "title": "Add summary",
                    "body": "why",
                    "user": {"login": "KabeloPhiri"},
                    "head": {"ref": "feature", "sha": "src111"},
                    "base": {"ref": "main", "sha": "tgt222"},
                    "draft": False,
                    "html_url": "https://github.com/KabeloPhiri/platform/pull/42",
                },
            )

        if path.endswith("/pulls/42/files"):
            if int(request.url.params.get("page", 1)) > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {"filename": "jobs/etl.py", "status": "modified", "patch": PATCH},
                    # No patch: binary or too large. Kept, but not reviewable.
                    {"filename": "docs/logo.png", "status": "added"},
                ],
            )

        if "/contents/" in path and method == "GET":
            if path.endswith(".prreview/config.yaml"):
                return httpx.Response(200, text="severity_gate: warning\n")
            return httpx.Response(404, json={"message": "Not Found"})

        if path.endswith("/pulls/42/comments") and method == "GET":
            if int(request.url.params.get("page", 1)) > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "node_id": "RC_node7",
                        "body": "stale\n<!-- prreview:abc123 -->",
                        "path": "jobs/etl.py",
                        "line": 4,
                    },
                    {"id": 8, "node_id": "RC_node8", "body": "human", "path": "a.py", "line": None},
                ],
            )

        if path.endswith("/issues/42/comments") and method == "GET":
            if int(request.url.params.get("page", 1)) > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"id": 9, "node_id": "IC_node9", "body": "sum\n<!-- prreview:summary:z -->"}],
            )

        if path.endswith("/pulls/42/comments") and method == "POST":
            if self.inline_comment_status >= 400:
                return httpx.Response(
                    self.inline_comment_status, json={"message": "line must be part of the diff"}
                )
            return httpx.Response(201, json={"id": 99, "node_id": "RC_new"})

        if path.endswith("/issues/42/comments") and method == "POST":
            return httpx.Response(201, json={"id": 100, "node_id": "IC_new"})

        if path.endswith("/statuses/src111") and method == "POST":
            return httpx.Response(201, json={"id": 1})

        if path == "/graphql" and method == "POST":
            return httpx.Response(200, json={"data": {"minimizeComment": {"minimizedComment": {}}}})

        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})

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
    return GitHubConnector(token="ghp-token", transport=httpx.MockTransport(api.handler))


async def test_missing_token_is_rejected_before_any_call():
    connector = GitHubConnector(token=None)
    with pytest.raises(ScmAuthError):
        await connector.get_pull_request(_ref())


async def test_owner_repo_accepts_both_addressing_styles(connector, api):
    await connector.get_pull_request(_ref())
    assert api.find("/pulls/42").url.path == "/repos/KabeloPhiri/platform/pulls/42"
    await connector.aclose()


async def test_owner_can_come_from_organization(api):
    connector = GitHubConnector(token="t", transport=httpx.MockTransport(api.handler))
    await connector.get_pull_request(_ref(repository="platform", organization="KabeloPhiri"))
    assert api.find("/pulls/42").url.path == "/repos/KabeloPhiri/platform/pulls/42"
    await connector.aclose()


async def test_missing_owner_is_a_config_error(connector):
    with pytest.raises(ScmError):
        await connector.get_pull_request(_ref(repository="platform"))
    await connector.aclose()


async def test_get_pull_request_reads_head_and_base(connector, api):
    pr = await connector.get_pull_request(_ref())
    assert pr.source_commit == "src111"
    assert pr.target_commit == "tgt222"
    assert pr.author == "KabeloPhiri"
    assert pr.source_branch == "feature"
    assert api.find("/pulls/42").headers["x-github-api-version"] == "2022-11-28"
    assert api.find("/pulls/42").headers["authorization"] == "Bearer ghp-token"
    await connector.aclose()


async def test_diff_reheads_each_patch_into_hunks(connector, api):
    pr = await connector.get_pull_request(_ref())
    diff = await connector.get_diff(pr)

    assert api.find("/pulls/42/files").url.params["per_page"] == "100"
    assert [f.path for f in diff.files] == ["jobs/etl.py", "docs/logo.png"]

    file = diff.files[0]
    assert file.change_type is ChangeType.EDIT
    assert file.language == "python"
    # The patch adds new-file lines 3, 4 and 5.
    assert file.changed_line_numbers() == {3, 4, 5}

    # A file GitHub gave no patch for is carried but flagged unreviewable.
    assert diff.files[1].is_binary is True
    await connector.aclose()


async def test_get_file_text_reads_the_head_commit(connector, api):
    pr = await connector.get_pull_request(_ref())
    content = await connector.get_file_text(pr, ".prreview/config.yaml")

    assert content == "severity_gate: warning\n"
    request = api.find("/contents/.prreview/config.yaml")
    assert request.url.params["ref"] == "src111"
    assert request.headers["accept"] == "application/vnd.github.raw"
    await connector.aclose()


async def test_missing_file_is_none_not_an_error(connector):
    pr = await connector.get_pull_request(_ref())
    assert await connector.get_file_text(pr, ".prreview/nope.md") is None
    await connector.aclose()


async def test_list_comments_covers_inline_and_summary(connector):
    pr = await connector.get_pull_request(_ref())
    comments = await connector.list_comments(pr)
    markers = {c.marker for c in comments}

    assert "prreview:abc123" in markers
    assert "prreview:summary:z" in markers

    inline = next(c for c in comments if c.marker == "prreview:abc123")
    assert inline.thread_id == "review:RC_node7"
    assert inline.file == "jobs/etl.py"
    assert inline.line == 4
    assert inline.is_closed is False

    # An inline comment GitHub has unanchored counts as already closed.
    assert next(c for c in comments if c.thread_id == "review:RC_node8").is_closed is True
    await connector.aclose()


async def test_inline_comment_is_anchored_to_the_head_commit(connector, api):
    pr = await connector.get_pull_request(_ref())
    thread_id = await connector.post_comment(
        pr, CommentDraft(body="body", marker="prreview:x", file="jobs/etl.py", line=4)
    )

    assert thread_id == "review:RC_new"
    body = json.loads(api.find("/pulls/42/comments", method="POST").content)
    assert body["commit_id"] == "src111"
    assert body["path"] == "jobs/etl.py"
    assert body["line"] == 4
    assert body["side"] == "RIGHT"
    assert "start_line" not in body
    await connector.aclose()


async def test_multi_line_finding_sets_the_start_line(connector, api):
    pr = await connector.get_pull_request(_ref())
    await connector.post_comment(
        pr,
        CommentDraft(body="body", marker="prreview:x", file="jobs/etl.py", line=4, end_line=5),
    )
    body = json.loads(api.find("/pulls/42/comments", method="POST").content)
    # GitHub wants the range as start_line..line, not line..end_line.
    assert body["start_line"] == 4
    assert body["line"] == 5
    assert body["start_side"] == "RIGHT"
    await connector.aclose()


async def test_summary_comment_goes_to_the_conversation_tab(connector, api):
    pr = await connector.get_pull_request(_ref())
    thread_id = await connector.post_comment(
        pr, CommentDraft(body="summary", marker="prreview:s", is_summary=True)
    )
    assert thread_id == "issue:IC_new"
    body = json.loads(api.find("/issues/42/comments", method="POST").content)
    assert body == {"body": "summary"}
    await connector.aclose()


async def test_rejected_inline_comment_falls_back_to_a_pr_comment(connector, api):
    """A finding must not be lost because GitHub refused to anchor it."""
    api.inline_comment_status = 422
    pr = await connector.get_pull_request(_ref())
    thread_id = await connector.post_comment(
        pr, CommentDraft(body="body", marker="prreview:x", file="jobs/etl.py", line=4)
    )

    assert thread_id == "issue:IC_new"
    # The body is posted verbatim, so the hidden marker still travels with it
    # and the next run recognises the finding as already reported.
    assert json.loads(api.find("/issues/42/comments", method="POST").content)["body"] == "body"
    await connector.aclose()


async def test_set_status_maps_the_verdict(connector, api):
    pr = await connector.get_pull_request(_ref())
    await connector.set_status(
        pr,
        Verdict(passed=False, gate=GateLevel.ERROR, reason="1 error"),
        "1 error, 0 warning, 0 info",
        name="ai-code-review",
        genre="pr-review-agent",
    )
    request = api.find("/statuses/src111", method="POST")
    body = json.loads(request.content)
    assert body["state"] == "failure"
    # Branch protection matches on this string, so it must stay stable.
    assert body["context"] == "pr-review-agent/ai-code-review"
    assert body["target_url"] == "https://github.com/KabeloPhiri/platform/pull/42"
    await connector.aclose()


async def test_passing_verdict_is_a_success_status(connector, api):
    pr = await connector.get_pull_request(_ref())
    await connector.set_status(
        pr, Verdict(passed=True, gate=GateLevel.ERROR, reason="clean"), "No findings"
    )
    assert json.loads(api.find("/statuses/src111", method="POST").content)["state"] == "success"
    await connector.aclose()


async def test_close_comment_minimises_through_graphql(connector, api):
    pr = await connector.get_pull_request(_ref())
    await connector.close_comment(pr, "review:RC_node7")

    body = json.loads(api.find("/graphql", method="POST").content)
    assert body["variables"]["id"] == "RC_node7"
    assert "minimizeComment" in body["query"]
    await connector.aclose()


async def test_unauthorised_is_an_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    connector = GitHubConnector(token="bad", transport=httpx.MockTransport(handler))
    with pytest.raises(ScmAuthError):
        await connector.get_pull_request(_ref())
    await connector.aclose()


async def test_spent_rate_limit_is_not_reported_as_an_auth_problem():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
        )

    connector = GitHubConnector(token="t", transport=httpx.MockTransport(handler))
    with pytest.raises(ScmError) as exc:
        await connector.get_pull_request(_ref())
    assert not isinstance(exc.value, ScmAuthError)
    assert "rate limit" in exc.value.message.lower()
    await connector.aclose()
