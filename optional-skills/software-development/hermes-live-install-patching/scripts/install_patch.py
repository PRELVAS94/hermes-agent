#!/usr/bin/env python3
"""Locate and safely patch the Hermes source a running interpreter actually imports.

resolve -- the file the interpreter imports, its import root, other copies on the path
record  -- snapshot a file (hash, git HEAD, original bytes) before you edit it
verify  -- did the file change, does it still parse, does the module still resolve to it
restore -- put the original back: git checkout when tracked, recorded bytes otherwise

Exit codes: 0 nothing to report, 1 the file changed, 2 refusal or a missing file,
3 the file no longer parses.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MODULE = "hermes_constants"
DEFAULT_RECORD = "install-patch-record.json"
MAX_EMBED_BYTES = 5 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 20

EXIT_OK = 0
EXIT_CHANGED = 1
EXIT_REFUSED = 2
EXIT_UNPARSEABLE = 3

_README = f"""resolve reports what the interpreter imports; record/verify/restore protect the edit.

  {DEFAULT_MODULE} is the default probe module: it ships with every install.
"""


def sha256_file(path: Path) -> str:
    """Streaming sha256, so a large source file never has to fit in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """The single git seam: every git call goes through here, none of them raises."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_facts(root: Path) -> dict:
    """HEAD, branch and dirty-file count for a checkout; {} when git or the repo is absent."""
    head = _run_git(root, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return {}
    branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _run_git(root, "status", "--porcelain")
    dirty = [line for line in (status.stdout if status else "").splitlines() if line.strip()]
    return {
        "root": str(root),
        "head": head.stdout.strip(),
        "branch": branch.stdout.strip() if branch else "",
        "dirty_files": len(dirty),
    }


def repo_relative(root: Path, path: Path) -> str:
    """The path as git wants it: relative to the repo root when it lives inside it."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def git_tracks(root: Path, path: Path) -> bool:
    """True when the file is tracked at HEAD, i.e. `git checkout <head> -- <path>` can restore it."""
    proc = _run_git(root, "ls-files", "--error-unmatch", repo_relative(root, path))
    return bool(proc is not None and proc.returncode == 0)


def module_origin(module: str) -> dict:
    """Where a module resolves without importing it (no side effects, no user code runs)."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):  # a dotted name whose parent cannot be imported
        spec = None
    origin = Path(spec.origin) if spec and spec.origin else None
    return {
        "module": module,
        "file": origin,
        "is_package": bool(spec and spec.submodule_search_locations),
    }


def import_root(file_of_module: Path, is_package: bool) -> Path:
    """The directory an interpreter adds to its path to reach this file."""
    parent = file_of_module.parent
    return parent.parent if is_package else parent


def module_candidates(module: str, file_of_module: Path | None, entries: list[str] | None = None) -> list[Path]:
    """Every copy of `module` reachable from the search path, the running one first."""
    found: list[Path] = []
    for entry in sys.path if entries is None else entries:
        base = Path(entry) if entry else Path.cwd()
        for candidate in (base / f"{module}.py", base / module / "__init__.py"):
            if candidate.is_file():
                found.append(candidate.resolve())
    if file_of_module is not None:
        running = file_of_module.resolve()
        found = [running, *(item for item in found if item != running)]
    return found


def resolve_report(module: str, entries: list[str] | None = None) -> dict:
    """What the interpreter imports for `module`, plus every other copy it could have found."""
    origin = module_origin(module)
    file_of_module = origin["file"]
    report: dict = {
        "interpreter": sys.executable,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "module": module,
        "file": None,
        "root": None,
        "other_copies": [],
        "git": {},
    }
    if file_of_module is None:
        return report
    root = import_root(file_of_module, origin["is_package"])
    report["file"] = str(file_of_module)
    report["root"] = str(root)
    report["other_copies"] = [str(item) for item in module_candidates(module, file_of_module, entries)[1:]]
    report["git"] = git_facts(root)
    return report


def build_record(path: Path, module: str | None = None) -> dict:
    """The pre-edit snapshot: identity, git context and the bytes needed to undo the edit."""
    resolved = path.resolve()
    root = resolved.parent
    facts = git_facts(root)
    git_root = Path(facts["root"]) if facts else None
    return {
        "file": str(resolved),
        "sha256": sha256_file(resolved),
        "size": resolved.stat().st_size,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "module": module,
        "git": facts,
        "tracked": bool(git_root and git_tracks(git_root, resolved)),
        "rel_path": repo_relative(git_root, resolved) if git_root else None,
        "original_b64": base64.b64encode(resolved.read_bytes()).decode("ascii"),
    }


def load_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_record(record: dict) -> dict:
    """Re-check the recorded file: content, syntax, and whether the module still resolves to it."""
    path = Path(record["file"])
    report: dict = {
        "file": str(path),
        "recorded_sha256": record.get("sha256"),
        "exists": path.is_file(),
        "current_sha256": None,
        "changed": True,
        "parses": None,
        "module": record.get("module"),
        "module_file": None,
        "same_file": None,
    }
    if not report["exists"]:
        return report
    report["current_sha256"] = sha256_file(path)
    report["changed"] = report["current_sha256"] != record.get("sha256")
    if path.suffix != ".py":
        report["parses"] = None
        return report
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        report["parses"] = False
        report["syntax_error"] = f"unreadable as text: {exc}"
        return report
    try:
        compile(source, str(path), "exec")
        report["parses"] = True
    except SyntaxError as exc:
        report["parses"] = False
        report["syntax_error"] = f"line {exc.lineno}: {exc.msg}"
    module = record.get("module")
    if module:
        origin = module_origin(module)
        report["module_file"] = str(origin["file"]) if origin["file"] else None
        report["same_file"] = report["module_file"] == str(path)
    return report


def verify_exit_code(report: dict) -> int:
    if not report["exists"]:
        return EXIT_REFUSED
    if report["parses"] is False:
        return EXIT_UNPARSEABLE
    return EXIT_CHANGED if report["changed"] else EXIT_OK


def restore_plan(record: dict, force: bool = False) -> tuple[str, str | None]:
    """('git' | 'bytes' | 'refused', reason) — which way back is available, and why not otherwise."""
    module = record.get("module")
    if module:
        origin = module_origin(module)
        live = str(origin["file"]) if origin["file"] else None
        if live != record["file"] and not force:
            return "refused", f"module {module!r} now resolves to {live}, not {record['file']}"
    facts = record.get("git") or {}
    if record.get("tracked") and facts.get("head"):
        return "git", None
    if record.get("original_b64"):
        return "bytes", None
    return "refused", "the record holds neither a tracked git file nor the original bytes"


def restore_record(record: dict, force: bool = False, dry_run: bool = False) -> dict:
    """Put the pre-edit bytes back, then prove it by re-hashing."""
    path = Path(record["file"])
    result: dict = {
        "file": str(path),
        "method": None,
        "reason": None,
        "before_sha256": None,
        "after_sha256": None,
        "restored": False,
        "exit_code": EXIT_OK,
    }
    if not path.is_file():
        result.update(exit_code=EXIT_REFUSED, reason="the recorded file is gone; nothing to restore")
        return result
    result["before_sha256"] = sha256_file(path)
    if result["before_sha256"] == record.get("sha256"):
        result.update(reason="already at the recorded baseline", after_sha256=result["before_sha256"])
        return result
    method, reason = restore_plan(record, force=force)
    result.update(method=method, reason=reason)
    if method == "refused":
        result["exit_code"] = EXIT_REFUSED
        return result
    if dry_run:
        result["reason"] = "dry run: nothing written"
        return result
    if method == "git":
        facts = record["git"]
        target = record.get("rel_path") or str(path)
        proc = _run_git(Path(facts["root"]), "checkout", facts["head"], "--", target)
        if proc is None or proc.returncode != 0:
            stderr = proc.stderr.strip() if proc else "git unavailable"
            result.update(exit_code=EXIT_REFUSED, reason=f"git checkout failed: {stderr[:200]}")
            return result
    else:
        path.write_bytes(base64.b64decode(record["original_b64"]))
    result["after_sha256"] = sha256_file(path)
    result["restored"] = result["after_sha256"] == record.get("sha256")
    if not result["restored"]:
        result.update(exit_code=EXIT_REFUSED, reason="the file still does not match the recorded baseline")
    return result


def _print_lines(title: str, lines: list[tuple[str, object]]) -> None:
    print(title)
    for key, value in lines:
        if isinstance(value, list):
            print(f"  {key}: {value if value else '[]'}")
        else:
            print(f"  {key}: {value}")


def _cmd_resolve(args: argparse.Namespace) -> int:
    report = resolve_report(args.module)
    if report["file"] is None:
        print(f"cannot resolve module {args.module!r} with this interpreter ({sys.executable}).", file=sys.stderr)
        print("Run the helper with the python that runs Hermes.", file=sys.stderr)
        return EXIT_REFUSED
    _print_lines(
        "running code:",
        [
            ("interpreter", report["interpreter"]),
            ("python", report["python"]),
            ("module", report["module"]),
            ("file", report["file"]),
            ("root", report["root"]),
            ("git", report["git"] or "no repository at this root"),
            ("other_copies", report["other_copies"]),
        ],
    )
    return EXIT_OK


def _cmd_record(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return EXIT_REFUSED
    size = path.stat().st_size
    if size > MAX_EMBED_BYTES:
        print(f"refusing: {path} is {size} bytes, over the {MAX_EMBED_BYTES} byte embed cap.", file=sys.stderr)
        print("Commit or copy it somewhere recoverable first.", file=sys.stderr)
        return EXIT_REFUSED
    record = build_record(path, module=args.module)
    out = Path(args.out).expanduser()
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    _print_lines(
        "recorded:",
        [
            ("file", record["file"]),
            ("sha256", record["sha256"]),
            ("size", record["size"]),
            ("git", record["git"] or "no repository at this root"),
            ("tracked", record["tracked"]),
            ("record", str(out)),
            ("restore_with", f"restore {out}"),
        ],
    )
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    record_path = Path(args.record).expanduser()
    if not record_path.is_file():
        print(f"no such record: {record_path}", file=sys.stderr)
        return EXIT_REFUSED
    report = verify_record(load_record(record_path))
    lines: list[tuple[str, object]] = [
        ("file", report["file"]),
        ("exists", report["exists"]),
        ("recorded_sha256", report["recorded_sha256"]),
        ("current_sha256", report["current_sha256"]),
        ("changed", report["changed"]),
        ("parses", report["parses"]),
    ]
    if report.get("syntax_error"):
        lines.append(("syntax_error", report["syntax_error"]))
    if report.get("module"):
        lines.append(("module", report["module"]))
        lines.append(("module_file", report["module_file"]))
        lines.append(("same_file", report["same_file"]))
    code = verify_exit_code(report)
    if report["exists"] and report["module"] and report["same_file"] is False:
        lines.append(("wrong_tree", f"{report['module']} now resolves to another copy; this edit is not live"))
    _print_lines("verify:", lines)
    return code


def _cmd_restore(args: argparse.Namespace) -> int:
    record_path = Path(args.record).expanduser()
    if not record_path.is_file():
        print(f"no such record: {record_path}", file=sys.stderr)
        return EXIT_REFUSED
    result = restore_record(load_record(record_path), force=args.force, dry_run=args.dry_run)
    _print_lines(
        "restore:",
        [
            ("file", result["file"]),
            ("method", result["method"]),
            ("reason", result["reason"]),
            ("before_sha256", result["before_sha256"]),
            ("after_sha256", result["after_sha256"]),
            ("restored", result["restored"]),
        ],
    )
    return result["exit_code"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_patch.py",
        description="Locate and safely patch the Hermes source a running interpreter imports.",
        epilog=_README,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve", help="what the interpreter imports, and other copies")
    resolve.add_argument("module", nargs="?", default=DEFAULT_MODULE, help=f"module to resolve (default: {DEFAULT_MODULE})")
    resolve.set_defaults(func=_cmd_resolve)

    record = subparsers.add_parser("record", help="snapshot a file before editing it")
    record.add_argument("file", help="the source file you are about to edit")
    record.add_argument("--module", default=None, help="module this file belongs to, re-resolved by verify")
    record.add_argument("--out", default=DEFAULT_RECORD, help=f"record path (default: {DEFAULT_RECORD})")
    record.set_defaults(func=_cmd_record)

    verify = subparsers.add_parser("verify", help="did the file change, parse, and stay the live copy")
    verify.add_argument("record", nargs="?", default=DEFAULT_RECORD, help=f"record path (default: {DEFAULT_RECORD})")
    verify.set_defaults(func=_cmd_verify)

    restore = subparsers.add_parser("restore", help="put the original back")
    restore.add_argument("record", nargs="?", default=DEFAULT_RECORD, help=f"record path (default: {DEFAULT_RECORD})")
    restore.add_argument("--dry-run", action="store_true", help="report the plan without writing")
    restore.add_argument("--force", action="store_true", help="restore even when the module moved")
    restore.set_defaults(func=_cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
