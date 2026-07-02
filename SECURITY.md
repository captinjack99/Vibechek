# Security Policy

Vibechek reads and writes audio files in directories you point it at, ships a Python sidecar bundled inside a Tauri shell, and (on Windows) automates the install of WSL Ubuntu and CUDA libraries. That's enough surface area that we take security reports seriously.

If you think you've found a vulnerability, please do **not** open a public GitHub issue. Disclose it privately so we can ship a fix before the details are public.

---

## How to report

Use **[GitHub private vulnerability reporting](https://github.com/captinjack99/Vibechek/security/advisories/new)** ("Report a vulnerability" on the repo's Security tab). Include:

- A description of the issue (what it lets an attacker do, not just what it is)
- The version you reproduced on (`vibechek --version` plus the installer / source you used)
- Step-by-step reproduction — the smaller the better
- Any proof-of-concept code or sample files
- Your name / handle for the credit line, or "anonymous" if you prefer

If you can't use GitHub, open a normal issue saying only "security report — need a private channel" (no details) and we'll arrange one.

We'll acknowledge receipt within **7 days**. If you haven't heard back by day 8, please poke us.

---

## What happens next

| Day  | What we do |
|------|------------|
| 0    | You report. |
| ≤ 7  | We acknowledge and confirm we can reproduce (or ask follow-up questions). |
| ≤ 30 | We have a fix in a private branch and share it with you for verification. |
| ≤ 60 | We ship the fix in a tagged release and publish an advisory crediting you. |

The 60-day target is the upper bound for issues that need cross-platform testing or a CVE coordination. Most fixes ship in the next beta within days.

If a fix would take longer than 60 days, we'll tell you and agree on a coordinated disclosure date together.

---

## Supported versions

Security fixes are backported to the latest minor release line only. Everything older should upgrade.

| Version          | Status                | Notes |
|------------------|-----------------------|-------|
| `0.6.x`          | Supported (current)   | Active development line. |
| `0.5.x`          | Supported for 90 days | Critical-severity fixes only. |
| `0.4.x` or older | Not supported         | Please upgrade. |

"Supported" means we will ship a tagged release with the fix on the supported branch. Unsupported versions may still receive a fix on `main`, but we make no commitments.

---

## Scope

In scope:

- The Vibechek Python sidecar (`vibechek/`)
- The Tauri shell (`ui/src-tauri/`)
- The React UI (`ui/src/`)
- The CLI (`vibechek` entry point)
- The release pipeline (`.github/workflows/release.yml`)
- The WSL bootstrap and CUDA install paths (`vibechek/wsl.py`, `vibechek/native_install.py`)

Out of scope (please report upstream):

- Bugs in Essentia, TensorFlow, Mutagen, Chromaprint, or other dependencies — report to the dependency directly. We'll bump the version once they ship a fix.
- Issues that require an attacker to already have local code execution on the user's machine.
- Social-engineering scenarios that aren't grounded in a specific Vibechek behavior.
- Findings against a modified fork that aren't reproducible on upstream `main`.

---

## A note on AGPL and modified copies

Vibechek is licensed AGPL-3.0-or-later. You are free to fork and modify the sidecar, and to redistribute or run a modified copy as a hosted service — but the AGPL obligation to ship source applies, and so does the responsibility to audit your changes.

If you ship a modified Vibechek sidecar to others (including as a hosted analyzer), **please run your own security review on the changes you made**. The upstream advisories apply only to the upstream code. We will happily call out reports against upstream, but we cannot audit your fork.

If a vulnerability turns out to exist only in a modification a downstream maintainer made, we'll point reporters at that maintainer rather than absorbing the report ourselves.
