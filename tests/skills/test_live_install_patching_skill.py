"""Behaviour contract for the hermes-live-install-patching helper.

The helper decides three things an operator cannot check by eye: which copy of a
module the running interpreter imports, whether an edit landed in that copy, and
how to put the original bytes back. Each of those is exercised here against a
synthetic install tree in ``tmp_path`` — never against a real install, and never
by reading the script's source (git and the interpreter are the only seams).

The docs/CLI contract test keeps SKILL.md's commands honest: a documented
subcommand or flag the parser does not know is a copy-paste failure for whoever
follows the skill.
"""
from __future__ import annotations

import base64
import hashlib
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "software-development"
    / "hermes-live-install-patching"
)
SCRIPT = SKILL / "scripts" / "install_patch.py"
BASELINE = b"VALUE = 1\n"
BASELINE_SHA = hashlib.sha256(BASELINE).hexdigest()


@pytest.fixture
def helper():
    """The helper's *real* globals, so tests can drive its functions and patch its seams.

    ``runpy.run_path`` hands back a copy of the module namespace; patching that copy
    would leave the functions reading the original, so take the dict they close over.
    """
    return runpy.run_path(str(SCRIPT))["main"].__globals__


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _record_for(target: Path, module: str | None = None, tracked: bool = False, root: Path | None = None) -> dict:
    return {
        "file": str(target),
        "sha256": BASELINE_SHA,
        "size": len(BASELINE),
        "recorded_at": "2026-01-01T00:00:00Z",
        "module": module,
        "git": {"root": str(root), "head": "abc1234", "branch": "main", "dirty_files": 1} if root else {},
        "tracked": tracked,
        "rel_path": target.name if root else None,
        "original_b64": base64.b64encode(BASELINE).decode("ascii"),
    }


def test_candidates_put_the_running_copy_first(helper, tmp_path):
    running_dir, shadow_dir = tmp_path / "checkout", tmp_path / "environment"
    running = _write(running_dir / "probe_mod.py", "VALUE = 1\n")
    shadow = _write(shadow_dir / "probe_mod.py", "VALUE = 2\n")
    found = helper["module_candidates"]("probe_mod", running, [str(running_dir), str(shadow_dir)])
    assert found == [running.resolve(), shadow.resolve()]


def test_candidates_find_a_package_directory(helper, tmp_path):
    checkout = tmp_path / "checkout"
    init = _write(checkout / "probe_pkg" / "__init__.py", "")
    assert helper["module_candidates"]("probe_pkg", None, [str(checkout)]) == [init.resolve()]


@pytest.mark.parametrize("is_package", [False, True])
def test_import_root_is_the_directory_the_interpreter_adds(helper, tmp_path, is_package):
    target = _write(tmp_path / "level" / ("__init__.py" if is_package else "probe_mod.py"), "")
    expected = target.parent.parent if is_package else target.parent
    assert helper["import_root"](target, is_package) == expected


def test_repo_relative_falls_back_outside_the_repository(helper, tmp_path):
    inside = tmp_path / "pkg" / "mod.py"
    assert helper["repo_relative"](tmp_path, inside) == "pkg/mod.py"
    assert helper["repo_relative"](tmp_path / "other", inside) == str(inside)


def test_record_verify_restore_round_trip_without_git(helper, monkeypatch, tmp_path):
    monkeypatch.setitem(helper, "git_facts", lambda root: {})
    target = _write(tmp_path / "mod.py", BASELINE.decode("utf-8"))
    record = helper["build_record"](target)
    assert record["sha256"] == BASELINE_SHA
    assert record["tracked"] is False
    assert base64.b64decode(record["original_b64"]) == BASELINE

    before = helper["verify_record"](record)
    assert before["changed"] is False and before["parses"] is True
    assert helper["verify_exit_code"](before) == helper["EXIT_OK"]

    _write(target, "VALUE = 2\n")
    after = helper["verify_record"](record)
    assert after["changed"] is True and after["parses"] is True
    assert helper["verify_exit_code"](after) == helper["EXIT_CHANGED"]

    result = helper["restore_record"](record)
    assert result["method"] == "bytes" and result["restored"] is True
    assert target.read_bytes() == BASELINE
    assert helper["verify_record"](record)["changed"] is False


def test_verify_reports_a_half_applied_edit_as_unparseable(helper, tmp_path):
    target = _write(tmp_path / "mod.py", "VALUE = (1\n")
    report = helper["verify_record"](_record_for(target))
    assert report["changed"] is True and report["parses"] is False
    assert helper["verify_exit_code"](report) == helper["EXIT_UNPARSEABLE"]
    assert "line 1" in report["syntax_error"]


def test_verify_names_the_copy_the_module_resolves_to_now(helper, monkeypatch, tmp_path):
    target = _write(tmp_path / "checkout" / "probe_mod.py", "VALUE = 2\n")
    elsewhere = _write(tmp_path / "environment" / "probe_mod.py", "VALUE = 9\n")
    monkeypatch.setitem(
        helper, "module_origin", lambda name: {"module": name, "file": elsewhere, "is_package": False}
    )
    report = helper["verify_record"](_record_for(target, module="probe_mod"))
    assert report["module_file"] == str(elsewhere)
    assert report["same_file"] is False
    assert helper["verify_exit_code"](report) == helper["EXIT_CHANGED"]


def test_verify_treats_a_vanished_file_as_a_refusal(helper, tmp_path):
    report = helper["verify_record"](_record_for(tmp_path / "gone.py"))
    assert report["exists"] is False
    assert helper["verify_exit_code"](report) == helper["EXIT_REFUSED"]


def test_restore_uses_git_when_the_file_was_tracked(helper, monkeypatch, tmp_path):
    target = _write(tmp_path / "mod.py", "VALUE = 2\n")
    calls = []

    def fake_git(root, *args):
        calls.append((str(root), *args))
        target.write_bytes(BASELINE)  # what `git checkout <head> -- <path>` would produce
        return subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")

    monkeypatch.setitem(helper, "_run_git", fake_git)
    record = _record_for(target, tracked=True, root=tmp_path)
    assert helper["restore_plan"](record)[0] == "git"

    result = helper["restore_record"](record)
    assert result["method"] == "git" and result["restored"] is True
    assert calls == [(str(tmp_path), "checkout", "abc1234", "--", "mod.py")]
    assert target.read_bytes() == BASELINE


def test_restore_dry_run_writes_nothing(helper, monkeypatch, tmp_path):
    target = _write(tmp_path / "mod.py", "VALUE = 2\n")
    monkeypatch.setitem(helper, "git_facts", lambda root: {})

    def explode(*args, **kwargs):  # a dry run must not reach git at all
        raise AssertionError("git called during a dry run")

    monkeypatch.setitem(helper, "_run_git", explode)
    result = helper["restore_record"](_record_for(target), dry_run=True)
    assert result["restored"] is False and "dry run" in result["reason"]
    assert target.read_bytes() == b"VALUE = 2\n"


def test_restore_refuses_when_the_module_moved_and_needs_force(helper, monkeypatch, tmp_path):
    target = _write(tmp_path / "mod.py", "VALUE = 2\n")
    elsewhere = _write(tmp_path / "other" / "probe_mod.py", "VALUE = 9\n")
    monkeypatch.setitem(
        helper, "module_origin", lambda name: {"module": name, "file": elsewhere, "is_package": False}
    )
    record = _record_for(target, module="probe_mod")

    refused = helper["restore_record"](record)
    assert refused["method"] == "refused" and refused["exit_code"] == helper["EXIT_REFUSED"]
    assert target.read_bytes() == b"VALUE = 2\n"

    forced = helper["restore_record"](record, force=True)
    assert forced["restored"] is True
    assert target.read_bytes() == BASELINE


def test_restore_is_a_no_op_on_the_recorded_baseline(helper, monkeypatch, tmp_path):
    target = _write(tmp_path / "mod.py", BASELINE.decode("utf-8"))
    monkeypatch.setitem(helper, "git_facts", lambda root: {})
    result = helper["restore_record"](helper["build_record"](target))
    assert result["restored"] is False
    assert "already at the recorded baseline" in result["reason"]
    assert result["exit_code"] == helper["EXIT_OK"]


def test_resolve_report_lists_the_other_copies(helper, monkeypatch, tmp_path):
    running = _write(tmp_path / "checkout" / "probe_mod.py", "VALUE = 1\n")
    shadow = _write(tmp_path / "environment" / "probe_mod.py", "VALUE = 2\n")
    monkeypatch.setitem(
        helper, "module_origin", lambda name: {"module": name, "file": running, "is_package": False}
    )
    monkeypatch.setitem(helper, "git_facts", lambda root: {})
    report = helper["resolve_report"]("probe_mod", [str(running.parent), str(shadow.parent)])
    assert report["file"] == str(running)
    assert report["root"] == str(running.parent)
    assert report["other_copies"] == [str(shadow.resolve())]


def test_cli_resolve_exit_codes(helper, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "resolve", "json"])
    assert helper["main"]() == helper["EXIT_OK"]
    assert "file:" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "resolve", "no_such_module_anywhere"])
    assert helper["main"]() == helper["EXIT_REFUSED"]
    assert "Run the helper with the python that runs Hermes" in capsys.readouterr().err


def test_cli_record_refuses_a_missing_file_and_an_oversize_one(helper, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "record", str(tmp_path / "gone.py")])
    assert helper["main"]() == helper["EXIT_REFUSED"]
    assert "no such file" in capsys.readouterr().err

    big = _write(tmp_path / "big.py", "VALUE = 1\n")
    monkeypatch.setitem(helper, "MAX_EMBED_BYTES", 4)
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "record", str(big), "--out", str(tmp_path / "record.json")]
    )
    assert helper["main"]() == helper["EXIT_REFUSED"]
    assert "embed cap" in capsys.readouterr().err
    assert not (tmp_path / "record.json").exists()


def test_cli_verify_and_restore_exit_codes(helper, monkeypatch, capsys, tmp_path):
    monkeypatch.setitem(helper, "git_facts", lambda root: {})
    target = _write(tmp_path / "mod.py", BASELINE.decode("utf-8"))
    record_path = tmp_path / "record.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "record", str(target), "--out", str(record_path)])
    assert helper["main"]() == helper["EXIT_OK"]
    capsys.readouterr()

    _write(target, "VALUE = 2\n")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "verify", str(record_path)])
    assert helper["main"]() == helper["EXIT_CHANGED"]
    assert "changed: True" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "restore", str(record_path)])
    assert helper["main"]() == helper["EXIT_OK"]
    assert target.read_bytes() == BASELINE

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "verify", str(tmp_path / "absent.json")])
    assert helper["main"]() == helper["EXIT_REFUSED"]
    assert "no such record" in capsys.readouterr().err


def test_documented_commands_and_flags_exist_in_the_cli():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"install_patch\.py[\"']?\s+([a-z][a-z-]*)", text))
    assert documented == {"resolve", "record", "verify", "restore"}
    top = _cli_help()
    for subcommand in documented:
        assert subcommand in top, subcommand
    helps = "\n".join(_cli_help(subcommand) for subcommand in documented)
    # A flag the docs put in backticks must be a real option of one of the subcommands.
    for flag in set(re.findall(r"`(--[a-z][a-z-]+)", text)):
        assert flag in helps, flag


def _cli_help(*args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout
