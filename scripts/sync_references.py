#!/usr/bin/env python3
"""Package shared references inside each independently installable skill."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("resume-review", "resume-edit", "job-apply", "mock-interview")
COMMON = ("candidate-context.md", "browser-session.md", "platforms.md")


def sync(check=False):
    errors = []
    for name in SKILLS:
        files = COMMON + (("ats.md",) if name in ("resume-review", "resume-edit") else ())
        for filename in files:
            source = ROOT / "shared" / filename
            target = ROOT / "skills" / name / "references" / filename
            data = source.read_bytes()
            if check:
                if not target.exists() or target.read_bytes() != data:
                    errors.append(str(target.relative_to(ROOT)))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    errors = sync(args.check)
    if errors:
        parser.exit(1, "Out-of-date packaged references:\n" + "\n".join(errors) + "\n")
    print("Shared references are consistent." if args.check else "Shared references packaged.")
