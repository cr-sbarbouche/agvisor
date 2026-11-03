# Security policy

agvisor is alpha software and has not been security-audited. The README's "Security" section lists the known gaps.

## Supported versions

Only the latest release gets fixes (0.x: no backports).

## Reporting a vulnerability

Please don't open a public issue for a vulnerability: it would tell everyone about the flaw before there is a fix. Email **sadokbarbouche1@outlook.com** with the version, steps to reproduce, and the impact (for example a sandbox escape, a bypassed limit, a forged or replayed gateway token, or an injection through `image`, `name`, `network` or `runtime`).

For ordinary bugs, questions and feature requests, open a GitHub issue.

## What to expect

An acknowledgement within 5 business days. This is a one-person project: I'll aim to ship a fix or a mitigation within 90 days, and credit you in the release notes if you want.

## Scope

In scope: `src/agvisor/`, `gateway/` and `web/`.

Out of scope:
- Bugs in gVisor, Docker, guacd or GNOME: report those upstream.
- Gaps the README's Security section already documents (no `/workspace` size cap, no `name=` ownership check, sandboxes on one network reaching each other).
