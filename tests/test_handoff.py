"""Scan-to-phone handoff: the one-time code store and its API.

The guarantees under test: a code works once, for the person who made it,
for two minutes; the token never appears in the QR, a URL or a log line; and
opening the redeem page does not spend the code (link scanners open links).
"""

import logging
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.jobs import InMemoryJobStore, JobRunner
from app.core.models import PullRequestRef
from app.workspace import build_workspace_router
from app.workspace.handoff import HandoffStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workspace-pr"
EMAIL = "dev@example.com"
TOKEN = "ghp_secretsecretsecretsecret"
REF = PullRequestRef(scm="github", repository="acme/platform", pull_request_id="7")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for name in ("DATABRICKS_APP_NAME", "PRREVIEW_WORKSPACE_USERS", "PRREVIEW_WORKSPACE_DEV_EMAIL"):
        monkeypatch.delenv(name, raising=False)


# -- the store ---------------------------------------------------------------------------


def test_a_code_redeems_exactly_once():
    store = HandoffStore()
    code = store.create(token=TOKEN, ref=REF, email=EMAIL)
    entry, reason = store.redeem(code, email=EMAIL)
    assert reason is None
    assert entry.token == TOKEN and entry.ref == REF
    assert store.redeem(code, email=EMAIL) == (None, "expired_or_unknown")


def test_a_code_expires():
    clock = Clock()
    store = HandoffStore(ttl=120, clock=clock)
    code = store.create(token=TOKEN, ref=REF, email=EMAIL)
    clock.now += 119
    assert len(store) == 1
    clock.now += 2
    assert store.redeem(code, email=EMAIL) == (None, "expired_or_unknown")
    assert len(store) == 0


def test_someone_else_cannot_redeem_and_burns_the_code():
    store = HandoffStore()
    code = store.create(token=TOKEN, ref=REF, email=EMAIL)
    assert store.redeem(code, email="thief@example.com") == (None, "different_user")
    # Burned: a photographed QR is now useless to its owner too, never to a thief.
    assert store.redeem(code, email=EMAIL) == (None, "expired_or_unknown")


def test_only_the_creator_can_withdraw_a_code():
    store = HandoffStore()
    code = store.create(token=TOKEN, ref=REF, email=EMAIL)
    store.discard(code, email="other@example.com")
    assert len(store) == 1
    store.discard(code, email=EMAIL)
    assert len(store) == 0


def test_codes_are_random_and_never_stored_in_the_clear():
    store = HandoffStore()
    codes = {store.create(token=TOKEN, ref=REF, email=EMAIL) for _ in range(50)}
    assert len(codes) == 50
    assert all(len(code) >= 40 for code in codes)
    assert not codes & set(store._entries)


def test_the_store_is_bounded():
    clock = Clock()
    store = HandoffStore(capacity=3, clock=clock)
    first = store.create(token=TOKEN, ref=REF, email=EMAIL)
    for _ in range(3):
        clock.now += 1
        store.create(token=TOKEN, ref=REF, email=EMAIL)
    assert len(store) == 3
    assert store.redeem(first, email=EMAIL) == (None, "expired_or_unknown")


# -- the API -----------------------------------------------------------------------------


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return HandoffStore(clock=clock)


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(build_workspace_router(JobRunner(InMemoryJobStore()), handoffs=store))
    with TestClient(app, base_url="https://app.example.com") as client:
        yield client


@pytest.fixture
def body(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "pr")
    return {"scm": "fake", "repository": "platform", "pull_request_id": "7",
            "fixture_dir": str(tmp_path / "pr")}


def h(email=EMAIL, token=TOKEN, **extra):
    out = {"x-forwarded-email": email, **extra}
    if token:
        out["x-scm-token"] = token
    return out


def test_create_returns_a_qr_and_never_the_token(client, body, caplog):
    caplog.set_level(logging.DEBUG)
    response = client.post("/workspace/api/handoff", json=body, headers=h())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["svg"].startswith("<svg")
    assert payload["expires_in"] == 120
    assert TOKEN not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in caplog.text
    assert payload["code"] not in caplog.text


def test_create_needs_a_token(client, body):
    response = client.post("/workspace/api/handoff", json=body, headers=h(token=None))
    assert response.status_code == 401


def test_create_from_another_site_is_refused(client, body, store):
    response = client.post(
        "/workspace/api/handoff", json=body, headers=h(**{"sec-fetch-site": "cross-site"})
    )
    assert response.status_code == 403
    assert len(store) == 0


def test_redeem_hands_the_token_and_the_pull_request_to_the_phone(client, body, caplog):
    caplog.set_level(logging.DEBUG)
    code = client.post("/workspace/api/handoff", json=body, headers=h()).json()["code"]
    # The phone has no token yet: only its Databricks sign-in.
    phone = h(token=None)
    redeemed = client.post("/workspace/api/handoff/redeem", json={"code": code}, headers=phone)
    assert redeemed.status_code == 200
    assert redeemed.headers["cache-control"] == "no-store"
    payload = redeemed.json()
    assert payload["token"] == TOKEN
    assert payload["scm"] == "fake"
    assert payload["next"].startswith("/workspace/m/pr?")
    assert "pull_request_id=7" in payload["next"] and "repository=platform" in payload["next"]
    assert TOKEN not in payload["next"]
    assert TOKEN not in caplog.text

    again = client.post("/workspace/api/handoff/redeem", json={"code": code}, headers=phone)
    assert again.status_code == 410
    assert again.json()["reason"] == "expired_or_unknown"
    assert "scan again" in again.json()["error"]


def test_redeem_by_another_account_is_refused(client, body):
    code = client.post("/workspace/api/handoff", json=body, headers=h()).json()["code"]
    response = client.post(
        "/workspace/api/handoff/redeem", json={"code": code}, headers=h(email="x@example.com")
    )
    assert response.status_code == 410
    assert response.json()["reason"] == "different_user"
    assert TOKEN not in response.text


def test_redeem_after_two_minutes_fails(client, body, clock):
    code = client.post("/workspace/api/handoff", json=body, headers=h()).json()["code"]
    clock.now += 121
    response = client.post("/workspace/api/handoff/redeem", json={"code": code}, headers=h())
    assert response.status_code == 410


def test_opening_the_redeem_page_does_not_spend_the_code(client, body, store):
    code = client.post("/workspace/api/handoff", json=body, headers=h()).json()["code"]
    page = client.get(f"/workspace/m/redeem?code={code}", headers=h(token=None))
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "no-referrer"
    assert len(store) == 1
    assert code not in page.text  # the script reads it from the URL, the page never echoes it


def test_closing_the_qr_withdraws_the_code(client, body, store):
    code = client.post("/workspace/api/handoff", json=body, headers=h()).json()["code"]
    assert client.delete(f"/workspace/api/handoff/{code}", headers=h()).status_code == 204
    assert len(store) == 0


def test_the_qr_points_at_this_app_behind_the_proxy(client, body, store, monkeypatch):
    seen = {}
    import segno

    real = segno.make

    def spy(content, **kwargs):
        seen["url"] = content
        return real(content, **kwargs)

    monkeypatch.setattr(segno, "make", spy)
    headers = h(**{"x-forwarded-host": "pr-review.cloud.databricksapps.com",
                   "x-forwarded-proto": "https"})
    code = client.post("/workspace/api/handoff", json=body, headers=headers).json()["code"]
    assert seen["url"] == (
        f"https://pr-review.cloud.databricksapps.com/workspace/m/redeem?code={code}"
    )
    assert TOKEN not in seen["url"]
