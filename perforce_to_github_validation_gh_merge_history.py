"""perforce_to_github_validation_gh_merge_history.py — Extract merge commits from the local bare clone and map them to P4 CL numbers.

STEP 2 of 3 in the P4→GitHub integration/merge-history validation pipeline.
Reads the bare-mirror clone produced by ``perforce_to_github_validation_gh_clone`` via ``git log
--merges``, iterates over every branch present in the repository, and
extracts the Perforce CL number from each merge commit's message (using the
same p4-fusion trailer pattern as ``perforce_to_github_validation_gh_commits``).

A merge commit in GitHub corresponds to a ``p4 integrate`` operation in
Perforce — the CL number embedded in the merge commit message is the key
used to match against the P4 integration records fetched by
``perforce_to_github_validation_p4_integrations``.

If the depot type reported by ``perforce_to_github_validation_depotype`` is
``local``, this step is skipped — local depots produce no merge commits
from P4 integrations.  The result is recorded as MATCH.

No GitHub API calls are made — all data comes from the local clone.

No placeholders are declared here — all inputs come from the preceding steps.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Inputs (from steps.perforce_to_github_validation_depotype):
  {{steps.perforce_to_github_validation_depotype.depot_type}} — Depot type ('stream' or 'local')

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_gh_merge_history.<key>}}:
  status, github_merge_history_path, migration_job_id,
  total_merge_commits, branches_processed
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
# ─── Inputs from step: perforce_to_github_validation_depotype ───────────────────────────────────────
DEPOT_TYPE       = {{steps.perforce_to_github_validation_depotype.depot_type}}
# ───────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Regex patterns — extract Perforce CL number from a commit message
# (identical to perforce_to_github_validation_gh_commits so matching logic is consistent)
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
_GIT_FORMAT  = f"%H{chr(31)}%P{chr(31)}%an{chr(31)}%aI{chr(31)}%s{chr(31)}%b{_RECORD_SEP}"

_LOG_FILE = f"{OUTPUT_DIR}/perforce_to_github_validation_gh_merge_history_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_gh_merge_history] {msg}\n"
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


def _read_merge_commits_for_branch(clone_path: str, branch: str) -> List[Dict[str, Any]]:
    """Return only merge commits reachable from *branch* (commits with ≥2 parents).

    ``--merges`` restricts git log to commits that have two or more parent
    commits — these are the GitHub-side representation of ``p4 integrate``
    operations.  Each record carries:
      - ``parent_ids``   — list of parent SHAs (first = main-line, rest = merged heads)
      - ``cl_number``    — P4 CL extracted from the p4-fusion trailer, if present
      - ``target_branch``— the branch on which this merge was found
    """
    git_cmd = [
        "git", "--git-dir", clone_path,
        "log", "--merges",
        f"--format={_GIT_FORMAT}",
        branch,
    ]
    _log(f"Reading merge commits for branch '{branch}' …")
    result = subprocess.run(
        git_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        _log(
            f"WARN git log --merges failed for branch '{branch}': "
            f"{result.stderr.decode('utf-8', errors='replace').strip()[:200]}"
        )
        return []

    raw     = result.stdout.decode("utf-8", errors="replace")
    records = [r for r in raw.split(_RECORD_SEP) if r.strip()]

    commits: List[Dict[str, Any]] = []
    for rec in records:
        parts = rec.strip().split(chr(31))
        parts += [""] * (6 - len(parts))
        sha, parents_raw, author, date, subject, body = parts[:6]

        sha         = sha.strip()
        parent_ids  = [p.strip() for p in parents_raw.strip().split() if p.strip()]
        author      = author.strip()
        date        = date.strip()
        subject     = subject.strip()[:255]
        body        = body.strip()

        if not sha:
            continue

        full_message = f"{subject}\n\n{body}".strip() if body else subject
        cl_num       = _extract_cl(full_message)

        commits.append({
            "target_id":            sha,
            "target_short_id":      sha[:7],
            "target_title":         subject,
            "target_description":   full_message,
            "target_url":           "",
            "target_type":          "merge_commit",
            "target_branch":        branch,
            "target_author":        author or None,
            "target_authored_date": date or None,
            "parent_ids":           parent_ids,
            "parent_count":         len(parent_ids),
            "cl_number":            cl_num,
        })

    return commits


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    clone_path = _clone_dir()
    _log(f"Starting — clone={clone_path} | depot_type={DEPOT_TYPE} | out={OUTPUT_DIR}")

    # ── Local-depot short-circuit ───────────────────────────────────────────
    # Local depots have no P4 stream integrations, so p4-fusion produces no
    # merge commits in GitHub from branch integrations.  There is nothing to
    # extract — this step is not applicable and is recorded as MATCH.
    if str(DEPOT_TYPE).lower() == "local":
        evidence = (
            "Depot type is 'local'. Local depots do not have P4 stream integrations — "
            "p4-fusion migrates the entire depot as a linear history to a single branch "
            "with no branch-to-branch merge operations. "
            "There are no GitHub merge commits originating from P4 integrations to extract, "
            "so this validation step is not applicable and is recorded as MATCH."
        )
        _log(f"Local depot — skipping merge-history extraction. {evidence}")

        out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_github_merge_history.json")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        _log(f"Saved empty merge history → {out_path}")

        print(json.dumps({
            "status":                    "completed",
            "github_merge_history_path": out_path,
            "repo_id":                   MIGRATION_JOB_ID,
            "depot_type":                "local",
            "total_merge_commits":       0,
            "merge_commits_with_cl":     0,
            "merge_commits_without_cl":  0,
            "branches_processed":        [],
            "overall_status":            "MATCH",
            "evidence_message":          evidence,
        }))
        return
    # ── End local-depot short-circuit ──────────────────────────────────────

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

    # Collect merge commits across all branches.
    # Deduplicate by SHA — a merge commit reachable from multiple branches
    # is kept once, attributed to the first (alphabetical) branch.
    seen_shas: set = set()
    all_merges: List[Dict[str, Any]] = []

    for branch in sorted(branches):
        branch_merges = _read_merge_commits_for_branch(clone_path, branch)
        new_count = 0
        for commit in branch_merges:
            sha = commit["target_id"]
            if sha not in seen_shas:
                seen_shas.add(sha)
                all_merges.append(commit)
                new_count += 1
        _log(
            f"  branch '{branch}': {len(branch_merges)} merge commits total, "
            f"{new_count} new (deduplicated)"
        )

    # Partition: merges with CL reference vs. without
    with_cl    = [m for m in all_merges if m.get("cl_number")]
    without_cl = [m for m in all_merges if not m.get("cl_number")]
    _log(
        f"Total unique merge commits: {len(all_merges)} "
        f"({len(with_cl)} with CL reference, {len(without_cl)} without)"
    )

    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_github_merge_history.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(all_merges, fh, indent=2, default=str)
    _log(f"Saved → {out_path} ({os.path.getsize(out_path)} bytes)")

    print(json.dumps({
        "status":                    "completed",
        "github_merge_history_path": out_path,
        "repo_id":                   MIGRATION_JOB_ID,
        "total_merge_commits":       len(all_merges),
        "merge_commits_with_cl":     len(with_cl),
        "merge_commits_without_cl":  len(without_cl),
        "branches_processed":        branches,
    }))


main()
