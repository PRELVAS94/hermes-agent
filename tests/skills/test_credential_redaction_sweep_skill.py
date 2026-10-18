"""Tests for optional-skills/security/credential-redaction-sweep/scripts/redact_sweep.py.

Every credential in this file is derived at run time from a fixed seed string, never written as a
literal: a test suite full of credential-shaped constants is itself a leak the sweep reports.
"""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "security"
    / "credential-redaction-sweep"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import redact_sweep as rs


def synthetic(prefix: str, seed: str, length: int = 32) -> str:
    """A credential-shaped string derived from ``seed``; no literal secret lives in this file."""
    body = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    while len(body) < length:
        body += hashlib.sha256(body.encode("utf-8")).hexdigest()
    return prefix + body[:length]


GITHUB = synthetic("ghp_", "fixture-github")
OPENAI = synthetic("sk-", "fixture-openai")
TELEGRAM = "123456789:AA" + synthetic("", "fixture-telegram", 34)
ASSIGNED = synthetic("", "fixture-assignment", 24)
LETTER_HEX = "a" + synthetic("", "fixture-letter-hex", 23)  # starts with a letter: a hex blob


class TestReportedSpans:
    def test_finds_a_prefixed_token(self):
        found = rs.find_spans("token " + GITHUB + " end")
        assert [f.kind for f in found] == ["github_token"]
        assert (found[0].start, found[0].end) == (6, 6 + len(GITHUB))
        assert found[0].digest == hashlib.sha256(GITHUB.encode()).hexdigest()[:12]

    def test_the_reported_digest_is_not_the_value(self):
        found = rs.find_spans(GITHUB)
        assert GITHUB not in json.dumps([f.to_record() for f in found])

    def test_ignores_a_token_glued_to_a_word_character(self):
        # A slug or filename that happens to carry a prefix is not a leak.
        assert rs.find_spans("see delegate-task-" + OPENAI + ".md for the slug") == []

    def test_redaction_widens_the_pattern_set(self):
        glued = "x" + GITHUB
        assert rs.find_spans(glued, mode="report") == []
        assert [f.kind for f in rs.find_spans(glued, mode="redact")] == ["github_token"]
        assert rs.find_spans(glued, mode="redact")[0].start == 1

    def test_ignores_ordinary_code_and_attributes(self):
        text = "api_key = _load_simple_env(path)\npassword = args.protect\nsecret = os.environ.get\n"
        assert rs.find_spans(text, mode="report") == []
        assert rs.find_spans(text, mode="redact") == []

    def test_reports_assignment_forms(self):
        for value in (ASSIGNED, LETTER_HEX):
            text = f'api_key = "{value}"\n'
            found = rs.find_spans(text, mode="report")
            assert [f.kind for f in found] == ["api_key"], value
            assert text[found[0].start : found[0].end] == value

    def test_a_hex_blob_is_a_credential_even_though_it_parses_as_an_identifier(self):
        found = rs.find_spans(f'refresh_token = "{LETTER_HEX}"\n', mode="report")
        assert [f.kind for f in found] == ["refresh_token"]

    def test_marks_documentation_placeholders_without_dropping_them(self):
        text = 'client_secret = "your-client-secret-1234"\n'
        found = rs.find_spans(text, mode="report")
        assert [f.placeholder_like for f in found] == [True]

    def test_overlapping_shapes_become_one_span(self):
        text = 'refresh_token = "{}"\n'.format(synthetic("ghp_", "fixture-overlap", 36))
        found = rs.find_spans(text, mode="report")
        assert len(found) == 1


class TestRedaction:
    def test_render_replaces_only_the_span(self, tmp_path):
        target = tmp_path / "digest.md"
        target.write_text(f"line one\nbot token: {TELEGRAM} trailing\n", encoding="utf-8")
        record = rs.redact_file(target)
        after = target.read_text(encoding="utf-8")
        assert TELEGRAM not in after
        assert after == "line one\nbot token: {} trailing\n".format("[REDACTED:telegram_bot_token]")
        assert record["changed"] is True and record["written"] is True

    def test_line_count_and_byte_delta_hold(self, tmp_path):
        target = tmp_path / "digest.md"
        before = f"a: {GITHUB}\nb\nc: {ASSIGNED}\n"
        target.write_text(before, encoding="utf-8")
        record = rs.redact_file(target)
        after = target.read_text(encoding="utf-8")
        assert after.count("\n") == before.count("\n")
        assert len(before.encode()) - len(after.encode()) == sum(
            span["length"] - len("[REDACTED:{}]".format(span["kind"])) for span in record["spans"]
        )
        assert all(record["checks"].values())

    def test_record_never_carries_the_value(self, tmp_path):
        target = tmp_path / "digest.md"
        target.write_text(f"token: {GITHUB}\n", encoding="utf-8")
        record = rs.redact_file(target)
        blob = json.dumps(record)
        assert GITHUB not in blob
        assert record["spans"][0]["sha256_12"] == hashlib.sha256(GITHUB.encode()).hexdigest()[:12]
        assert record["before"]["sha256"] != record["after"]["sha256"]

    def test_second_pass_is_unchanged(self, tmp_path):
        target = tmp_path / "digest.md"
        target.write_text(f"token: {GITHUB}\n", encoding="utf-8")
        rs.redact_file(target)
        again = rs.redact_file(target)
        assert again["changed"] is False
        assert "written" not in again

    def test_dry_run_writes_nothing(self, tmp_path):
        target = tmp_path / "digest.md"
        original = f"token: {GITHUB}\n"
        target.write_text(original, encoding="utf-8")
        record = rs.redact_file(target, dry_run=True)
        assert record["changed"] is True and record["written"] is False
        assert target.read_text(encoding="utf-8") == original

    def test_no_temp_file_is_left_behind(self, tmp_path):
        target = tmp_path / "digest.md"
        target.write_text(f"token: {GITHUB}\n", encoding="utf-8")
        rs.redact_file(target)
        assert list(tmp_path.glob("*.tmp")) == []


class TestFailClosed:
    def test_assert_clean_refuses_a_dirty_render(self):
        with pytest.raises(rs.RedactionRefused):
            rs.assert_clean(f"still here: {GITHUB}")
        rs.assert_clean("nothing to see\n")

    def test_a_blind_redactor_leaves_the_file_alone(self, tmp_path, monkeypatch):
        # Every check between the render and the write must run, and a failure must not write.
        target = tmp_path / "digest.md"
        original = f"token: {GITHUB}\n"
        target.write_text(original, encoding="utf-8")
        monkeypatch.setattr(rs, "render", lambda text, spans: text)
        with pytest.raises(rs.RedactionRefused):
            rs.redact_file(target)
        assert target.read_bytes() == original.encode("utf-8")

    def test_a_rerun_that_still_sees_a_shape_refuses(self, tmp_path, monkeypatch):
        target = tmp_path / "digest.md"
        original = f"token: {GITHUB}\n"
        target.write_text(original, encoding="utf-8")
        real = rs.find_spans
        calls = {"count": 0}

        def blind_on_rescan(text, *, mode="report"):
            calls["count"] += 1
            if calls["count"] > 1:  # the writer's own re-scan of the rendered text
                return [rs.Finding(kind="unknown_shape", start=0, end=1, line=1, digest="0" * 12)]
            return real(text, mode=mode)

        monkeypatch.setattr(rs, "find_spans", blind_on_rescan)
        with pytest.raises(rs.RedactionRefused):
            rs.redact_file(target)
        assert target.read_text(encoding="utf-8") == original


class TestCommandLine:
    def test_scan_exit_codes(self, tmp_path, capsys):
        dirty = tmp_path / "dirty.md"
        dirty.write_text(f"token: {GITHUB}\n", encoding="utf-8")
        clean = tmp_path / "clean.md"
        clean.write_text("nothing here\n", encoding="utf-8")
        assert rs.main(["scan", str(dirty)]) == 1
        assert rs.main(["scan", str(clean)]) == 0
        assert "github_token" in capsys.readouterr().out

    def test_scan_json_report_parses(self, tmp_path, capsys):
        dirty = tmp_path / "dirty.md"
        dirty.write_text(f"token: {GITHUB}\n", encoding="utf-8")
        assert rs.main(["scan", str(dirty), "--json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["findings"][0]["kind"] == "github_token"
        assert GITHUB not in json.dumps(report)

    def test_redact_writes_the_record_and_the_file(self, tmp_path, capsys):
        dirty = tmp_path / "dirty.md"
        dirty.write_text(f"token: {GITHUB}\n", encoding="utf-8")
        record_path = tmp_path / "record.json"
        assert rs.main(["redact", str(dirty), "--record", str(record_path)]) == 0
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["written"] == 1
        assert GITHUB not in json.dumps(record)
        assert GITHUB not in dirty.read_text(encoding="utf-8")
        assert capsys.readouterr().out.strip().endswith("1 file(s) rewritten")

    def test_dry_run_leaves_the_bytes_alone(self, tmp_path):
        dirty = tmp_path / "dirty.md"
        original = f"token: {GITHUB}\n"
        dirty.write_text(original, encoding="utf-8")
        assert rs.main(["redact", str(dirty), "--dry-run"]) == 0
        assert dirty.read_text(encoding="utf-8") == original


class TestSkipsAreReported:
    def test_archive_members_are_scanned(self, tmp_path):
        archive = tmp_path / "snapshot.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("digest.md", f"token: {GITHUB}\n")
            zf.writestr("notes.txt", "nothing here\n")
        report = rs.scan_paths([str(archive)])
        assert [hit["path"].split("::")[-1] for hit in report["findings"]] == ["digest.md"]
        assert report["findings"][0]["kind"] == "github_token"

    def test_redact_does_not_rewrite_an_archive(self, tmp_path):
        archive = tmp_path / "snapshot.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("digest.md", f"token: {GITHUB}\n")
        before = archive.read_bytes()
        result = rs.redact_paths([str(archive)])
        assert result["written"] == 0
        assert [entry["reason"] for entry in result["skipped"]] == [
            "archive: scan the members, rewrite via zipfile"
        ]
        assert archive.read_bytes() == before

    def test_an_oversized_file_is_skipped_not_called_clean(self, tmp_path, monkeypatch):
        big = tmp_path / "big.log"
        big.write_text(f"token: {GITHUB}\n", encoding="utf-8")
        monkeypatch.setattr(rs, "MAX_SCAN_BYTES", 4)
        report = rs.scan_paths([str(big)])
        assert report["findings"] == []
        assert report["skipped"] == [{"path": str(big), "reason": "larger than the scan cap"}]

    def test_a_non_utf8_file_is_skipped_not_called_clean(self, tmp_path):
        binary = tmp_path / "sessions.sqlite"
        binary.write_bytes(b"\x80\x81 token: " + GITHUB.encode("utf-8"))
        report = rs.scan_paths([str(binary)])
        assert report["findings"] == []
        assert report["skipped"][0]["reason"] == "not UTF-8 text"


class TestDocumentedCli:
    """The flags the SKILL.md advertises must be the flags the parser accepts, and only those."""

    @staticmethod
    def _option_strings(parser) -> set:
        options = set()
        for action in parser._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    options |= TestDocumentedCli._option_strings(sub)
        return options

    def test_documented_flags_and_parser_flags_are_the_same_set(self):
        documented = set(re.findall(r"--[a-z][a-z-]+", SKILL_MD.read_text(encoding="utf-8")))
        exposed = self._option_strings(rs.build_parser()) - {"-h", "--help"}
        assert documented, "no flags found in SKILL.md — the extraction, not the skill, is broken"
        assert documented == exposed, f"documented-only: {sorted(documented - exposed)}; undocumented: {sorted(exposed - documented)}"
