#!/usr/bin/env python3
"""Validate portable skill manifests, local links, metadata and packaged references."""
import ast
import json
from pathlib import Path
import re
import sys
from sync_references import ROOT, SKILLS, sync


def validate():
    errors = ["Unsynchronised reference: " + x for x in sync(check=True)]
    for name in SKILLS:
        folder = ROOT / "skills" / name
        manifest = folder / "SKILL.md"
        if not manifest.exists():
            errors.append(f"Missing manifest: {name}")
            continue
        content = manifest.read_text(encoding="utf-8")
        front = re.match(r"\A---\n(.*?)\n---\n", content, re.S)
        if not front:
            errors.append(f"Invalid frontmatter: {name}")
            continue
        fields = dict(re.findall(r"^(name|description):\s*(.+)$", front[1], re.M))
        if fields.get("name") != name or not fields.get("description"):
            errors.append(f"Invalid name/description: {name}")
        description = fields.get("description", "")
        if description.startswith('"'):
            try:
                json.loads(description)
            except ValueError:
                errors.append(f"Invalid quoted description: {name}")
        elif ": " in description or " #" in description:
            errors.append(f"Quote YAML punctuation in description: {name}")
        if not re.fullmatch(r"[a-z0-9-]{1,63}", name):
            errors.append(f"Invalid skill name: {name}")
        metadata = folder / "agents/openai.yaml"
        if not metadata.exists() or f"${name}" not in metadata.read_text(encoding="utf-8"):
            errors.append(f"Missing UI invocation: {name}")
        for path in folder.rglob("*.md"):
            body = path.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)]+)\)", body):
                if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                linked = (path.parent / target.split("#")[0]).resolve()
                if not linked.is_relative_to(folder.resolve()) or not linked.exists():
                    errors.append(f"Nonportable/missing reference in {path.relative_to(ROOT)}: {target}")
        for path in folder.rglob("*.py"):
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                errors.append(str(exc))
    return errors


if __name__ == "__main__":
    errors = validate()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)
    print("Validated 4 skills: manifests, local references, UI metadata, Python syntax, shared content.")
