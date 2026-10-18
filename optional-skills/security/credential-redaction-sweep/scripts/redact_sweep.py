#!/usr/bin/env python3
"""Scan a text surface for credential-shaped strings, then redact them in place.

Two modes over one pattern set. ``scan`` reports what it found and changes nothing; a finding
carries the kind, the byte span and a truncated digest of the match, never the match itself.
``redact`` rewrites each file atomically, replacing only the matched characters with
``[REDACTED:<kind>]``, and refuses to write at all when its own re-scan of the rendered text is
still credential-shaped (fail closed).

Deterministic and stdlib only: the same bytes in give the same report out, and nothing here reads
the network, a process environment or a user home directory.

    python redact_sweep.py scan   memory/ notes.md
    python redact_sweep.py redact memory/ --record redaction-record.json
    python redact_sweep.py redact memory/ --dry-run

Exit codes: ``0`` clean (or the redaction was written), ``1`` ``scan`` found something, ``3`` a
write was refused because the rendered text was not clean, ``2`` bad usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

RECORD_SCHEMA = 1
PLACEHOLDER = "[REDACTED:{kind}]"
MAX_SCAN_BYTES = 16 * 1024 * 1024


class RedactionRefused(RuntimeError):
    """The writer declined: the rendered text was not clean, so nothing was written."""


class Shape(NamedTuple):
    """One credential shape: its kind, its body regex, and how it is matched per mode."""

    kind: str
    body: str
    anchored: bool = True
    unanchored_for_redaction: bool = False


#: A credential shape is recognised in a report only between token boundaries; the redactor also
#: accepts the unanchored form, because a 20+ character run after one of these prefixes is never
#: English and leaving it in place is the failure this script exists to stop.
_TOKEN_CHARS = r"A-Za-z0-9_\-"
_LEFT = r"(?<![" + _TOKEN_CHARS + r"/])"
_RIGHT = r"(?![" + _TOKEN_CHARS + r"])"

SHAPES: tuple[Shape, ...] = (
    Shape("github_token", r"gh[pousr]_[A-Za-z0-9]{20,}", anchored=True, unanchored_for_redaction=True),
    Shape("github_pat", r"github_pat_[A-Za-z0-9_]{20,}", anchored=True, unanchored_for_redaction=True),
    Shape("aws_access_key", r"AKIA[0-9A-Z]{16}", anchored=True, unanchored_for_redaction=True),
    Shape("google_api_key", r"AIza[0-9A-Za-z_\-]{35}", anchored=True, unanchored_for_redaction=True),
    Shape("slack_token", r"xox[abprs]-[0-9A-Za-z-]{10,}", anchored=True, unanchored_for_redaction=True),
    Shape("openai_key", r"sk-[A-Za-z0-9]{20,}", anchored=True, unanchored_for_redaction=True),
    Shape(
        "telegram_bot_token",
        r"\d{8,10}:AA[A-Za-z0-9_\-]{30,}",
        anchored=True,
        unanchored_for_redaction=True,
    ),
    # Assembled from fragments so this file is not itself a PEM-shaped literal.
    Shape("private_key", "-----BEGIN " + r"[A-Z ]{0,24}" + "PRIVATE KEY-----", anchored=False),
)

_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|client[_-]?secret|access[_-]?key|refresh[_-]?token"
    r"|auth[_-]?token|token|password|passwd|passphrase)\b\s*[:=]\s*[\"']?([A-Za-z0-9+/_.\-]{16,})[\"']?"
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_DOC_PLACEHOLDER = re.compile(
    r"(?i)^(?:<|your[_-]|x{3}|example|dummy|planted|xyzzy|placeholder|redacted|change[_-]?me"
    r"|not[_-]the[_-])"
)


@dataclass(frozen=True)
class Finding:
    """One span: kind, offsets, line and a truncated digest — never the matched value."""

    kind: str
    start: int
    end: int
    line: int
    digest: str
    placeholder_like: bool = False

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def placeholder(self) -> str:
        return PLACEHOLDER.format(kind=self.kind)

    def to_record(self) -> dict:
        return {
            "kind": self.kind,
            "line": self.line,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "sha256_12": self.digest,
            "placeholder_like": self.placeholder_like,
        }


def _compile(shape: Shape, *, mode: str) -> re.Pattern[str]:
    if shape.unanchored_for_redaction and mode == "redact":
        body = shape.body
    elif shape.anchored:
        body = _LEFT + shape.body + _RIGHT
    else:
        body = shape.body
    return re.compile(body)


REPORT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (s.kind, _compile(s, mode="report")) for s in SHAPES
)
REDACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (s.kind, _compile(s, mode="redact")) for s in SHAPES
)


def _finding(kind: str, start: int, end: int, text: str, *, placeholder_like: bool = False) -> Finding:
    return Finding(
        kind=kind,
        start=start,
        end=end,
        line=text.count("\n", 0, start) + 1,
        digest=hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()[:12],
        placeholder_like=placeholder_like,
    )


def _ordinary_value(value: str) -> bool:
    """True for ordinary code or prose an assignment keyword happened to sit in front of.

    A dotted attribute (``args.protect``) or a snake_case identifier (``_load_simple_env``) is
    code. A bare hex or base64 blob (``refresh_token = "a1b2…"``) also parses as an identifier,
    but it is a credential shape, so it is *not* ordinary: only the underscored/dotted forms are.
    """
    if "://" in value:
        return True
    if not (re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)):
        return True
    if not _IDENTIFIER.fullmatch(value):
        return False
    return "." in value or value.count("_") >= 2


def _drop_overlaps(spans: list[Finding]) -> list[Finding]:
    kept: list[Finding] = []
    for span in spans:
        if kept and span.start < kept[-1].end:
            continue
        kept.append(span)
    return kept


def find_spans(text: str, *, mode: str = "report") -> list[Finding]:
    """Every credential-shaped span in ``text``, in document order, overlaps resolved.

    ``mode="report"`` uses the anchored shapes only: a leak report full of prose false positives
    gets ignored, so precision matters more here than recall. ``mode="redact"`` adds the unanchored
    variants and the assignment keyword layer, because a miss there is a live secret on disk.
    """
    patterns = REDACT_PATTERNS if mode == "redact" else REPORT_PATTERNS
    found: list[Finding] = []
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            found.append(_finding(kind, match.start(), match.end(), text))
    for match in _ASSIGNMENT.finditer(text):
        value = match.group(2)
        if _ordinary_value(value):
            continue
        found.append(
            _finding(
                match.group(1).lower(),
                match.start(2),
                match.end(2),
                text,
                placeholder_like=bool(_DOC_PLACEHOLDER.match(value)),
            )
        )
    found.sort(key=lambda f: (f.start, -f.end))
    return _drop_overlaps(found)


def render(text: str, spans: Iterable[Finding]) -> str:
    """``text`` with every span replaced by its placeholder; later spans first so offsets hold."""
    out = text
    for span in sorted(spans, key=lambda f: f.start, reverse=True):
        out = out[: span.start] + span.placeholder + out[span.end :]
    return out


def assert_clean(text: str) -> None:
    """Raise :class:`RedactionRefused` when ``text`` still carries a credential shape."""
    remaining = find_spans(text, mode="redact")
    if remaining:
        kinds = ", ".join(sorted({f.kind for f in remaining}))
        raise RedactionRefused(
            f"rendered text still carries {len(remaining)} credential-shaped span(s): {kinds}"
        )


def verify_render(before: str, after: str, spans: list[Finding]) -> dict:
    """The four checks a redaction must pass before it is allowed to touch the file."""
    checks = {
        "line_count_unchanged": after.count("\n") == before.count("\n"),
        "byte_delta_matches_spans": (
            len(before.encode("utf-8"))
            - len(after.encode("utf-8"))
            == sum(f.length - len(f.placeholder) for f in spans)
        ),
        "placeholder_for_every_span": all(f.placeholder in after for f in spans),
        "nothing_credential_shaped_remains": not find_spans(after, mode="redact"),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RedactionRefused(f"verification failed: {', '.join(failed)}")
    return checks


def atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step, leaving the original untouched if anything fails."""
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _digest_block(data: bytes, text: str) -> dict:
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "lines": text.count("\n"),
    }


def redact_file(path: Path, *, dry_run: bool = False) -> dict:
    """Redact one text file in place and return its integrity record.

    ``changed`` is False for a file with nothing to redact, which is a normal result and not an
    error. On a failed check the function raises before writing, so the file keeps its bytes.
    """
    before_bytes = path.read_bytes()
    before_text = before_bytes.decode("utf-8")
    spans = find_spans(before_text, mode="redact")
    record: dict = {
        "schema": RECORD_SCHEMA,
        "path": str(path),
        "changed": bool(spans),
        "before": _digest_block(before_bytes, before_text),
        "spans": [span.to_record() for span in spans],
    }
    if not spans:
        return record
    after_text = render(before_text, spans)
    record["checks"] = verify_render(before_text, after_text, spans)
    after_bytes = after_text.encode("utf-8")
    record["after"] = _digest_block(after_bytes, after_text)
    if dry_run:
        record["written"] = False
        return record
    atomic_write(path, after_bytes)
    if path.read_bytes() != after_bytes:
        raise RedactionRefused(f"read-back after the write does not match the rendered bytes: {path}")
    record["written"] = True
    return record


def collect(paths: Iterable[str | Path]) -> tuple[list[Path], list[Path], list[dict]]:
    """``(text files, archives, skips)`` under ``paths`` — sorted, so a report is reproducible.

    A file too large to sweep is skipped *and recorded*: a silent skip reads as "clean".
    """
    files: list[Path] = []
    archives: list[Path] = []
    skipped: list[dict] = []
    for path in sorted({Path(p) for p in paths}):
        candidates = sorted(c for c in path.rglob("*") if c.is_file()) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.suffix.lower() == ".zip":
                archives.append(candidate)
            elif candidate.stat().st_size > MAX_SCAN_BYTES:
                skipped.append({"path": str(candidate), "reason": "larger than the scan cap"})
            else:
                files.append(candidate)
    return files, archives, skipped


def archive_members(path: Path) -> Iterator[tuple[str, str]]:
    """``(member, text)`` for every UTF-8 member of a zip archive — a tree sweep misses these."""
    try:
        with zipfile.ZipFile(path) as archive:
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                if info.is_dir() or info.file_size > MAX_SCAN_BYTES:
                    continue
                try:
                    yield info.filename, archive.read(info).decode("utf-8")
                except (UnicodeDecodeError, RuntimeError, zipfile.BadZipFile):
                    continue
    except (OSError, zipfile.BadZipFile):
        return


def scan_paths(paths: Iterable[str]) -> dict:
    """Report credential-shaped spans across files and archive members, changing nothing."""
    files, archives, skipped = collect(paths)
    findings: list[dict] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append({"path": str(path), "reason": "not UTF-8 text"})
            continue
        for span in find_spans(text, mode="report"):
            findings.append({"path": str(path), **span.to_record()})
    for path in archives:
        for member, text in archive_members(path):
            for span in find_spans(text, mode="report"):
                findings.append({"path": f"{path}::{member}", **span.to_record()})
    return {
        "scanned": [str(p) for p in files],
        "archives": [str(p) for p in archives],
        "skipped": skipped,
        "findings": findings,
    }


def redact_paths(paths: Iterable[str], *, dry_run: bool = False) -> dict:
    """Redact every text file under ``paths``; archives are scanned, never rewritten here."""
    files, archives, skipped = collect(paths)
    records: list[dict] = []
    for path in files:
        try:
            records.append(redact_file(path, dry_run=dry_run))
        except UnicodeDecodeError:
            skipped.append({"path": str(path), "reason": "not UTF-8 text"})
    for path in archives:
        skipped.append({"path": str(path), "reason": "archive: scan the members, rewrite via zipfile"})
    return {
        "records": records,
        "skipped": skipped,
        "written": sum(1 for record in records if record.get("written")),
    }


def _print_scan(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    for hit in result["findings"]:
        marker = " [documentation placeholder]" if hit["placeholder_like"] else ""
        print(
            f"{hit['path']}:{hit['line']}: {hit['kind']} ({hit['length']} bytes, "
            f"sha256:{hit['sha256_12']}){marker}"
        )
    print(
        f"scanned {len(result['scanned'])} file(s), {len(result['archives'])} archive(s): "
        f"{len(result['findings'])} finding(s), {len(result['skipped'])} skipped"
    )
    for entry in result["skipped"]:
        print(f"  skipped: {entry['path']} — {entry['reason']}")


def _print_redact(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    for record in result["records"]:
        state = "unchanged" if not record["changed"] else ("written" if record.get("written") else "not written")
        print(f"{record['path']}: {state} ({len(record['spans'])} span(s))")
    for entry in result["skipped"]:
        print(f"  skipped: {entry['path']} — {entry['reason']}")
    print(f"{result['written']} file(s) rewritten")


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Exposed so a test can hold the skill's documented flags to it."""
    parser = argparse.ArgumentParser(prog="redact_sweep.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="report credential-shaped spans; change nothing")
    redact = subparsers.add_parser("redact", help="replace each span with its placeholder, in place")
    for command in (scan, redact):
        command.add_argument("paths", nargs="+", help="files, directories or zip archives")
        command.add_argument("--json", action="store_true", help="machine-readable output")
    redact.add_argument("--record", help="also write the per-file integrity record to this path")
    redact.add_argument("--dry-run", action="store_true", help="render and verify, write nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        result = scan_paths(args.paths)
        _print_scan(result, args.json)
        return 1 if result["findings"] else 0
    try:
        redacted = redact_paths(args.paths, dry_run=args.dry_run)
    except RedactionRefused as refused:
        print(f"refused, nothing written: {refused}", file=sys.stderr)
        return 3
    if args.record:
        Path(args.record).write_text(json.dumps(redacted, indent=2, sort_keys=True), encoding="utf-8")
    _print_redact(redacted, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
