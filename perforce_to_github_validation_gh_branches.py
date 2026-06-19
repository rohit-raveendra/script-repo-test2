"""perforce_to_github_validation_gh_branches.py — Read all branch names from the already-cloned GitHub repository.

STEP 2 of 3 in the P4→GitHub branch validation pipeline.

Reads the bare-mirror clone produced by ``perforce_to_github_validation_gh_clone`` using
``git branch -r`` (remote-tracking refs) and returns the list of branch names
present in the GitHub repository.  No GitHub API calls are made — all data
comes from the local clone.

The clone is expected at ``{output_dir}/{migration_job_id}/`` — the same path that
``perforce_to_github_validation_gh_clone`` writes to.  Both scripts derive this path from the same
formula so no path needs to be passed between steps.

No placeholders are declared here — all inputs come from the preceding
``perforce_to_github_validation_inputs`` step via ``{{steps.perforce_to_github_validation_inputs.<key>}}``.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_gh_branches.<key>}}:
  status, github_branches, total_branches, github_data_path, migration_job_id
"""
import json
import os
import subprocess
import sys
from typing import List

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ───────────────────────────────────────────────────────────────────────────

_LOG_FILE = f"/tmp/perforce_to_github_validation_gh_branches_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_gh_branches] {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clone_dir() -> str:
    """Derive the bare-clone path — mirrors the formula in perforce_to_github_validation_gh_clone."""
    return os.path.join(OUTPUT_DIR, MIGRATION_JOB_ID)


def _read_branches(clone_path: str) -> List[str]:
    """Return all branch names from the bare clone via ``git for-each-ref``."""
    result = subprocess.run(
        [
            "git", "--git-dir", clone_path,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/",
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
        _log("refs/heads/ returned no results — falling back to git show-ref")
        result2 = subprocess.run(
            ["git", "--git-dir", clone_path, "show-ref", "--heads"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for line in result2.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split()
            if len(parts) == 2:
                ref = parts[1]
                if ref.startswith("refs/heads/"):
                    branches.append(ref[len("refs/heads/"):])

    return branches


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

    _log("Reading branches from local clone …")
    branches = _read_branches(clone_path)
    _log(f"Found {len(branches)} branch(es): {', '.join(branches) or '(none)'}")

    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_github_branches.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(branches, fh, indent=2)
    _log(f"Saved → {out_path} ({os.path.getsize(out_path)} bytes)")

    print(json.dumps({
        "status":            "completed",
        "github_branches":   branches,
        "total_branches":    len(branches),
        "github_data_path":  out_path,
        "migration_job_id":  MIGRATION_JOB_ID,
    }))


main()
