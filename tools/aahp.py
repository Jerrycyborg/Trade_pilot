#!/usr/bin/env python3
"""Small AAHP helper for manifest validation and handoff hygiene."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDOFF_DIR = ROOT / ".ai" / "handoff"
MANIFEST_PATH = HANDOFF_DIR / "manifest.json"


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text())


def _resolve_context_paths(manifest: dict[str, object]) -> list[Path]:
    raw_paths = manifest.get("context_files", [])
    if not isinstance(raw_paths, list):
        raise SystemExit("manifest.json: context_files must be a list")
    return [ROOT / str(item) for item in raw_paths]


def _iter_manifest_files(manifest: dict[str, object]) -> list[Path]:
    files: list[Path] = []
    for path in _resolve_context_paths(manifest):
        if path.is_file():
            if not _should_skip(path):
                files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.is_file() and not _should_skip(p)))
        else:
            raise SystemExit(f"manifest.json references missing path: {path.relative_to(ROOT)}")
    return sorted(set(files))


def _should_skip(path: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(ROOT).parts)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest() -> int:
    manifest = _load_manifest()
    resolved = _resolve_context_paths(manifest)
    missing = [path.relative_to(ROOT).as_posix() for path in resolved if not path.exists()]
    if missing:
        print(json.dumps({"ok": False, "missing": missing}, indent=2))
        return 1

    files = [path.relative_to(ROOT).as_posix() for path in _iter_manifest_files(manifest)]
    print(
        json.dumps(
            {
                "ok": True,
                "context_entry_count": len(resolved),
                "expanded_file_count": len(files),
                "checksum_file": manifest.get("checksum_file"),
            },
            indent=2,
        )
    )
    return 0


def generate_checksums() -> int:
    manifest = _load_manifest()
    files = _iter_manifest_files(manifest)
    checksum_rel = manifest.get("checksum_file")
    if not isinstance(checksum_rel, str):
        raise SystemExit("manifest.json: checksum_file must be set")
    checksum_path = ROOT / checksum_rel
    checksum_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": {path.relative_to(ROOT).as_posix(): _sha256(path) for path in files},
    }
    checksum_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "output": checksum_path.relative_to(ROOT).as_posix(),
                "file_count": len(files),
            },
            indent=2,
        )
    )
    return 0


def create_task_brief(task_id: str, title: str) -> int:
    manifest = _load_manifest()
    template_rel = manifest.get("task_brief_template")
    if not isinstance(template_rel, str):
        raise SystemExit("manifest.json: task_brief_template must be set")
    template_path = ROOT / template_rel
    content = template_path.read_text().replace("TASK-XXX Title", f"{task_id} {title}")
    target = HANDOFF_DIR / "task_briefs" / f"{task_id}.md"
    if target.exists():
        raise SystemExit(f"task brief already exists: {target.relative_to(ROOT)}")
    target.write_text(content)
    print(json.dumps({"ok": True, "output": target.relative_to(ROOT).as_posix()}, indent=2))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: aahp.py <validate-manifest|generate-checksums|create-task-brief>")
        return 1

    command = argv[1]
    if command == "validate-manifest":
        return validate_manifest()
    if command == "generate-checksums":
        return generate_checksums()
    if command == "create-task-brief":
        if len(argv) < 4:
            print("usage: aahp.py create-task-brief <task-id> <title>")
            return 1
        return create_task_brief(argv[2], " ".join(argv[3:]))

    print(f"unknown command: {command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
