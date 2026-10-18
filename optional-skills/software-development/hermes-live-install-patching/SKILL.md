---
name: hermes-live-install-patching
description: "Patch the running Hermes build, then verify or undo it."
version: 1.0.0
author: Pedro Relvas (PRELVAS94), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, install, patching, rollback, development]
    category: software-development
    related_skills: [hermes-agent, systematic-debugging]
---

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

By hand, when the helper cannot run (wrong interpreter, a broken import), the same three questions:

| Question | Command | Read it as |
| --- | --- | --- |
| Which file does this interpreter import? | `python -c "import hermes_constants; print(hermes_constants.__file__)"` | it must be the file you are about to edit — run it with the interpreter that runs Hermes, not a system one |
| Is that root a git checkout? | `git -C <root> rev-parse --is-inside-work-tree` | `true` — a checkout, so there is a HEAD to roll back to; `not a git repository` — a packaged copy, and the record's embedded bytes are the only way back |
| Is a second copy on the search path? | `python -c "import sys; print(*sys.path, sep='\n')"` | every entry is a directory a copy can hide in; `resolve` names the files found, this names the places |

### 2. Patch the live path, as a small, recorded edit

**Never edit the installed tree in place.** An edit inside the installed environment is invisible to
git, unreviewable, and silently discarded by the next update. Work in a git checkout: clone the
repository, branch, patch there, and make the install import that checkout (an editable install, or
the launcher putting the checkout on `sys.path`). When `resolve` names a real checkout, that checkout
*is* the place to edit. When it names a frozen copy inside an environment, edit the copy's source
checkout instead and do not touch the frozen one — two edited copies is the failure this skill exists
to prevent, and the record is the fallback only for the case where no checkout exists.

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

## Walkthrough

End to end, from nothing: build a throwaway install, patch it, prove the patch, undo it. Every path is
generic — `DEMO` is a fresh temp directory and `SKILL` resolves the skill's own directory from
`HERMES_HOME`, so the block runs as written once the skill is installed. Run it in the `terminal`
tool as one block. It is POSIX shell; on Windows, run the same commands in PowerShell with
`$env:PYTHONPATH` set instead of the `PYTHONPATH=` prefix.

```bash
# A throwaway install: a git checkout holding one importable package.
DEMO="$(mktemp -d)/demo-install"
SKILL="${HERMES_HOME:-$HOME/.hermes}/skills/software-development/hermes-live-install-patching"
mkdir -p "$DEMO/app_pkg"
git -C "$DEMO" init -q
printf 'VERSION = "before"\n' > "$DEMO/app_pkg/__init__.py"
git -C "$DEMO" add -A
git -C "$DEMO" -c user.email=demo@example.com -c user.name=demo commit -qm "demo install"

# 1. Which copy does this interpreter import? The checkout is on its path, as a launcher puts it.
PYTHONPATH="$DEMO" python "$SKILL/scripts/install_patch.py" resolve app_pkg

# 2. Record the file before touching it.
cd "$DEMO"
PYTHONPATH="$DEMO" python "$SKILL/scripts/install_patch.py" record app_pkg/__init__.py --module app_pkg

# 3. Edit the file the resolve step named: one file, one edit.
printf 'VERSION = "after"\n' > app_pkg/__init__.py

# 4. Did the edit land in the copy the runtime reads? Exit 1 means "changed", not "failed".
PYTHONPATH="$DEMO" python "$SKILL/scripts/install_patch.py" verify install-patch-record.json
echo "verify exit $?"

# 5. Prove the behaviour from a fresh process, then keep the change as a re-appliable patch.
PYTHONPATH="$DEMO" python -c "import app_pkg; print(app_pkg.VERSION)"
git diff -- app_pkg/__init__.py > fix.patch
git apply --reverse --check fix.patch
echo "reverse-check exit $?"

# 6. Undo, and confirm the file and the tree are back at the baseline.
PYTHONPATH="$DEMO" python "$SKILL/scripts/install_patch.py" restore install-patch-record.json
PYTHONPATH="$DEMO" python "$SKILL/scripts/install_patch.py" verify install-patch-record.json
PYTHONPATH="$DEMO" python -c "import app_pkg; print(app_pkg.VERSION)"
git status --porcelain -- app_pkg/__init__.py
echo "done — worktree clean for app_pkg/__init__.py"
```

What a correct run shows, in order: `resolve` names a `file:` under `$DEMO` and `other_copies: []`;
`record` prints `tracked: True`; `verify` prints `changed: True`, `parses: True`, `same_file: True`,
then `verify exit 1`; the fresh process prints `after`; `reverse-check exit 0`; `restore` prints
`method: git`, `restored: True`, exit 0; the next `verify` prints `changed: False`, exit 0; the probe
prints `before` again; and the scoped `git status` prints nothing before the final line. If `verify`
instead prints `same_file: False`, the edit went into a copy the runtime does not import — the
`wrong_tree` line names the copy it resolves to now.

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
