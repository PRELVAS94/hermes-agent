"""Contract for the hermes-live-install-patching optional skill.

Two halves, both behaviour contracts:

* The document — ``SKILL.md`` is what an installer and the listings read, so its front
  matter and section order are checked as data (via the loader's own parser), not as text
  by eye.
* The helper — it decides three things an operator cannot check by eye: which copy of a
  module the running interpreter imports, whether an edit landed in that copy, and how to
  put the original bytes back. Each is exercised against a synthetic install tree in
  ``tmp_path`` — never against a real install — and every git call goes through the
  helper's single ``_run_git`` seam, which the tests replace.

The CLI contract keeps SKILL.md's copy-pasteable commands honest: a documented subcommand
or flag the parser does not know is a failure for whoever follows the skill.
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

from agent.skill_utils import parse_frontmatter

SKILL = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "software-development"
    / "hermes-live-install-patching"
)
SKILL_MD = SKILL / "SKILL.md"
SCRIPT = SKILL / "scripts" / "install_patch.py"
SCRIPTS = sorted((SKILL / "scripts").glob("*.py"))
BASELINE = b"VALUE = 1\n"
BASELINE_SHA = hashlib.sha256(BASELINE).hexdigest()

AUTHOR = "Pedro Relvas (PRELVAS94), Hermes Agent"
# The authoring standards' section order, minus the ones this skill adds for its own runbook.
REQUIRED_SECTIONS = (
    "When to Use",
    "Prerequisites",
    "How to Run",
    "Quick Reference",
    "Procedure",
    "Pitfalls",
    "Verification",
)
MARKETING = re.compile(
    r"\b(powerful|comprehensive|seamless|advanced|revolutionary|cutting-edge|"
    r"state-of-the-art|best-in-class|blazing)\b",
    re.IGNORECASE,
)


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


def _frontmatter() -> tuple[dict, str]:
    return parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))


def test_skill_md_exists_and_parses_as_front_matter():
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"
    metadata, body = _frontmatter()
    assert metadata, "the front matter did not parse into a mapping"
    assert metadata["name"] == SKILL.name
    assert body.lstrip().startswith("# "), "the body must open with the skill title"


def test_frontmatter_declares_what_an_install_reads():
    metadata, _ = _frontmatter()
    assert metadata["version"]
    assert metadata["license"] == "MIT"
    assert metadata["platforms"] == ["linux", "macos", "windows"]
    hermes = metadata["metadata"]["hermes"]
    assert hermes["category"] == SKILL.parent.name, "the category must match the shipped path"
    assert hermes["tags"], "a tagless skill does not surface in search"


def test_title_is_the_skill_name_plus_skill():
    _, body = _frontmatter()
    title = body.lstrip().splitlines()[0]
    assert title.endswith(" Skill"), title
    assert "live install patching" in title.lower(), title


def test_description_is_a_short_plain_sentence():
    metadata, _ = _frontmatter()
    description = metadata["description"]
    assert len(description) <= 60, f"{len(description)} chars: {description!r}"
    assert description.endswith("."), description
    assert description.count(".") == 1, f"not one sentence: {description!r}"
    assert description == description.strip()
    offender = MARKETING.search(description)
    assert offender is None, f"marketing word {offender and offender.group(0)!r} in the description"


def test_author_credits_the_human_before_the_agent():
    metadata, _ = _frontmatter()
    assert metadata["author"] == AUTHOR


def test_required_sections_appear_once_each_in_the_standard_order():
    text = SKILL_MD.read_text(encoding="utf-8")
    positions = {}
    for section in REQUIRED_SECTIONS:
        heading = f"\n## {section}\n"
        assert text.count(heading) == 1, f"## {section} must appear exactly once as a section"
        positions[section] = text.index(heading)
    ordered = [name for name, _ in sorted(positions.items(), key=lambda item: item[1])]
    assert ordered == list(REQUIRED_SECTIONS)


def test_the_skill_ships_the_helper_its_commands_name():
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    assert SCRIPTS, "scripts/ ships no helper to run"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_every_helper_script_is_importable_and_has_an_entry_point(script):
    # run_name is not "__main__", so importing the helper runs its definitions only.
    namespace = runpy.run_path(str(script))
    assert callable(namespace.get("main")), f"{script.name} exposes no main(argv) entry point"


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


@pytest.mark.platforms("posix")
def test_same_path_matches_two_routes_to_one_file(helper, tmp_path):
    real = _write(tmp_path / "checkout" / "probe_mod.py", "VALUE = 1\n")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "checkout", target_is_directory=True)
    assert helper["same_path"](real, alias / "probe_mod.py") is True
    assert helper["same_path"](real, tmp_path / "checkout" / "other.py") is False
    assert helper["same_path"](None, real) is False


@pytest.mark.platforms("posix")
def test_verify_does_not_call_a_symlinked_route_a_wrong_tree(helper, monkeypatch, tmp_path):
    """A record taken through a symlink names the same file the runtime resolves.

    macOS reaches a temp directory by two paths (/var and /private/var); comparing the raw
    strings reported a correct edit as ``wrong_tree`` and made restore refuse to undo it.
    """
    _write(tmp_path / "checkout" / "probe_mod.py", "VALUE = 2\n")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "checkout", target_is_directory=True)
    record = _record_for(alias / "probe_mod.py", module="probe_mod")
    real = tmp_path / "checkout" / "probe_mod.py"
    monkeypatch.setitem(
        helper, "module_origin", lambda name: {"module": name, "file": real, "is_package": False}
    )

    report = helper["verify_record"](record)
    assert report["same_file"] is True and "wrong_tree" not in report
    assert helper["restore_plan"](record)[0] == "bytes"


def test_documented_commands_and_flags_exist_in_the_cli(helper, capsys):
    text = SKILL_MD.read_text(encoding="utf-8")
    documented = set(re.findall(r"install_patch\.py[\"']?\s+([a-z][a-z-]*)", text))
    assert documented == {"resolve", "record", "verify", "restore"}
    usage = helper["build_parser"]().format_help()
    known = set(re.search(r"\{([a-z,-]+)\}", usage).group(1).split(","))
    assert known == documented, f"the parser accepts {sorted(known)}"
    helps = "\n".join(_subcommand_help(helper, capsys, name) for name in sorted(documented))
    # A flag the docs put in backticks must be a real option of one of the subcommands.
    for flag in set(re.findall(r"`(--[a-z][a-z-]+)", text)):
        assert flag in helps, flag


def _subcommand_help(helper, capsys, name: str) -> str:
    """The subcommand's own ``--help``, rendered in-process: argparse exits 0 with it on stdout."""
    with pytest.raises(SystemExit) as exit_info:
        helper["main"]([name, "--help"])
    assert exit_info.value.code == 0, name
    return capsys.readouterr().out
