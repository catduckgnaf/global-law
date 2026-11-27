#!/usr/bin/env python3
"""Weekly change watch for the United States corpora in lexifina/global-law.

global-law pins every body of law as a git submodule and moves each pin forward as the corpus
changes. This script snapshots the pins (United States and Puerto Rico only), diffs them against the
last snapshot, asks GitHub what each moved pin contains, and writes a Markdown report.

  us_law_watch.py snapshot  --clone DIR --out FILE          write a snapshot of DIR (a global-law clone)
  us_law_watch.py report    --clone DIR --state FILE --report FILE [--config FILE] [--offline]
                            diff DIR against the snapshot in FILE, write the report, update FILE

Standard library only. GitHub access uses GH_TOKEN or GITHUB_TOKEN, else `gh auth token`.
Exit status: 0 on success, 1 on usage or data errors. A report with changes is not an error.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCOPE = ("united-states/", "puerto-rico/")
UPSTREAM = "lexifina/global-law"
MAX_LISTED = 12  # commit subjects listed per statute corpus


def git(clone: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, encoding="utf-8", errors="replace")
    if done.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def in_scope(path: str) -> bool:
    return path.startswith(SCOPE)


def is_caselaw(path: str) -> bool:
    return path.endswith("/caselaw")


def take_snapshot(clone: Path) -> dict:
    """Pins and metadata for every in-scope corpus, read from the clone's HEAD (no working tree needed)."""
    head = git(clone, "rev-parse", "HEAD").strip()
    pins: dict[str, str] = {}
    for line in git(clone, "ls-tree", "-r", "HEAD", "united-states", "puerto-rico").splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if mode == "160000" and in_scope(path):
            pins[path] = sha
    urls: dict[str, str] = {}
    for line in git(clone, "config", "--blob", "HEAD:.gitmodules", "--get-regexp", r"^submodule\..*\.url$").splitlines():
        key, url = line.split(" ", 1)
        path = key[len("submodule."):-len(".url")]
        if path in pins:
            urls[path] = url.removesuffix(".git").removeprefix("https://github.com/")
    manifest = json.loads(git(clone, "show", "HEAD:manifest.json"))
    meta = {c["path"]: c for c in manifest["corpora"] if c["path"] in pins}
    fresh: dict[str, dict] = {}
    rows = git(clone, "show", "HEAD:data/freshness.tsv").splitlines()
    for row in csv.DictReader(rows, delimiter="\t"):
        if row["path"] in pins:
            fresh[row["path"]] = row
    corpora = {}
    for path, sha in sorted(pins.items()):
        m, f = meta.get(path, {}), fresh.get(path, {})
        corpora[path] = {
            "pin": sha,
            "repo": urls.get(path, ""),
            "history": m.get("history", ""),
            "coverage": (m.get("coverage") or "")[:300],
            "status": f.get("status", ""),
            "last_verified": f.get("last_verified", ""),
        }
    def tree(spec: str) -> str:
        done = subprocess.run(["git", "-C", str(clone), "rev-parse", spec], capture_output=True, encoding="utf-8")
        return done.stdout.strip() if done.returncode == 0 else ""
    return {
        "upstream_commit": head,
        "upstream_date": git(clone, "log", "-1", "--format=%cI", "HEAD").strip(),
        "tools_tree": tree("HEAD:tools"),
        "judgments_us_tree": tree("HEAD:judgments/us"),
        "corpora": corpora,
    }


def diff_snapshots(old: dict, new: dict) -> dict:
    oc, nc = old.get("corpora", {}), new["corpora"]
    out = {
        "added": sorted(set(nc) - set(oc)),
        "removed": sorted(set(oc) - set(nc)),
        "advanced": sorted(p for p in nc if p in oc and nc[p]["pin"] != oc[p]["pin"]),
        "status_changes": [],
        "tools_changed": bool(old.get("tools_tree")) and old.get("tools_tree") != new.get("tools_tree"),
        "judgments_changed": bool(old.get("judgments_us_tree")) and old.get("judgments_us_tree") != new.get("judgments_us_tree"),
    }
    for p in sorted(set(nc) & set(oc)):
        if oc[p]["status"] != nc[p]["status"]:
            out["status_changes"].append((p, oc[p]["status"], nc[p]["status"]))
    return out


# ---- GitHub -----------------------------------------------------------------------------------------

_token: str | None = None


def token() -> str:
    global _token
    if _token is None:
        _token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not _token:
            done = subprocess.run(["gh", "auth", "token"], capture_output=True, encoding="utf-8")
            _token = done.stdout.strip() if done.returncode == 0 else ""
    return _token


def compare(repo: str, old: str, new: str) -> dict:
    """GitHub compare API for one moved pin. Returns {error: ...} instead of raising."""
    url = f"https://api.github.com/repos/{repo}/compare/{old}...{new}?per_page=100"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if token():
        req.add_header("Authorization", f"Bearer {token()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} from the compare API"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    commits = [(c["commit"]["message"].splitlines() or [""])[0] for c in data.get("commits", [])]
    return {
        "status": data.get("status", ""),
        "total": data.get("total_commits", len(commits)),
        "commits": commits,
        "files": len(data.get("files", [])),
        "files_capped": len(data.get("files", [])) >= 300,
    }


# ---- report -----------------------------------------------------------------------------------------

def short(sha: str) -> str:
    return sha[:8]


def render(old: dict, new: dict, delta: dict, details: dict[str, dict], priority: list[str], today: str) -> str:
    nc, oc = new["corpora"], old.get("corpora", {})
    L: list[str] = []
    L.append(f"# U.S. law update report, {today}")
    L.append("")
    L.append(f"Upstream `{UPSTREAM}` moved `{short(old.get('upstream_commit', '') or '0')}` to `{short(new['upstream_commit'])}` "
             f"(United States and Puerto Rico only; {len(nc)} corpora tracked).")
    L.append("")
    adv = delta["advanced"]
    L.append(f"- Corpora with new commits: **{len(adv)}**")
    L.append(f"- Corpora added: **{len(delta['added'])}**, removed: **{len(delta['removed'])}**")
    L.append(f"- Freshness status changes: **{len(delta['status_changes'])}**")
    if delta["tools_changed"]:
        L.append("- **The global-law `tools/` directory changed.** Re-run the local tests before relying on the MCP server.")
    if delta["judgments_changed"]:
        L.append("- `judgments/us/` changed (judgments indexed against provisions).")
    L.append("")

    def key(p: str):
        return (0 if p in priority else 1, is_caselaw(p), p)

    statute = [p for p in sorted(adv, key=key) if not is_caselaw(p)]
    cases = [p for p in sorted(adv, key=key) if is_caselaw(p)]

    if statute:
        L.append("## Statutes, regulations and guidance")
        L.append("")
        for p in statute:
            d = details.get(p, {})
            star = " (priority)" if p in priority else ""
            L.append(f"### `{p}`{star}")
            L.append("")
            L.append(f"`{short(oc[p]['pin'])}` to `{short(nc[p]['pin'])}` · repo `{nc[p]['repo']}` · {nc[p]['history']} · "
                     f"freshness {nc[p]['status'] or 'unknown'} (last verified {nc[p]['last_verified'] or 'never'})")
            L.append("")
            if "error" in d:
                L.append(f"Could not read the change list: {d['error']}. Compare by hand: "
                         f"https://github.com/{nc[p]['repo']}/compare/{oc[p]['pin']}...{nc[p]['pin']}")
            elif d:
                extra = ""
                if d["status"] not in ("ahead", "identical"):
                    extra = f" Compare status is **{d['status']}**: the pin did not move strictly forward, so history may have been rewritten. Check before relying on it."
                capped = "+" if d["files_capped"] else ""
                L.append(f"{d['total']} new commit(s), {d['files']}{capped} file(s) touched.{extra}")
                L.append("")
                for s in d["commits"][:MAX_LISTED]:
                    L.append(f"- {s}")
                if d["total"] > MAX_LISTED:
                    L.append(f"- … and {d['total'] - MAX_LISTED} more")
            L.append("")

    if cases:
        L.append("## Case law (one commit per decision)")
        L.append("")
        L.append("| Corpus | Commits | From | To | Freshness |")
        L.append("| --- | ---: | --- | --- | --- |")
        for p in cases:
            d = details.get(p, {})
            n = d.get("total", "?") if "error" not in d else "error"
            L.append(f"| `{p}`{' ★' if p in priority else ''} | {n} | `{short(oc[p]['pin'])}` | `{short(nc[p]['pin'])}` | {nc[p]['status'] or 'unknown'} |")
        L.append("")

    if delta["added"]:
        L.append("## New corpora")
        L.append("")
        for p in delta["added"]:
            L.append(f"- `{p}` · {nc[p]['history']} · {nc[p]['coverage'] or 'no coverage statement'}")
        L.append("")
    if delta["removed"]:
        L.append("## Corpora removed upstream")
        L.append("")
        for p in delta["removed"]:
            L.append(f"- `{p}`")
        L.append("")

    if delta["status_changes"]:
        pri = [c for c in delta["status_changes"] if c[0] in priority]
        L.append("## Freshness changes")
        L.append("")
        if pri:
            L.append("Priority corpora:")
            L.append("")
            for p, a, b in pri:
                L.append(f"- `{p}`: {a or 'unknown'} to {b or 'unknown'}")
            L.append("")
        rest = len(delta["status_changes"]) - len(pri)
        if rest:
            L.append(f"{rest} other corpora changed freshness status; see `data/freshness.tsv` upstream.")
            L.append("")

    unverified = [p for p in priority if p not in nc or nc[p]["status"] != "current"]
    if unverified:
        L.append("## Priority corpora not currently verified against their official source")
        L.append("")
        for p in unverified:
            if p not in nc:
                L.append(f"- `{p}`: not pinned upstream")
            else:
                L.append(f"- `{p}`: {nc[p]['status'] or 'unknown'}, last verified {nc[p]['last_verified'] or 'never'}")
        L.append("")
        L.append("Treat these as a research aid and confirm against the official publication before filing or citing.")
        L.append("")

    L.append("This report tracks the upstream dataset. It does not update any local checkout or establish that the law changed.")
    L.append("")
    return "\n".join(L).rstrip() + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_report(args: argparse.Namespace) -> int:
    clone = Path(args.clone)
    new = take_snapshot(clone)
    state = Path(args.state)
    if not state.is_file():
        print(f"no snapshot at {state}; run `snapshot` once to seed it", file=sys.stderr)
        return 1
    old = json.loads(state.read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    priority = config.get("priority", [])
    delta = diff_snapshots(old, new)
    details: dict[str, dict] = {}
    if not args.offline:
        for p in delta["advanced"]:
            details[p] = compare(new["corpora"][p]["repo"], old["corpora"][p]["pin"], new["corpora"][p]["pin"])
    changed = any([delta["added"], delta["removed"], delta["advanced"], delta["status_changes"],
                   delta["tools_changed"], delta["judgments_changed"]])
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    report = render(old, new, delta, details, priority, today)
    incomplete = any("error" in detail for detail in details.values())
    if incomplete:
        report += "\n**Comparison incomplete. The previous baseline was retained and will be retried on the next run.**\n"
    atomic_write(Path(args.report), report)
    if not args.keep_state and not incomplete:
        atomic_write(state, json.dumps({"_doc": "Last seen pins for the United States corpora in lexifina/global-law. Written by scripts/us_law_watch.py; do not edit.",
                                        "taken": today, **new}, indent=2, sort_keys=True) + "\n")
    out = os.environ.get("GITHUB_OUTPUT")
    # "material" excludes pure freshness churn, which flaps weekly for national law.
    material = any([delta["added"], delta["removed"], delta["advanced"], delta["tools_changed"], delta["judgments_changed"]])
    for k, v in (("changed", changed), ("material", material), ("advanced", len(delta["advanced"]))):
        line = f"{k}={str(v).lower() if isinstance(v, bool) else v}"
        print(line)
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--clone", required=True)
    s.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--clone", required=True)
    r.add_argument("--state", required=True)
    r.add_argument("--report", required=True)
    r.add_argument("--config")
    r.add_argument("--offline", action="store_true", help="skip GitHub compare calls")
    r.add_argument("--keep-state", action="store_true", help="do not overwrite the snapshot (dry run)")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snap = take_snapshot(Path(args.clone))
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        atomic_write(Path(args.out), json.dumps({"_doc": "Last seen pins for the United States corpora in lexifina/global-law. Written by scripts/us_law_watch.py; do not edit.",
                                                 "taken": today, **snap}, indent=2, sort_keys=True) + "\n")
        print(f"snapshot: {len(snap['corpora'])} corpora at {short(snap['upstream_commit'])}")
        return 0
    return run_report(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
