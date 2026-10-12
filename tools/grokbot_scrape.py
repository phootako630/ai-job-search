#!/usr/bin/env python3
"""Compact, deduplicated job search for Grok Bot (or any agent) - stdlib + bun only.

Runs the ENABLED portal CLIs under .agents/skills/ with the queries from a JSON
config (default: examples/grokbot/search_config.toronto-canada.json, or
.grokbot/search_config.json when you have your own), drops results older than
the recency window, drops title patterns you never want, de-duplicates against
job_scraper/seen_jobs.json and job_search_tracker.csv, and prints a short list
for the agent to rank.

Read-only by default. `--record` appends the new postings to seen_jobs.json with
status "new" (what /scrape Step 4 does) so the next run does not show them again.

Usage:
  python3 tools/grokbot_scrape.py                       # top 3 priority categories, markdown
  python3 tools/grokbot_scrape.py --format json
  python3 tools/grokbot_scrape.py --broad               # every category
  python3 tools/grokbot_scrape.py --focus frontend      # categories whose name matches
  python3 tools/grokbot_scrape.py --record              # also persist to seen_jobs.json
  python3 tools/grokbot_scrape.py --dry-run             # print the CLI calls, run nothing

Portals other than linkedin-search and freehire-search have no query mapping
here; they are listed under "unmapped_portals" so the agent can run them by hand
from their own SKILL.md. Disabled portals (`enabled: false`) are reported and skipped.

Exit codes: 0 ok (even with partial portal failures, listed under "errors"),
2 when every CLI call failed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import job_key  # noqa: E402
import rank_state  # noqa: E402

SKILLS = ROOT / ".agents" / "skills"
SEEN = ROOT / "job_scraper" / "seen_jobs.json"
TRACKER = ROOT / "job_search_tracker.csv"
LOCAL_CFG = ROOT / ".grokbot" / "search_config.json"
EXAMPLE_CFG = ROOT / "examples" / "grokbot" / "search_config.toronto-canada.json"
MAPPED = ("linkedin-search", "freehire-search")


def portal_enabled(skill_md: Path) -> bool:
    """`enabled` in the SKILL.md frontmatter; a missing key means enabled."""
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return True
    for line in m.group(1).splitlines():
        km = re.match(r"^enabled:\s*(\S+)", line)
        if km:
            return km.group(1).strip().lower() not in ("false", "no", "0")
    return True


def cli_cmd(portal: str) -> list[str]:
    return ["bun", "run", str(SKILLS / portal / "cli" / "src" / "cli.ts")]


def build_calls(cfg: dict, portals: list[str], categories: list[dict], days: int, limit: int):
    calls = []
    for cat in categories:
        for q in cat["queries"]:
            for loc in cfg["locations"]:
                if "linkedin-search" in portals and "linkedin" in loc:
                    l = loc["linkedin"]
                    args = ["search", "-q", q, "-l", l["location"], "--jobage", str(days),
                            "--limit", str(limit), "--format", "json"]
                    if l.get("remote"):
                        args += ["--remote", l["remote"]]
                    calls.append(("linkedin-search", cat["name"], loc["name"], q, args))
                if "freehire-search" in portals and "freehire" in loc:
                    f = {**cat.get("freehire", {}), **loc["freehire"]}
                    args = ["search", "-q", q, "--jobage", str(days), "--limit", str(limit),
                            "--no-description", "--format", "json"]
                    for flag in ("country", "city", "region", "seniority", "category", "remote"):
                        if f.get(flag):
                            args += [f"--{flag}", str(f[flag])]
                    calls.append(("freehire-search", cat["name"], loc["name"], q, args))
    return calls


def run_call(portal: str, args: list[str], timeout: int = 90):
    proc = subprocess.run(cli_cmd(portal) + args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:300] or f"exit {proc.returncode}")
    return json.loads(proc.stdout).get("results", [])


def clean_url(url: str) -> str:
    """Drop utm_* tracking params (freehire appends utm_source=freehire.me)."""
    base, sep, query = (url or "").partition("?")
    kept = [kv for kv in query.split("&") if kv and not kv.lower().startswith("utm_")]
    return base + ("?" + "&".join(kept) if kept else "")


def norm_url(url: str) -> str:
    return re.sub(r"[?#].*$", "", (url or "").strip()).rstrip("/").lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path)
    ap.add_argument("--days", type=int, help="recency window (default: config, 14)")
    ap.add_argument("--limit", type=int, help="results per CLI call (default: config, 15)")
    ap.add_argument("--broad", action="store_true", help="run every category, not just the top 3 priorities")
    ap.add_argument("--focus", help="only categories whose name contains this text")
    ap.add_argument("--portals", help="comma-separated subset of enabled mapped portals")
    ap.add_argument("--format", choices=("markdown", "json"), default="markdown")
    ap.add_argument("--max-results", type=int, default=60, help="cap on printed postings, newest first (0 = no cap)")
    ap.add_argument("--record", action="store_true", help="append new postings to seen_jobs.json (status new)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between LinkedIn calls (keep volume low)")
    ap.add_argument("--state", type=Path, default=SEEN)
    ap.add_argument("--tracker", type=Path, default=TRACKER)
    args = ap.parse_args()

    cfg_path = args.config or (LOCAL_CFG if LOCAL_CFG.is_file() else EXAMPLE_CFG)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    days = args.days or cfg.get("days", 14)
    limit = args.limit or cfg.get("limit_per_query", 15)
    exclude = re.compile(cfg["exclude_title_regex"], re.I) if cfg.get("exclude_title_regex") else None

    cats = sorted(cfg["categories"], key=lambda c: c.get("priority", 99))
    if args.focus:
        cats = [c for c in cats if args.focus.lower() in c["name"].lower()]
    elif not args.broad:
        cats = cats[:3]

    installed = sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))
    enabled = [p for p in installed if portal_enabled(SKILLS / p / "SKILL.md")]
    disabled = [p for p in installed if p not in enabled]
    mapped = [p for p in enabled if p in MAPPED]
    if args.portals:
        want = {p.strip() for p in args.portals.split(",")}
        mapped = [p for p in mapped if p in want]
    unmapped = [p for p in enabled if p not in MAPPED]

    calls = build_calls(cfg, mapped, cats, days, limit)
    if args.dry_run:
        for portal, cat, loc, q, a in calls:
            print(" ".join(["bun run", f".agents/skills/{portal}/cli/src/cli.ts"] + [repr(x) if " " in x else x for x in a]))
        print(f"# {len(calls)} calls; config={cfg_path.relative_to(ROOT) if cfg_path.is_relative_to(ROOT) else cfg_path}; "
              f"disabled={disabled}; unmapped={unmapped}", file=sys.stderr)
        return 0

    if subprocess.run(["bun", "--version"], capture_output=True).returncode != 0:
        print("bun not found: install it (https://bun.sh) or fall back to WebSearch", file=sys.stderr)
        return 2

    # --- state for dedup
    seen_doc, seen = ({"seen": {}}, {})
    if args.state.is_file():
        seen_doc, seen = rank_state.load_state(args.state)
    seen_urls = {norm_url(e.get("url", "")) for e in seen.values()}
    tracked = rank_state.tracker_pairs(args.tracker)

    cutoff = date.today() - timedelta(days=days)
    pool: dict[str, dict] = {}
    errors, ok_calls, counts = [], 0, {"raw": 0, "too_old": 0, "title_excluded": 0,
                                       "already_seen": 0, "in_tracker": 0, "dup_in_run": 0}
    for i, (portal, cat, loc, q, a) in enumerate(calls):
        try:
            results = run_call(portal, a)
            ok_calls += 1
        except Exception as exc:  # noqa: BLE001 - report and continue, per /scrape Step 1b
            errors.append({"portal": portal, "query": q, "location": loc, "error": str(exc)})
            continue
        for r in results:
            counts["raw"] += 1
            title, company, url = r.get("title") or "", r.get("company") or "", clean_url(r.get("url") or "")
            posted = (r.get("date") or None) and r["date"][:10]
            try:
                if posted and date.fromisoformat(posted[:10]) < cutoff:
                    counts["too_old"] += 1
                    continue
            except ValueError:
                posted = None
            if exclude and exclude.search(title):
                counts["title_excluded"] += 1
                continue
            key = job_key.make_key(company, title, url)
            if key in seen or norm_url(url) in seen_urls:
                counts["already_seen"] += 1
                continue
            if (rank_state.norm(company), rank_state.norm(title)) in tracked:
                counts["in_tracker"] += 1
                continue
            if key in pool:
                counts["dup_in_run"] += 1
                pool[key]["also"].append(f"{portal}:{loc}")
                continue
            pool[key] = {"key": key, "title": title, "company": company,
                         "location": r.get("location"), "posted_date": posted, "url": url,
                         "portal": portal, "category": cat, "search_location": loc,
                         "query": q, "also": [], "job_id": r.get("id")}
        if portal == "linkedin-search" and i < len(calls) - 1:
            time.sleep(args.pause)

    jobs = sorted(pool.values(), key=lambda j: j["posted_date"] or "", reverse=True)

    truncated = 0
    if args.max_results and len(jobs) > args.max_results and not args.record:
        truncated = len(jobs) - args.max_results
        jobs = jobs[: args.max_results]

    recorded = 0
    if args.record and jobs:
        today = date.today().isoformat()
        for j in jobs:
            seen[j["key"]] = {"title": j["title"], "company": j["company"], "url": j["url"],
                              "first_seen": today, "posted_date": j["posted_date"], "deadline": None,
                              "fit": None, "status": "new", "portal": j["portal"], "source": "cli"}
            recorded += 1
        seen_doc["seen"] = seen
        args.state.parent.mkdir(parents=True, exist_ok=True)
        fd = tempfile.NamedTemporaryFile("w", dir=args.state.parent, delete=False, encoding="utf-8",
                                         prefix=".seen_jobs.", suffix=".tmp")
        with fd:
            json.dump(seen_doc, fd, indent=2, ensure_ascii=False)
        Path(fd.name).replace(args.state)

    summary = {"date": date.today().isoformat(), "window_days": days, "calls": len(calls),
               "calls_ok": ok_calls, "counts": counts, "new": len(jobs), "recorded": recorded, "truncated": truncated,
               "portals_run": mapped, "skipped_disabled": disabled, "unmapped_portals": unmapped,
               "errors": errors}

    if args.format == "json":
        print(json.dumps({"summary": summary, "jobs": jobs}, indent=2, ensure_ascii=False))
    else:
        print(f"## New postings - {summary['date']} ({len(jobs)} new, last {days} days)\n")
        print(f"portals run: {', '.join(mapped) or 'none'} | skipped (disabled): {', '.join(disabled) or '-'}"
              + (f" | unmapped (run by hand): {', '.join(unmapped)}" if unmapped else ""))
        print(f"filtered: {counts['already_seen']} already seen, {counts['too_old']} too old, "
              f"{counts['title_excluded']} title-excluded, {counts['in_tracker']} in tracker, "
              f"{counts['dup_in_run']} duplicates across queries/portals ({counts['raw']} raw results)")
        if truncated:
            print(f"showing newest {len(jobs)}; {truncated} more omitted (raise --max-results or narrow with --focus)")
        for e in errors:
            print(f"health: {e['portal']} - call failed ({e['query']!r} / {e['location']}): {e['error']}")
        print("\n| # | Posted | Title | Company | Location | Portal | URL |\n|---|---|---|---|---|---|---|")
        for n, j in enumerate(jobs, 1):
            esc = lambda s: (s or "").replace("|", "/")
            print(f"| {n} | {j['posted_date'] or 'unknown'} | {esc(j['title'])} | {esc(j['company'])} | "
                  f"{esc(j['location'])} | {j['portal'].replace('-search', '')} | {j['url']} |")
        if args.record:
            print(f"\nrecorded {recorded} entries in {args.state} (status new, fit null)")

    return 2 if calls and ok_calls == 0 else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
