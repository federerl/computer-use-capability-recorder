"""Meridian CU Back Office - the target application.

A deliberately legacy-shaped stand-in for the back-office systems this project
exists to automate: server-rendered, table-laid-out, framed, no test IDs, and
control names that carry no meaning.

Runtime faults are injected *into this app* through /_control/fault, never into
the automation. /_control/ sits on the automation policy deny list, so the agent
cannot reach the lever that would let it cheat.
"""

from __future__ import annotations

import argparse
import os
import random
import time
from datetime import datetime, timezone

from flask import (
    Blueprint, Flask, redirect, render_template, request, session, url_for,
)

from .seed import MEMBERS, OPERATORS

TENANTS = {
    "meridian": {
        "brand": "Meridian CU - Back Office",
        "member_label": "Member Number",
        "accent": "#1f3a5f",
        "prefix": "",
    },
    "pinnacle": {
        "brand": "Pinnacle Financial :: Servicing Console",
        "member_label": "Account Holder ID",
        "accent": "#5f1f2e",
        "prefix": "/svc",
    },
}

# Sticky until consumed or reset. Single process, so a module dict is the whole store.
FAULTS: dict[str, bool] = {}

FAULT_KINDS = (
    "system_notice",       # unexpected interstitial - a declared recoverable
    "session_expired",     # session dies mid-flow - a declared recoverable
    "slow_load",           # 6s search - bounded wait
    "security_challenge",  # UNDECLARED blocking dialog - drives escalation
    "wrong_confirmation",  # lands somewhere that is not the confirmation - checkpoint failure
    "relabel_submit",      # submit control renamed - locator exhaustion
)

bp = Blueprint("app", __name__)


def cfg() -> dict:
    return TENANTS[os.environ.get("TENANT", "meridian")]


def take(kind: str) -> bool:
    """Consume a one-shot fault."""
    return bool(FAULTS.pop(kind, False))


def peek(kind: str) -> bool:
    return FAULTS.get(kind, False)


def notice() -> bool:
    """The interstitial fires once, on whichever page renders next."""
    return take("system_notice")


def authed() -> bool:
    if take("session_expired"):
        session.clear()
        return False
    return bool(session.get("user"))


def guard():
    """Return a rendered response if the caller may not proceed, else None."""
    if not authed():
        return render_template("expired.html", c=cfg()), 401
    return None


# --------------------------------------------------------------------------- auth

@bp.route("/", methods=["GET"])
def root():
    return redirect(url_for("app.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        # Meaningless control names are the point.
        user, pw = request.form.get("f1", ""), request.form.get("f2", "")
        if OPERATORS.get(user) == pw:
            session["user"] = user
            return redirect(url_for("app.members"))
        error = "Sign-on failed. Check your credentials and try again."
    return render_template("login.html", c=cfg(), error=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("app.login"))


# ------------------------------------------------------------------------ search

@bp.route("/members", methods=["GET"])
def members():
    if (g := guard()):
        return g
    if take("slow_load"):
        time.sleep(6)

    q = (request.args.get("f3") or "").strip()
    searched = bool(q)
    m = MEMBERS.get(q) if searched else None
    results = [m] if m else []

    return render_template(
        "search.html", c=cfg(), q=q, searched=searched, results=results,
        notice=notice(),
    )


# ------------------------------------------------------------------- member panes

@bp.route("/members/<mid>", methods=["GET"])
def member_shell(mid: str):
    if (g := guard()):
        return g
    m = MEMBERS.get(mid)
    if not m:
        return render_template("search.html", c=cfg(), q=mid, searched=True,
                               results=[], notice=False), 404
    return render_template("member_shell.html", c=cfg(), m=m)


@bp.route("/members/<mid>/detail", methods=["GET"])
def member_detail(mid: str):
    if (g := guard()):
        return g
    m = MEMBERS.get(mid)
    if not m:
        return render_template("frame_error.html", c=cfg(),
                               message="No records match that number."), 404
    return render_template("detail.html", c=cfg(), m=m, tab="profile",
                           notice=notice())


@bp.route("/members/<mid>/accounts", methods=["GET"])
def member_accounts(mid: str):
    if (g := guard()):
        return g
    m = MEMBERS.get(mid)
    if not m:
        return render_template("frame_error.html", c=cfg(),
                               message="No records match that number."), 404
    if m.restricted:
        # A legitimate business outcome, not a crash.
        return render_template("frame_error.html", c=cfg(),
                               message="Your role cannot service restricted accounts."), 403
    return render_template("accounts.html", c=cfg(), m=m, tab="accounts",
                           notice=notice())


# -------------------------------------------------------------------- stop payment

@bp.route("/stoppay/new", methods=["GET"])
def stoppay_new():
    if (g := guard()):
        return g
    m = MEMBERS.get(request.args.get("m", ""))
    acct = m.account(request.args.get("a", "")) if m else None
    if not m or not acct:
        return render_template("frame_error.html", c=cfg(),
                               message="No records match that number."), 404
    if peek("security_challenge"):
        return render_template("challenge.html", c=cfg(), m=m, acct=acct, error=None)
    return render_template("stoppay_form.html", c=cfg(), m=m, acct=acct, error=None,
                           relabel=peek("relabel_submit"), notice=notice())


@bp.route("/stoppay/challenge", methods=["POST"])
def stoppay_challenge():
    if (g := guard()):
        return g
    if (request.form.get("f9") or "").strip().lower() == "fairfield":
        FAULTS.pop("security_challenge", None)
    m = MEMBERS.get(request.form.get("m", ""))
    acct = m.account(request.form.get("a", "")) if m else None
    if not m or not acct:
        return render_template("frame_error.html", c=cfg(), message="Session context lost."), 400
    if peek("security_challenge"):
        return render_template("challenge.html", c=cfg(), m=m, acct=acct,
                               error="Answer not recognized.")
    return render_template("stoppay_form.html", c=cfg(), m=m, acct=acct, error=None,
                           relabel=peek("relabel_submit"), notice=notice())


@bp.route("/stoppay", methods=["POST"])
def stoppay_submit():
    if (g := guard()):
        return g
    m = MEMBERS.get(request.form.get("m", ""))
    acct = m.account(request.form.get("a", "")) if m else None
    if not m or not acct:
        return render_template("frame_error.html", c=cfg(), message="Session context lost."), 400

    number = (request.form.get("f5") or "").strip()
    reason = request.form.get("f6") or ""
    chk = next((c for c in acct.checks if c.number == number), None)

    def invalid(msg: str):
        return render_template("stoppay_form.html", c=cfg(), m=m, acct=acct, error=msg,
                               relabel=peek("relabel_submit"), notice=False)

    if not number:
        return invalid("Check number is required.")
    if chk is None:
        return invalid(f"Check {number} was not found on this account.")
    if chk.status == "cleared":
        return invalid(f"Check {number} has already cleared.")

    chk.status = "stopped"
    conf = f"SP-{random.randint(10_000_000, 99_999_999)}"

    if take("wrong_confirmation"):
        return render_template("frame_error.html", c=cfg(),
                               message="Request queued for overnight processing.")

    placed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return render_template("stoppay_confirm.html", c=cfg(), m=m, acct=acct,
                           chk=chk, conf=conf, fee=32.00, reason=reason,
                           placed_at=placed_at)


# ----------------------------------------------------------------- fault controller

@bp.route("/_control/fault", methods=["POST"])
def control_fault():
    if os.environ.get("FAULTS", "on") != "on":
        return ("", 404)
    kind = (request.json or {}).get("kind")
    if kind not in FAULT_KINDS:
        return ({"error": f"unknown fault {kind!r}", "known": list(FAULT_KINDS)}, 400)
    FAULTS[kind] = True
    return ({"armed": kind, "faults": FAULTS}, 200)


@bp.route("/_control/reset", methods=["POST"])
def control_reset():
    if os.environ.get("FAULTS", "on") != "on":
        return ("", 404)
    FAULTS.clear()
    for m in MEMBERS.values():
        for a in m.accounts:
            for c in a.checks:
                if c.status == "stopped":
                    c.status = "open"
    return ({"faults": FAULTS}, 200)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("APP_SECRET", "local-dev-only")
    app.register_blueprint(bp, url_prefix=cfg()["prefix"] or None)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Meridian CU Back Office (target app)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT", "meridian"),
                    choices=sorted(TENANTS))
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    os.environ["TENANT"] = args.tenant
    create_app().run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
