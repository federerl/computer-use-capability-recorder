"""Target-app smoke tests.

These do not test the automation - they pin the *target* behaviour that every
later milestone depends on. If a fault stops reproducing, the evidence runs that
rely on it silently become fiction, so each one gets a test.
"""

from __future__ import annotations

import pytest

from app import server
from app.seed import MEMBERS

MEMBER = "100482"
ACCT = "****4821"
OPEN_CHECK = "1043"
CLEARED_CHECK = "1009"


@pytest.fixture()
def client():
    server.FAULTS.clear()
    for m in MEMBERS.values():
        for a in m.accounts:
            for c in a.checks:
                if c.status == "stopped":
                    c.status = "open"
    app = server.create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def sign_on(client):
    r = client.post("/login", data={"f1": "roper", "f2": "meridian-demo-pw"})
    assert r.status_code == 302
    return client


def text(resp) -> str:
    return resp.get_data(as_text=True)


def arm(client, kind: str):
    r = client.post("/_control/fault", json={"kind": kind})
    assert r.status_code == 200, text(r)


# ------------------------------------------------------------------ happy path

def test_full_flow_reaches_confirmation(client):
    sign_on(client)

    body = text(client.get(f"/members?f3={MEMBER}"))
    assert "BARNES" in body and ">View</a>" in body

    body = text(client.get(f"/members/{MEMBER}"))
    assert 'name="detail"' in body, "detail pane must be framed"

    body = text(client.get(f"/members/{MEMBER}/accounts"))
    assert ACCT in body and "Stop Pay" in body

    body = text(client.get(f"/stoppay/new?m={MEMBER}&a={ACCT}"))
    assert "Submit Stop Payment" in body

    body = text(client.post("/stoppay", data={
        "m": MEMBER, "a": ACCT, "f5": OPEN_CHECK, "f6": "lost_check"}))
    assert "Stop Payment Confirmed" in body
    assert "Confirmation Number" in body
    assert "32.00" in body


def test_form_controls_have_no_accessible_name(client):
    """The stop-payment fields are deliberately unnamed: no label/for, no title,
    no aria. That is what forces a second targeting strategy to exist."""
    sign_on(client)
    body = text(client.get(f"/stoppay/new?m={MEMBER}&a={ACCT}"))
    field = body[body.index('name="f5"') - 40: body.index('name="f5"') + 40]
    assert "title=" not in field and "aria-label" not in field and "id=" not in field


# ------------------------------------------------------------- business outcomes

def test_member_not_found(client):
    sign_on(client)
    assert "No records match" in text(client.get("/members?f3=999999"))


def test_check_already_cleared(client):
    sign_on(client)
    body = text(client.post("/stoppay", data={
        "m": MEMBER, "a": ACCT, "f5": CLEARED_CHECK, "f6": "lost_check"}))
    assert "has already cleared" in body
    assert "Stop Payment Confirmed" not in body


def test_restricted_account(client):
    sign_on(client)
    r = client.get("/members/100633/accounts")
    assert r.status_code == 403
    assert "cannot service restricted accounts" in text(r)


# -------------------------------------------------------------------- faults

def test_system_notice_is_one_shot(client):
    sign_on(client)
    arm(client, "system_notice")
    first = text(client.get(f"/members?f3={MEMBER}"))
    assert 'aria-label="System Notice"' in first
    second = text(client.get(f"/members?f3={MEMBER}"))
    assert 'aria-label="System Notice"' not in second


def test_session_expired(client):
    sign_on(client)
    arm(client, "session_expired")
    r = client.get(f"/members?f3={MEMBER}")
    assert r.status_code == 401
    assert "Your session has expired" in text(r)


def test_security_challenge_blocks_until_answered(client):
    sign_on(client)
    arm(client, "security_challenge")

    body = text(client.get(f"/stoppay/new?m={MEMBER}&a={ACCT}"))
    assert "Security Verification Required" in body
    assert "Submit Stop Payment" not in body

    wrong = text(client.post("/stoppay/challenge", data={
        "m": MEMBER, "a": ACCT, "f9": "nowhere"}))
    assert "Answer not recognized" in wrong

    right = text(client.post("/stoppay/challenge", data={
        "m": MEMBER, "a": ACCT, "f9": "Fairfield"}))
    assert "Submit Stop Payment" in right


def test_wrong_confirmation_breaks_the_checkpoint(client):
    sign_on(client)
    arm(client, "wrong_confirmation")
    body = text(client.post("/stoppay", data={
        "m": MEMBER, "a": ACCT, "f5": OPEN_CHECK, "f6": "lost_check"}))
    assert "Stop Payment Confirmed" not in body
    assert "queued for overnight processing" in body


def test_relabel_submit(client):
    sign_on(client)
    arm(client, "relabel_submit")
    body = text(client.get(f"/stoppay/new?m={MEMBER}&a={ACCT}"))
    assert "Process Request" in body
    assert "Submit Stop Payment" not in body


def test_slow_load(client):
    import time
    sign_on(client)
    arm(client, "slow_load")
    t0 = time.monotonic()
    client.get(f"/members?f3={MEMBER}")
    assert time.monotonic() - t0 >= 5.5


# ------------------------------------------------------- fault controller safety

def test_control_endpoint_disabled_when_faults_off(client, monkeypatch):
    monkeypatch.setenv("FAULTS", "off")
    assert client.post("/_control/fault", json={"kind": "system_notice"}).status_code == 404
    assert client.post("/_control/reset").status_code == 404


def test_unknown_fault_rejected(client):
    r = client.post("/_control/fault", json={"kind": "make-it-work"})
    assert r.status_code == 400


def test_auth_required_everywhere(client):
    for path in (f"/members?f3={MEMBER}", f"/members/{MEMBER}",
                 f"/members/{MEMBER}/accounts", f"/stoppay/new?m={MEMBER}&a={ACCT}"):
        assert client.get(path).status_code == 401, path
