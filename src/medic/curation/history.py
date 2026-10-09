"""Scaffold, and validate, append-only curation history records.

Adopted from dismech (``scripts/new_history.py``) so MeDIC history records share dismech's
format; only the target kinds and repository differ.

History records (``history/<kind>/<slug>/<TIMESTAMP>-<actor>-<shortid>.yaml``)
are append-only curation/review/audit provenance files. They are documented in
``src/medic/schema/history.yaml`` and validated with ``just validate-history``.

This helper removes the friction that has kept curation PRs from including a
record: it generates the correct path, a UTC timestamp, a collision-proof
session id, and a schema-valid skeleton, so a curator only supplies the event
content. It is the intended way to *start* a record -- edit the emitted
``details`` afterwards, then validate with ``just validate-history <path>``.

Examples
--------
Record creation of a combination entry (AI-assisted)::

    uv run python -m medic.curation.history new \
        --kind combination --slug acetylsalicylic-acid-omeprazole \
        --event CREATE --outcome changed \
        --summary "Create: acetylsalicylic acid / omeprazole" \
        --agent-tool claude-code --model claude-opus-5-5 \
        --sections claims --details "Curated from DailyMed setid ae8f4eb4..., v1."

Record a human review that changed nothing::

    uv run python -m medic.curation.history new \
        --kind combination --slug acetylsalicylic-acid-omeprazole \
        --event REVIEW --outcome no_change --summary "Review" \
        --actor-name kshefchek --actor-type human --details "Checked attribution."

The script prints the path it created as its final stdout line, so automation
can capture it.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO_URL = "https://github.com/monarch-initiative/medic"

# kind -> (kb directory, requires a slug-derived path). ``schema``/``other``
# targets live outside kb/, so they require an explicit --path.
KIND_DIRS = {
    "combination": "kb/combinations",
}
KINDS = list(KIND_DIRS) + ["schema", "other"]

EVENTS = ["GENERAL", "CREATE", "EDIT", "REVIEW", "AUDIT"]
OUTCOMES = ["changed", "no_change", "needs_followup", "blocked"]
ACTOR_TYPES = ["human", "ai_agent", "automation", "other"]


class _LiteralStr(str):
    """A string forced to serialize as a YAML literal block scalar (``|``)."""


def _literal_representer(dumper: yaml.Dumper, data: _LiteralStr):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(_LiteralStr, _literal_representer)


def _actor_slug(name: str) -> str:
    """Filename-safe actor token: lowercase, non-alnum collapsed to '-'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "actor"


def _expand_ref(value: str, kind: str) -> str:
    """Turn a bare issue/PR number into a full GitHub URL; pass URLs through."""
    value = value.strip()
    if not value:
        return value
    if value.startswith(("http://", "https://")):
        return value
    if value.isdigit():
        segment = "issues" if kind == "issue" else "pull"
        return f"{REPO_URL}/{segment}/{value}"
    return value


def normalize_model_id(model: str) -> str:
    """The model id alone: drop runtime suffixes such as a context-window tag.

    ``claude-opus-5-5[1m]`` is the model ``claude-opus-5-5`` run with a 1M-token context
    window; the bracket is a session setting, not part of the identifier a later reader
    would look up.
    """
    return re.sub(r"\[[^\]]*\]$", "", model.strip())


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def build_record(args: argparse.Namespace) -> tuple[dict, Path]:
    now = datetime.now(UTC).replace(microsecond=0)
    file_ts = now.strftime("%Y-%m-%dT%H%M%SZ")
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    shortid = secrets.token_hex(3)

    # Resolve the target path.
    if args.kind in KIND_DIRS:
        if not args.slug:
            sys.exit(f"error: --slug is required for kind '{args.kind}'")
        path = f"{KIND_DIRS[args.kind]}/{args.slug}.yaml"
    else:
        if not args.path:
            sys.exit(f"error: --path is required for kind '{args.kind}'")
        path = args.path

    slug = args.slug or Path(path).stem

    # Which model did the work is the provenance a reader of an AI-curated record most
    # needs, and the one that cannot be recovered later. dismech leaves it optional.
    if args.actor_type == "ai_agent" and not args.model:
        sys.exit("error: --model is required for ai_agent actors (e.g. claude-opus-5-5)")

    actor_token = _actor_slug(args.actor_name)
    session_id = f"{file_ts}-{actor_token}-{shortid}"

    actor: dict = {"type": args.actor_type, "name": args.actor_name}
    if args.model:
        actor["model"] = normalize_model_id(args.model)
    if args.agent_tool:
        actor["agent_tool"] = args.agent_tool
    if args.agent_version:
        actor["agent_version"] = args.agent_version

    target: dict = {"kind": args.kind}
    if slug:
        target["slug"] = slug
    target["path"] = path

    event: dict = {"type": args.event, "outcome": args.outcome}
    sections = _split_csv(args.sections)
    if sections:
        event["sections"] = sections
    event["summary"] = args.summary
    event["details"] = _LiteralStr(
        (args.details or "TODO: replace with curation/review notes.").rstrip() + "\n"
    )

    record = {
        "history_version": 1,
        "target": target,
        "session": {
            "id": session_id,
            "timestamp": iso_ts,
            "actors": [actor],
        },
        "links": {
            "issues": [_expand_ref(v, "issue") for v in args.issue],
            "prs": [_expand_ref(v, "pr") for v in args.pr],
            "urls": list(args.url),
        },
        "events": [event],
    }

    # history/<kind>s/<slug>/ mirrors the KB layout; schema/other records use the kind name.
    subdir = {"combination": "combinations"}.get(args.kind, args.kind)
    out_dir = Path("history") / subdir / slug
    out_path = out_dir / f"{session_id}.yaml"
    return record, out_path


def target_missing_warning(path: str) -> str | None:
    """Warn when scaffolding a record whose target does not exist on disk.

    Committed records must point at a real file (or carry ``target.superseded_by`` when
    the entry was renamed later), so catching a bad slug here saves a red check.
    """
    if Path(path).exists():
        return None
    return (
        f"warning: target {path} does not exist yet.\n"
        "  If this is a typo or a stale slug, fix --slug/--path before committing:\n"
        "  committed records must point at a real file, or carry a\n"
        "  'target.superseded_by' block when the entry was renamed later.\n"
        "  If you are scaffolding ahead of creating the entry, ignore this."
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scaffold a schema-valid MeDIC history record.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--kind", required=True, choices=KINDS, help="Target object kind.")
    p.add_argument("--slug", help="Target slug / file stem (e.g. acetylsalicylic-acid-omeprazole).")
    p.add_argument("--path", help="Explicit repo-relative target path (required for schema/other).")
    p.add_argument("--event", default="EDIT", choices=EVENTS, help="Event type (default: EDIT).")
    p.add_argument("--outcome", default="changed", choices=OUTCOMES, help="Event outcome (default: changed).")
    p.add_argument("--summary", required=True, help="Short one-line summary for listings.")
    p.add_argument("--details", help="Rich free-text notes (multi-line ok). Placeholder inserted if omitted.")
    p.add_argument("--sections", help="Comma-separated sections touched (e.g. ingredients,claims).")
    p.add_argument("--actor-name", default="claude-code", help="Actor name (default: claude-code).")
    p.add_argument("--actor-type", default="ai_agent", choices=ACTOR_TYPES, help="Actor category (default: ai_agent).")
    p.add_argument("--model", help="Model id for AI-assisted curation (e.g. claude-opus-5-5). Required for ai_agent actors.")
    p.add_argument("--agent-tool", help="Agent tool/client (e.g. claude-code, codex).")
    p.add_argument("--agent-version", help="Agent tool/client version.")
    p.add_argument("--issue", action="append", default=[], help="Issue number or URL (repeatable).")
    p.add_argument("--pr", action="append", default=[], help="PR number or URL (repeatable).")
    p.add_argument("--url", action="append", default=[], help="Other relevant URL (repeatable).")
    p.add_argument("--force", action="store_true", help="Overwrite if the target file already exists.")
    return p.parse_args(argv)


HISTORY_DIR = Path("history")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "history.yaml"


def check_record(path: Path) -> list[str]:
    """Problems with one committed history record (empty when it is valid)."""
    from linkml.validator import validate

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [f"not valid YAML: {exc}"]
    problems = [r.message for r in validate(data, str(SCHEMA_PATH), "HistoryRecord").results]
    if problems:
        return problems
    session = data["session"]
    if path.stem != session["id"]:
        problems.append(f"file name must match session id {session['id']}")
    for actor in session["actors"]:
        if actor["type"] == "ai_agent" and not actor.get("model"):
            problems.append(f"ai_agent actor {actor['name']!r} must record its model")
    target = data["target"]
    if not Path(target["path"]).exists():
        moved = (target.get("superseded_by") or {}).get("path")
        if not (moved and Path(moved).exists()):
            problems.append(f"target {target['path']} does not exist and has no superseded_by")
    return problems


def records_for(target_path: str, history_dir: Path = HISTORY_DIR) -> list[Path]:
    """Committed history records whose target is ``target_path``."""
    out = []
    for path in sorted(history_dir.rglob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            continue
        target = data.get("target") or {}
        moved = (target.get("superseded_by") or {}).get("path")
        if target_path in (target.get("path"), moved):
            out.append(path)
    return out


def validate_main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(HISTORY_DIR.rglob("*.yaml"))
    failed = 0
    for path in paths:
        for problem in check_record(path):
            failed += 1
            print(f"ERROR   {path}: {problem}")
    print(f"{len(paths)} history record(s), {failed} problem(s)")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["validate"]:
        return validate_main(argv[1:])
    if argv[:1] == ["new"]:
        argv = argv[1:]
    args = parse_args(argv)
    record, out_path = build_record(args)

    if out_path.exists() and not args.force:
        sys.exit(f"error: {out_path} already exists (use --force to overwrite)")

    warning = target_missing_warning(record["target"]["path"])
    if warning:
        print(warning, file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        yaml.dump(record, fh, sort_keys=False, default_flow_style=False, allow_unicode=True)

    print(f"Wrote history record: {out_path}", file=sys.stderr)
    print(f"Next: edit the 'details', then run: just validate-history {out_path}", file=sys.stderr)
    print(out_path)  # machine-capturable final stdout line
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
