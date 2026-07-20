"""Shared user-facing error shape for the JSON-RPC seam.

The desktop shell renders every failed RPC through ONE error surface (an
ErrorToast). For that surface to show a plain-English headline, a "what happens
next" action, and a collapsible technical detail — instead of a raw exception
string — the error needs a small structured envelope the dispatcher can put in
`error.data`.

`UserFacingError` is that envelope. Raise it anywhere a handler wants to control
what the user reads:

    raise UserFacingError(
        "Analysis stopped unexpectedly while processing your library.",
        detail=f"vibechek exited with {rc}.\n{stderr_tail}",
        kind="retryable",
        options={"can_open_doctor": True},
    )

`rpc._dispatch` serializes it into::

    {"code": -32000,
     "error": {"message": <headline>,
               "data": {"kind": ..., "headline": ..., "detail": ..., <options>}}}

The consumer degrades gracefully if any field is absent (partial adoption is
safe), so a site can start by only setting a headline and add `detail`/`kind`
later. `str(exc)` is the headline, so legacy code that does `str(e)` still gets
the plain string rather than a repr.

Voice rules (see the UX-audit voice guide + zero-setup doctrine):
  * headline  — plain DJ-app terms + the next step; never an exit code, byte
                count, `.so` name, distro, or stack fragment.
  * detail    — the technical identifiers, DEMOTED not deleted (bug reports and
                Doctor still need them).
  * kind      — lets the shell pick the right recovery affordance.
"""

from __future__ import annotations

from typing import Any, Literal

# The three recovery classes the shell keys its action button off of:
#   "retryable"   — re-issuing the SAME call can genuinely succeed (a transient
#                   crash, a corrupt-but-regenerable output file). → "Try again".
#   "engine_dead" — the analysis service/environment needs a restart to recover
#                   (a corrupted launch shim, a dead worker pool). → "Restart".
#   "fatal"       — a blind retry/restart won't help; the headline itself states
#                   the real next step (switch a setting, free memory, reinstall).
ErrorKind = Literal["retryable", "engine_dead", "fatal"]


class UserFacingError(Exception):
    """An error with a plain headline, a demoted technical detail, and a kind.

    Subclasses `Exception` (NOT `ValueError`/`RuntimeError`) on purpose: the many
    `except RuntimeError:` / `except ValueError:` blocks scattered through the
    analyze path must not accidentally swallow — or worse, re-wrap and strip the
    structure from — a deliberately-shaped user-facing error. It propagates clean
    to the dispatch seam.
    """

    def __init__(
        self,
        headline: str,
        *,
        detail: str | None = None,
        kind: ErrorKind = "fatal",
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(headline)
        self.headline = headline
        self.detail = detail
        self.kind: ErrorKind = kind
        # Extra machine-readable flags the shell keys buttons off of, e.g.
        # {"can_install_wsl": True} or {"can_switch_classifier": True}. Kept
        # separate from kind so a site can add affordances without inventing new
        # kinds.
        self.options: dict[str, Any] = dict(options or {})

    def to_error_data(self) -> dict[str, Any]:
        """Build the `error.data` payload. `headline` is always present; `detail`
        only when set; option flags are merged in at the top level so a consumer
        can read `data.can_install_wsl` directly."""
        data: dict[str, Any] = {"kind": self.kind, "headline": self.headline}
        if self.detail:
            data["detail"] = self.detail
        # Options never shadow the reserved keys above.
        for k, v in self.options.items():
            if k not in data:
                data[k] = v
        return data


__all__ = ["UserFacingError", "ErrorKind"]
