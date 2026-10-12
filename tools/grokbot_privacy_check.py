#!/usr/bin/env python3
"""Refuse to publish a personalised profile to a public fork.

/setup writes your name, contact details and work history into TRACKED files
(CLAUDE.md, .claude/skills/job-application-assistant/01-*.md, cv/main_example.tex ...).
A GitHub fork is always public, so pushing those files there leaks them. This
check exits 1 when the working tree looks personalised AND the push target is
public (or cannot be shown to be private).

Usage:
  python3 tools/grokbot_privacy_check.py                 # check, default remote "origin"
  python3 tools/grokbot_privacy_check.py --remote myprivate
  python3 tools/grokbot_privacy_check.py --install-hook  # run automatically before every git push

Stdlib only. Uses `gh repo view` when gh is available to read visibility.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files /setup personalises, each with a placeholder that a personalised copy no longer has.
TEMPLATE_MARKERS = {
    "CLAUDE.md": "[YOUR_NAME]",
    ".claude/skills/job-application-assistant/01-candidate-profile.md": "[",
}


def git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout.strip()


def personalised() -> list[str]:
    hits = []
    claude = ROOT / "CLAUDE.md"
    if claude.is_file() and "[YOUR_NAME]" not in claude.read_text(encoding="utf-8"):
        hits.append("CLAUDE.md no longer contains the [YOUR_NAME] placeholder")
    prof = ROOT / ".claude/skills/job-application-assistant/01-candidate-profile.md"
    if prof.is_file() and not re.search(r"\[[A-Z][A-Z_]{3,}\]", prof.read_text(encoding="utf-8")):
        hits.append("01-candidate-profile.md has no [PLACEHOLDER] tokens left")
    return hits


def visibility(remote: str) -> tuple[str, str]:
    rc, url = git("remote", "get-url", remote)
    if rc != 0:
        return "none", ""
    m = re.search(r"github\.com[:/]+([^/]+/[^/.]+?)(?:\.git)?$", url)
    if not m:
        return "unknown", url
    try:
        p = subprocess.run(["gh", "repo", "view", m.group(1), "--json", "visibility"],
                           capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            return json.loads(p.stdout)["visibility"].lower(), m.group(1)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        pass
    return "unknown", m.group(1)


def install_hook() -> int:
    rc, gitdir = git("rev-parse", "--git-path", "hooks")
    hooks = (ROOT / gitdir) if not Path(gitdir).is_absolute() else Path(gitdir)
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    # pre-push receives the remote name as $1
    hook.write_text('#!/bin/sh\nexec python3 "$(git rev-parse --show-toplevel)/tools/grokbot_privacy_check.py" --remote "$1"\n')
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    print(f"installed {hook}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--install-hook", action="store_true")
    args = ap.parse_args()
    if args.install_hook:
        return install_hook()

    flags = personalised()
    vis, repo = visibility(args.remote)
    if not flags:
        print(f"ok: profile files still look like the template (remote {args.remote}: {vis} {repo})")
        return 0
    if vis == "private":
        print(f"ok: personalised profile, but {repo} is private")
        return 0
    print(f"BLOCKED: this tree looks personalised ({'; '.join(flags)}) and remote "
          f"'{args.remote}' ({repo or 'unknown'}) is {vis}, not private.\n"
          "Push to a private repository instead (see GROKBOT.md > Privacy), or keep the commits local.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
