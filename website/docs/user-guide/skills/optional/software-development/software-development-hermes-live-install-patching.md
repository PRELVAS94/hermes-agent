---
title: "Hermes Live Install Patching — Patch the running Hermes build, then verify or undo it"
sidebar_label: "Hermes Live Install Patching"
description: "Patch the running Hermes build, then verify or undo it"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Hermes Live Install Patching

Patch the running Hermes build, then verify or undo it.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/software-development/hermes-live-install-patching` |
| Path | `optional-skills/software-development/hermes-live-install-patching` |
| Version | `1.0.0` |
| Author | Pedro Relvas (PRELVAS94), Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `hermes`, `install`, `patching`, `rollback`, `development` |
| Related skills | [`hermes-agent`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent.md), [`systematic-debugging`](../../bundled/software-development/software-development-systematic-debugging.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Hermes Live Install Patching Skill

Change the Hermes source that a *running* install actually imports, and keep a way back. The helper
resolves the import root the interpreter uses, records a file before you edit it, checks that the edit
landed where the runtime reads it, and restores the original bytes when you are done. It does not
diagnose the bug (that is `systematic-debugging`), publish anything upstream, or patch a packaged
install that ships no source.

## When to Use

- The installed Hermes misbehaves and the fix has to go into its source to be tested.
- An edit was applied and the behaviour did not change — the classic *half-applied patch*: the change
  landed in a copy of the module the runtime never imports.
- A local edit must be undone before an update, a bug report, or a clean test run.
- Keywords: hotfix the install, patch the running build, editable install, shadowed module,
  `sys.path` precedence, roll back a local change.

## Prerequisites

- A Hermes install whose running code is a directory you can write to — a git checkout, or a source
  install the launcher puts on `sys.path`.
- The python that runs Hermes, for the helper: `hermes` is a launcher around an environment, so use
  the environment's interpreter (`…/venv/bin/python` on POSIX, `…\Scripts\python.exe` on Windows)
  rather than a bare system `python`. A different interpreter resolves a different install.
- `git` on the PATH for git-backed restore. Without it the helper still restores, from the bytes it
  embedded in the record.
- The `terminal` tool to run the helper, `read_file` and `patch` to inspect and edit.

## How to Run

Run every command with the `terminal` tool, from the install you intend to change. `<skill>` is the
skill's own directory — `skills/software-development/hermes-live-install-patching` once installed.

1. **Resolve what is running.** Never skip this: the file you edit must be the file the interpreter
   imports.

   ```bash
   python "<skill>/scripts/install_patch.py" resolve
   ```

   It prints `interpreter`, `python`, `module`, `file`, `root`, `git:` and any `other_copies` of the
   same module found on the search path. Done when `file:` is a path you can read and it is the file
   you were about to edit.

2. **Record the file before touching it.**

   ```bash
   python "<skill>/scripts/install_patch.py" record <path/to/module.py>
   ```

   Writes `install-patch-record.json` in the current directory: absolute path, sha256, size, git HEAD,
   whether git tracks the file, and the original bytes. It is a local artifact — do not commit it.

3. **Edit with `patch`**, in the tree `resolve` named. Edit one file, once. Do not "keep the other copy
   in sync": two copies of one module is the defect this skill exists to avoid.

4. **Verify the edit landed where the runtime reads it.**

   ```bash
   python "<skill>/scripts/install_patch.py" verify install-patch-record.json
   ```

   `changed: true` is expected here; `parses: false` means the edit is broken (exit 3); a `module_file:`
   that differs from `file:` means the module now resolves somewhere else, so the edit is in the wrong
   tree (exit 2 with the record's module named).

5. **Prove the behaviour, from a fresh process.** Restart the process that must pick the change up
   (gateway, scheduler, desktop backend, a new `hermes` invocation) and run the command that failed.
   Keep the before/after output; the file's mtime is not evidence.

6. **Undo when you are done.**

   ```bash
   python "<skill>/scripts/install_patch.py" restore install-patch-record.json
   ```

   Prefers `git checkout <recorded HEAD> -- <file>` when the file was tracked, otherwise writes the
   recorded bytes back. Use `--dry-run` first to see what it would do, and `--force` only when the
   module has moved and you know why.

## Quick Reference

| Command | Purpose |
| --- | --- |
| `install_patch.py resolve [MODULE]` | the file the interpreter imports, its import root, other copies on the path |
| `install_patch.py record FILE` | snapshot before editing: sha256, size, git HEAD, original bytes |
| `install_patch.py verify RECORD` | did the file change, does it still parse, does the module still resolve to it |
| `install_patch.py restore RECORD` | put the original back (git first, recorded bytes as the fallback) |

Exit codes: `0` nothing to report · `1` the file changed since the record · `2` refusal or a missing
file / record · `3` the file no longer parses.

`record` options: `--out PATH` (default `install-patch-record.json`), `--module NAME` (the module to
re-resolve during `verify`). `restore` options: `--dry-run`, `--force`.

## Procedure

### 1. Find the import root, not just the file

Hermes resolves a module through the interpreter's search path, and an install can hold the same
module twice:

- a **source checkout** (a git clone the launcher puts at `sys.path[0]`, or an editable install's
  target) — editing here changes behaviour;
- an environment's **frozen copy** of the package — editing here changes nothing the running process
  can see, because the checkout wins the lookup.

The rule of thumb: `sys.path[0]` beats a finder appended to `sys.meta_path`. `resolve` reports both:
`file:` is what the interpreter imports now, `other_copies` is everything else a lookup could have
found. Done when you can say which path is live and which one you are not touching.

### 2. Patch the live path, as a small, recorded edit

Record first (step 2 above), then edit with `patch`, then run the file's own tests before you trust it:
Hermes ships a test runner (`scripts/run_tests.sh tests/<area>/<test_file>.py -q` in a source checkout)
and each area has tests that mirror its source tree. Point `HERMES_HOME` at a throwaway directory for
that run — a test suite that writes into a real data root is worse than no suite.

### 3. Prove it, then keep the change portable

- Before: capture the failing command's output. After: restart and capture it again.
- Keep the change as a patch so it survives a re-clone or an update:

  ```bash
  git -C <root> diff -- <file> > fix.patch
  git -C <root> apply --reverse --check fix.patch
  ```

  `git -C <root> apply --reverse --check fix.patch` proves the patch describes the tree you are
  standing in (plain `git -C <root> apply --check` fails there, because the change is already
  applied); on a fresh clone, `git -C <root> apply fix.patch` is what re-applies it.

- A running process that already imported the module keeps the old code in memory; the edit only
  takes effect for processes started afterwards. Restarting is part of the fix, not an extra step.

### 4. Undo it the same way

`restore` puts the recorded file back and re-hashes it, so the rollback is verified rather than
assumed. Confirm the tree is clean for that file afterwards (`git -C <root> status --porcelain --
<file>`), and delete `install-patch-record.json` once you no longer need it.

## Pitfalls

- **The half-applied patch.** Applied "twice" and still broken usually means one of two copies was
  edited, or the file was edited but the process was never restarted. `resolve` and `verify` answer
  both.
- **A green edit is not a verified fix.** `read_file` showing your change proves the file changed, not
  that the runtime uses it. The evidence is the failing command, re-run in a fresh process.
- **Hand-editing a second copy.** A facade plus its siblings, or a duplicated module, is exactly how a
  patch gets half-applied. One file, one edit, verified by `resolve`.
- **Updating over a local edit.** `hermes update` and a re-clone will overwrite or discard it. Keep
  `fix.patch` and the record, and re-apply deliberately.
- **No git, no history.** An edit inside an installed environment has no reflog; the record's embedded
  bytes are then the only way back. Take the record *before* the edit, always.
- **Wrong interpreter.** Running the helper, or the tests, with a different python than the one that
  runs Hermes resolves a different install and reports it as healthy.
- **Deleting evidence too early.** Keep the record until the rollback is confirmed; `restore` refuses
  to act on a record it cannot line up with the live module.

## Verification

- `resolve` names a `file:` that exists and a `root:` with git history, and `other_copies` explains
  every duplicate found (`[]` when there is none).
- `verify` before the edit reports `changed: false`; after the edit `changed: true`, `parses: true`,
  and `module_file:` equal to `file:`.
- The failing command's output differs after a restart, with both outputs kept.
- `restore` reports the hash it wrote back, and a following `verify` reports `changed: false`.
