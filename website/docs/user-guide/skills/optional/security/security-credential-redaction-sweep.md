---
title: "Credential Redaction Sweep — Redact leaked credentials from agent memory and logs"
sidebar_label: "Credential Redaction Sweep"
description: "Redact leaked credentials from agent memory and logs"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Credential Redaction Sweep

Redact leaked credentials from agent memory and logs.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/security/credential-redaction-sweep` |
| Path | `optional-skills/security/credential-redaction-sweep` |
| Version | `1.0.0` |
| Author | Pedro Relvas (PRELVAS94), Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `secrets`, `security`, `redaction`, `memory-hygiene`, `log-hygiene` |
| Related skills | [`1password`](../../optional/security/security-1password.md), [`oss-forensics`](../../optional/security/security-oss-forensics.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Credential Redaction Sweep Skill

Find credential-shaped strings across an agent's own memory and log surface — daily digests, memory
files, session exports, logs, snapshot archives — and redact them in place, with the write path
fail-closed: the writer re-scans its own rendered text and refuses to write a file that is still
credential-shaped. It is a deterministic stdlib helper plus the procedure that stops a capture path
from persisting a secret again. It is not a credential-store manager: it never reads a live secret
out of a store, never rotates or revokes anything, and it does not rewrite git history.

## When to Use

- A real credential was found (or is suspected) in memory files, digests, logs, a snapshot or a backup.
- A capture path — a session-to-memory writer, a digest job, a backup push — must be stopped from
  ever persisting a credential again.
- A copied estate, a restored snapshot or a second checkout of the same data needs a leak sweep.

Don't use for: rotating or revoking a key (an account action, not a file edit), editing a credential
store, or "cleaning" a tree by deleting the files a sweep touched.

## Prerequisites

- Python 3.9+ with the stdlib only (`re`, `hashlib`, `zipfile`, `tempfile`); no network, no packages.
- Write access to the files being swept, plus somewhere to keep the integrity record.
- The helper at `scripts/redact_sweep.py` inside this skill's directory — `hermes skills` shows where
  the skill is installed; run the script from there.
- Decide the scope before you scan: a session store (the transcript of record) is out of scope, and a
  credential store (`.env`, an auth file, a provider config) is the owner's decision, not a sweep's.

## How to Run

```python
terminal(command="python scripts/redact_sweep.py scan memory/ notes.md", timeout=120)
terminal(command="python scripts/redact_sweep.py scan memory/ --json", timeout=120)
terminal(command="python scripts/redact_sweep.py redact memory/ --record redaction-record.json", timeout=120)
terminal(command="python scripts/redact_sweep.py redact memory/ --dry-run", timeout=120)
```

`scan` reports and changes nothing: exit `1` when it finds something, `0` when the surface is clean.
`redact` rewrites each file atomically and exits `3` without writing anything when its own re-scan of
the rendered text is not clean. Use `read_file` to inspect a suspect file, `search_files` to sweep a
surface for a shape, and `patch` only for edits a redaction record does not need to describe.

## Quick Reference

| Command | What it does |
|---|---|
| `redact_sweep.py scan PATH...` | report every credential-shaped span; exit 1 when any is found |
| `redact_sweep.py scan PATH... --json` | the same report as JSON (findings, skips, archives) |
| `redact_sweep.py redact PATH...` | replace each span with `[REDACTED:<kind>]`, in place |
| `redact_sweep.py redact PATH... --dry-run` | render and verify, write nothing |
| `redact_sweep.py redact PATH... --record FILE` | also write the integrity record for every file |

## Procedure

1. **Find the capture path before touching data.** Sweep the estate for the directory the leak landed
   in (`search_files`) rather than assuming the writer. The class of bug is always the same: a writer
   serialises session content straight to disk with no filter. *Done when* you can name the writer and
   the file it produces.
2. **Install two gates, not one.** Redact at ingestion — the content is usually also printed to stdout
   and captured by the job layer, so a write-time gate alone still leaks into the job's own log — and
   assert-clean immediately before the write: fail closed, with a non-zero exit and nothing written,
   so a shape nobody taught the redactor stops the job instead of persisting the secret. The writer
   must also refuse to run when the redactor module is missing: a silent downgrade is worse than a
   failed job. *Done when* both gates exist and the missing-redactor case exits non-zero.
3. **Keep the two pattern sets apart.** Reporting uses anchored patterns only, so a URL slug or a
   filename that happens to contain `sk-` does not fill the report with prose; redaction uses the same
   set plus unanchored variants of the prefix-anchored kinds, because a 20+ character run after those
   prefixes is never English. Assignment forms (`api_key = …`, `"refresh_token": "…"`) are the fuzzy
   layer: accept them only when the value is 16+ characters, has letters **and** digits, and is not a
   dotted attribute or a snake_case identifier — a bare hex or base64 blob parses as an identifier too,
   and is still a credential, so it is redacted. *Done when* `scan` reports the planted fixture and not
   the slug in the same file.
4. **Sweep every form a copy can take.** The live tree; every tree that mirrors it (restored
   snapshots, another agent's workspace); logs and job output; archive members, which a tree sweep
   never sees (`scan` reads zip members and labels them `archive:member`). Binary stores — a sqlite
   session database, a vector index — are skipped by a text sweep: check them at byte level and say in
   the report that it was a byte-level check, not a semantic one. A surface scope is the leak verdict;
   a full-tree scope is an inventory that also flags legitimate stores. Do not conflate them.
5. **Scrub in place, never delete.** Replace only the matched characters with `[REDACTED:<kind>]`;
   everything else in the file stays byte-identical, line count included. Rewrite atomically (temp
   file in the same directory plus `os.replace`). *Done when* `redact --dry-run` prints `checks` with
   all four true: line count unchanged, byte delta equal to the sum of the reductions, a placeholder
   for every recorded span, nothing credential-shaped left.
6. **Keep the integrity record.** Per file, pre- and post-write digests plus the spans that changed
   (kind, offsets, length, truncated sha256). That is what lets a later reader tell a deliberate
   redaction from corruption. Never write the credential itself into the record — a truncated hash is
   the correlation id: the same credential in two files is one revocation.
7. **Close the loop on the capture path.** The gate is the deliverable; the scrub is incident
   response. Re-run `scan` over the same surface afterwards and confirm it exits `0`.

## Pitfalls

- Do not scrub a **credential store**: that is an owner decision. Report it with kinds and
  fingerprints and let the owner choose.
- A `.gitignore` that names a secret file usually does **not** cover its variants (`auth.json.bak-*`)
  — check, because that is how a store ends up in a nightly backup push.
- Redacting a working tree does not rewrite **history**: old blobs stay reachable from past commits in
  a backup repository. Say so in the report; revocation is the mitigation.
- Fixture hygiene: generate synthetic credentials at runtime from a hash instead of writing a literal
  into a test file, and assemble PEM markers from fragments — otherwise the test suite becomes a leak
  the sweep reports.
- Redacting a **session store** would rewrite the transcript of record: keep it out of scope and state
  the boundary in the report.
- A file that is skipped (larger than the scan cap, not UTF-8 text, or an archive in `redact` mode) is
  printed, never silently counted as clean.
- A second pass over an already-redacted file reports `unchanged`. That is the expected result, not a
  failure.

## Verification

- `terminal(command="python scripts/redact_sweep.py scan <surface>")` exits `0` and prints
  `0 finding(s)`.
- Every rewritten file carries `checks` with all four entries true in the record written by
  `--record`, and its line count matches the pre-sweep copy.
- A placeholder exists for each recorded span: `search_files(pattern="\\[REDACTED:", path="<surface>")`
  returns the count the record lists, and no span's digest appears in a file you did not rewrite.
- The write path really is fail-closed: plant a synthetic token in a scratch copy, make the redactor
  blind to one shape, and confirm the writer refuses (exit `3`) with the file byte-identical
  afterwards.
