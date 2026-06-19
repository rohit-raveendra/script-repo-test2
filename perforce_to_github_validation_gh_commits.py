"""perforce_to_github_validation_gh_commits.py — Read commits from every branch in the local bare-clone and extract embedded P4 CL numbers.

STEP 4 of 5 in the P4→GitHub changelist comparison pipeline.
Reads the bare-mirror clone produced by ``perforce_to_github_validation_gh_clone`` via ``git log``,

iterates over every branch present in the repository, extracts the Perforce
CL number from each commit message, and writes the result to
``{output_dir}/{migration_job_id}_github_commits.json`` for the comparison step.

No specific branch is targeted — all branches in the cloned repository are
processed automatically so the comparison step can match P4 changelists
against the correct branch without any manual branch input.

No GitHub API calls are made — all data comes from the local clone.

No placeholders are declared here — all inputs come from the preceding
``perforce_to_github_validation_inputs`` step via ``{{steps.perforce_to_github_validation_inputs.<key>}}``.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_gh_commits.<key>}}:
  status, github_data_path, migration_job_id, total_commits, branches_processed
"""
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ───────────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# Regex patterns — extract Perforce CL number from a commit message
# ---------------------------------------------------------------------------

# p4-fusion standard trailer:  [p4-fusion: depot-paths = "...": change = 12345]
_P4_FUSION_RE = re.compile(r'\[p4-fusion:.*?change\s*=\s*(\d+)', re.DOTALL)

# Generic fallback: "Change: 12345" / "cl: 12345" / "Changelist #12345"
_FALLBACK_CL_RE = re.compile(
    r'(?:^|\b)(?:changelist|change|cl|p4change)\s*[:#=\s]+(\d{4,})',
    re.IGNORECASE | re.MULTILINE,
)

# git log record / field separators
_RECORD_SEP = "\x1e"
_GIT_FORMAT = f"%H{chr(31)}%an{chr(31)}%aI{chr(31)}%s{chr(31)}%b{_RECORD_SEP}"

_LOG_FILE = f"/tmp/perforce_to_github_validation_gh_commits_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_gh_commits] {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_cl(message: str) -> Optional[str]:
    m = _P4_FUSION_RE.search(message)
    if m:
        return m.group(1)
    m = _FALLBACK_CL_RE.search(message)
    if m:
        return m.group(1)
    return None


def _clone_dir() -> str:
    """Derive the bare-clone path — mirrors the formula in perforce_to_github_validation_gh_clone."""
    return os.path.join(OUTPUT_DIR, MIGRATION_JOB_ID)


def _list_branches(clone_path: str) -> List[str]:
    """Return all branch names present in the bare clone."""
    result = subprocess.run(
        [
            "git", "--git-dir", clone_path,
            "for-each-ref", "--format=%(refname:short)", "refs/heads/",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    branches = [
        line.strip()
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if not branches:
        # Fallback for older git versions
        result2 = subprocess.run(
            ["git", "--git-dir", clone_path, "show-ref", "--heads"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for line in result2.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                branches.append(parts[1][len("refs/heads/"):])
    return branches


def _read_commits_for_branch(clone_path: str, branch: str) -> List[Dict[str, Any]]:
    """Return ALL commits reachable from *branch* via a simple ``git log <branch>``.

    Each commit is tagged with ``target_branch`` so the comparison step can
    group results by branch.  Deduplication across branches is handled by the
    caller using the commit SHA.
    """
    git_cmd = [
        "git", "--git-dir", clone_path,
        "log", f"--format={_GIT_FORMAT}",
        branch,
    ]
    _log(f"Reading commits for branch '{branch}' …")
    result = subprocess.run(
        git_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        _log(f"WARN git log failed for branch '{branch}': {result.stderr.decode('utf-8', errors='replace').strip()[:200]}")
        return []

    raw     = result.stdout.decode("utf-8", errors="replace")
    records = [r for r in raw.split(_RECORD_SEP) if r.strip()]

    commits: List[Dict[str, Any]] = []
    for rec in records:
        parts = rec.strip().split(chr(31))
        parts += [""] * (5 - len(parts))
        sha, author, date, subject, body = parts[:5]

        sha     = sha.strip()
        author  = author.strip()
        date    = date.strip()
        subject = subject.strip()[:255]
        body    = body.strip()

        if not sha:
            continue

        full_message = f"{subject}\n\n{body}".strip() if body else subject

        commits.append({
            "target_id":            sha,
            "target_short_id":      sha[:7],
            "target_title":         subject,
            "target_description":   full_message,
            "target_url":           "",
            "target_type":          "commit",
            "target_branch":        branch,
            "target_author":        author or None,
            "target_authored_date": date or None,
            "cl_number":            _extract_cl(full_message),
        })

    return commits


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    clone_path = _clone_dir()
    _log(f"Starting — clone={clone_path} | out={OUTPUT_DIR}")

    if not os.path.isdir(clone_path):
        raise RuntimeError(
            f"Clone folder not found: {clone_path} "
            "— ensure perforce_to_github_validation_gh_clone completed successfully."
        )

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    branches = _list_branches(clone_path)
    if not branches:
        raise RuntimeError(
            f"No branches found in clone at {clone_path!r}. "
            "The repository may be empty or the clone may be incomplete."
        )
    _log(f"Branches found ({len(branches)}): {', '.join(branches)}")

    # Collect commits across all branches.
    # Deduplicate by SHA — a commit reachable from multiple branches is kept
    # once, attributed to the first branch that surfaces it (alphabetical order
    # of branch names ensures deterministic attribution).
    seen_shas: set = set()
    all_commits: List[Dict[str, Any]] = []
    for branch in sorted(branches):
        branch_commits = _read_commits_for_branch(clone_path, branch)
        new_count = 0
        for commit in branch_commits:
            sha = commit["target_id"]
            if sha not in seen_shas:
                seen_shas.add(sha)
                all_commits.append(commit)
                new_count += 1
        _log(
            f"  branch '{branch}': {len(branch_commits)} commits total, "
            f"{new_count} new (deduplicated)"
        )

    _log(f"Total unique commits across all branches: {len(all_commits)}")

    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_github_commits.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(all_commits, fh, indent=2, default=str)
    _log(f"Saved → {out_path} ({os.path.getsize(out_path)} bytes)")

    print(json.dumps({
        "status":             "completed",
        "github_data_path":   out_path,
        "migration_job_id":   MIGRATION_JOB_ID,
        "total_commits":      len(all_commits),
        "branches_processed": branches,
    }))


main()
