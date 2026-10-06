"""Admin console pages: access control, rendering, filters and the UI rules.

The console is mounted on a bare FastAPI app with a fake trace source, so
these tests need neither MLflow nor Databricks.
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.console import build_console_router
from app.console.analytics import REVIEW, TraceRecord
from app.core.jobs import InMemoryJobStore, JobRunner

ADMIN = "admin@example.com"
VIEWER = "viewer@example.com"
A = "github/acme__payments"
B = "github/acme__ledger"

PAGES = [
    "/console",
    "/console/repositories",
    "/console/reviews",
    "/console/quality",
    "/console/config",
    "/console/standards",
    "/console/prompts",
]

#: Emoji and pictographic/symbol blocks. Plain punctuation (dashes, '#') is fine.
ORNAMENT = re.compile(
    "[\U0001f000-\U0001faff←-⇿⌀-⏿■-➿⤀-⥿"
    "⬀-⯿️]"
)


def _record(repo, n, *, passed=True, tokens=100):
    return TraceRecord(
        trace_id=f"tr-{repo}-{n}",
        kind=REVIEW,
        timestamp_ms=1_791_000_000_000 + n,
        duration_ms=1500,
        state="OK",
        repo=repo,
        pr=str(n),
        model="databricks-claude-sonnet-5-5",
        input_tokens=tokens,
        output_tokens=10,
        total_tokens=tokens + 10,
        tags={"prreview.passed": str(passed), "prreview.findings": "2"},
    )


RECORDS = [_record(A, 1), _record(A, 2, passed=False), _record(B, 3, tokens=900)]


def _client(source=lambda start, end: RECORDS) -> TestClient:
    app = FastAPI()
    app.include_router(build_console_router(JobRunner(InMemoryJobStore()), trace_source=source))
    return TestClient(app)


@pytest.fixture(autouse=True)
def allowlists(monkeypatch):
    monkeypatch.setenv("PRREVIEW_CONSOLE_ADMINS", f"{ADMIN}, other-admin@example.com")
    monkeypatch.setenv("PRREVIEW_CONSOLE_VIEWERS", VIEWER)


def _as(email):
    return {"X-Forwarded-Email": email}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _as("stranger@example.com"),
        # The GitHub Actions service principal: CAN_USE on the app, not a console user.
        _as("eaf382c6-9e28-4ddb-9754-472edc184a2b"),
    ],
)
def test_everyone_not_on_a_list_is_refused(headers):
    response = _client().get("/console", headers=headers)
    assert response.status_code == 403
    assert "Access denied" in response.text


@pytest.mark.parametrize("email, role", [(ADMIN, "Admin"), (VIEWER, "Viewer")])
def test_allowlisted_users_see_their_role(email, role):
    response = _client().get("/console", headers=_as(email.upper()))  # case-insensitive
    assert response.status_code == 200
    assert f'<span class="role">{role}</span>' in response.text


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(path):
    response = _client().get(path, headers=_as(ADMIN))
    assert response.status_code == 200, response.text[:500]
    assert "<nav" in response.text


@pytest.mark.parametrize("path", PAGES + ["/console/standards?name=python"])
def test_pages_carry_no_icons_or_emoji(path):
    """Production console: text labels only, no decorative symbols."""
    html = _client().get(path, headers=_as(ADMIN)).text
    found = ORNAMENT.findall(html)
    assert not found, f"{path} contains {found!r}"


def test_repository_filter_narrows_every_figure():
    everything = _client().get("/console/reviews", headers=_as(ADMIN)).text
    only_b = _client().get(f"/console/reviews?repo={B}", headers=_as(ADMIN)).text
    assert A in everything and B in everything
    assert B in only_b
    assert f"<td>{A}</td>" not in only_b


def test_repositories_page_lists_each_repository_and_sorts():
    """Both directions, so the test cannot pass on alphabetical or default
    order: B (ledger) sorts first by name and by last review, A by fewer tokens."""

    def order(query):
        html = _client().get(f"/console/repositories?{query}", headers=_as(ADMIN)).text
        table = html.split("<tbody>", 1)[1]  # the top bar lists repos alphabetically
        return "A" if table.index(f">{A}<") < table.index(f">{B}<") else "B", html

    first, html = order("sort=total_tokens&dir=desc")
    assert first == "B" and 'aria-sort="descending"' in html
    first, html = order("sort=total_tokens&dir=asc")
    assert first == "A" and 'aria-sort="ascending"' in html


def test_unreadable_traces_show_an_error_not_a_crash():
    def broken(start, end):
        raise RuntimeError("tracking server unavailable")

    response = _client(broken).get("/console", headers=_as(ADMIN))
    assert response.status_code == 200
    assert "Could not read review traces" in response.text
    assert "tracking server unavailable" in response.text


def test_empty_range_shows_empty_states():
    html = _client(lambda s, e: []).get("/console/reviews", headers=_as(ADMIN)).text
    assert "No reviews in this range" in html


def test_config_page_names_the_layer_each_value_comes_from(monkeypatch):
    monkeypatch.setenv("PRREVIEW_SEVERITY_GATE", "warning")
    html = _client().get("/console/config", headers=_as(ADMIN)).text
    match = re.search(r"<code>severity_gate</code>.*?</tr>", html, re.DOTALL)
    assert match, "the severity_gate row is missing from the config page"
    assert "warning" in match.group(0) and "Environment" in match.group(0)


def test_prompts_page_shows_guidance_and_the_fixed_output_format():
    html = _client().get("/console/prompts", headers=_as(ADMIN)).text
    assert "meticulous senior code reviewer" in html
    assert "Output format (fixed)" in html
    assert "Return JSON only" in html


def test_invalid_days_falls_back_to_the_default():
    html = _client().get("/console?days=999", headers=_as(ADMIN)).text
    assert "last 7 days" in html


def test_false_positives_page_shows_rates_and_flags():
    from app.console.analytics import FEEDBACK

    records = RECORDS + [
        TraceRecord(
            trace_id="fb-1", kind=FEEDBACK, timestamp_ms=1_791_000_000_100, duration_ms=5,
            state="OK", repo=A, pr="1", model="databricks-claude-sonnet-5-5",
            tags={"prreview.feedback": "false_positive", "prreview.rule": "python.naming",
                  "prreview.finding_model": "databricks-claude-sonnet-5-5",
                  "prreview.fp_reason": "matches our convention", "prreview.flagged_by": "dev",
                  "prreview.file": "a.py"},
        ),
        TraceRecord(
            trace_id="rv-9", kind=REVIEW, timestamp_ms=1_791_000_000_050, duration_ms=5,
            state="OK", repo=A, pr="1", model="databricks-claude-sonnet-5-5",
            tags={"prreview.passed": "False", "prreview.findings": "4",
                  "prreview.rules": '{"python.naming": 4}'},
        ),
    ]
    html = _client(lambda s, e: records).get("/console/quality", headers=_as(ADMIN)).text
    assert "matches our convention" in html
    assert "25.0%" in html  # 1 flag over 4 posted
    assert 'href="/console/standards?' in html  # the python family links to its standard
