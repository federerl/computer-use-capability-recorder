"""Recording what the person did while they held the session.

Without this, a handoff is a hole in the audit trail: the run log would show
automation stopping, then resuming against a page that had changed, with nothing
saying how. In a regulated setting that gap is the whole problem.

Values pass through the same redactor as everything else, and anything typed
into a password field is dropped rather than redacted - there is no version of
that value worth keeping.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BINDING = "__recordHumanAction"

# Installed as an init script so it survives every navigation the person makes,
# and applies to child frames as well as the top document.
_SCRIPT = """
() => {
  if (window.__handoffRecorderInstalled) return;
  window.__handoffRecorderInstalled = true;

  const label = el => {
    if (!el || !el.getAttribute) return '';
    const direct = el.getAttribute('aria-label') || el.getAttribute('title')
                || el.getAttribute('placeholder') || el.value
                || (el.textContent || '').trim();
    if (direct && String(direct).length < 80) return String(direct);
    const cell = el.closest && el.closest('td');
    const prev = cell && cell.previousElementSibling;
    return prev ? (prev.textContent || '').trim().slice(0, 80) : '';
  };

  const send = (kind, el, extra) => {
    try {
      window.__recordHumanAction(Object.assign({
        kind,
        tag: el && el.tagName ? el.tagName.toLowerCase() : '',
        type: el && el.getAttribute ? (el.getAttribute('type') || '') : '',
        name: el && el.getAttribute ? (el.getAttribute('name') || '') : '',
        label: label(el),
        url: location.href,
      }, extra || {}));
    } catch (e) { /* the page must not break because recording failed */ }
  };

  document.addEventListener('click', e => send('click', e.target), true);
  document.addEventListener('change', e => send('change', e.target,
      { value: e.target && e.target.type === 'password' ? null : (e.target || {}).value }), true);
  document.addEventListener('submit', e => send('submit', e.target), true);
}
"""


@dataclass
class HumanAction:
    at: str
    kind: str
    tag: str = ""
    type: str = ""
    name: str = ""
    label: str = ""
    value: Any = None
    url: str = ""


@dataclass
class HumanActionRecorder:
    page: Any
    redactor: Any
    actions: list[HumanAction] = field(default_factory=list)
    _installed: bool = False

    def start(self) -> None:
        """Install the listeners, everywhere, and more than once if asked.

        Two things make this fiddlier than it looks. The binding belongs to the
        page and may only be registered once, while a run can hand over several
        times. And an init script only affects documents loaded after it, so the
        document already on screen - the one the person is about to work in -
        needs the listeners applied directly.

        That last part has to reach every frame. In a framed application the
        controls a person touches are in a child document, so installing only in
        the top frame records nothing and the handover looks unobserved.
        """
        if not self._installed:
            try:
                self.page.expose_binding(
                    BINDING, lambda _source, payload: self._capture(payload))
            except Exception as exc:
                if "already registered" not in str(exc):
                    raise
            self.page.add_init_script(_SCRIPT)
            self._installed = True

        for frame in list(self.page.frames):
            try:
                frame.evaluate(_SCRIPT)
            except Exception:
                continue  # detached mid-navigation

    def _capture(self, payload: dict) -> None:
        value = payload.get("value")
        if payload.get("type") == "password":
            value = "<<omitted:password>>"
        elif isinstance(value, str):
            value = self.redactor.text(value)

        self.actions.append(HumanAction(
            at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            kind=str(payload.get("kind", "")),
            tag=str(payload.get("tag", "")),
            type=str(payload.get("type", "")),
            name=str(payload.get("name", "")),
            label=self.redactor.text(str(payload.get("label", ""))),
            value=value,
            url=str(payload.get("url", "")),
        ))

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for action in self.actions:
                fh.write(json.dumps(asdict(action)) + "\n")
        return p

    def summary(self) -> list[str]:
        out = []
        for a in self.actions:
            what = a.label or a.name or a.tag
            out.append(f"{a.kind} {what!r}"
                       + (f" = {a.value!r}" if a.value not in (None, "") else ""))
        return out
