"""perforce_to_github_validation_gh_clone.py — Bare-mirror clone the target GitHub repository to the shared EFS volume.

STEP 3 of 5 in the P4→GitHub changelist comparison pipeline.
Clones the repository to ``{output_dir}/{migration_job_id}/`` so the next step
(perforce_to_github_validation_gh_commits) can read commits directly from the local clone
without any further GitHub API calls.

No placeholders are declared here — all inputs come from the preceding
``perforce_to_github_validation_inputs`` step via ``{{steps.perforce_to_github_validation_inputs.<key>}}``.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.target_token}}      — GitHub personal access token
  {{steps.perforce_to_github_validation_inputs.target_url}}        — GitHub server URL (e.g. "https://github.com")
  {{steps.perforce_to_github_validation_inputs.target_org}}        — Target GitHub organisation
  {{steps.perforce_to_github_validation_inputs.target_repo}}       — Target GitHub repository name
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_gh_clone.<key>}}:
  status, migration_job_id
"""
import json
import os
import subprocess
import sys

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
TARGET_TOKEN     = {{steps.perforce_to_github_validation_inputs.target_token}}
TARGET_URL       = {{steps.perforce_to_github_validation_inputs.target_url}}
TARGET_ORG       = {{steps.perforce_to_github_validation_inputs.target_org}}
TARGET_REPO      = {{steps.perforce_to_github_validation_inputs.target_repo}}
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ───────────────────────────────────────────────────────────────────────────

_LOG_FILE = f"{OUTPUT_DIR}/perforce_to_github_validation_gh_clone_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_gh_clone] {msg}\n"
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
    """Return the bare-clone destination path: {OUTPUT_DIR}/{MIGRATION_JOB_ID}

    Both perforce_to_github_validation_gh_clone and perforce_to_github_validation_gh_commits derive this path from the same
    formula so they always agree without passing the path between steps.
    """
    return os.path.join(OUTPUT_DIR, MIGRATION_JOB_ID)


def _build_clone_url() -> str:
    bare = TARGET_URL.replace("https://", "").replace("http://", "")
    return f"https://x-access-token:{TARGET_TOKEN}@{bare}/{TARGET_ORG}/{TARGET_REPO}.git"


# ---------------------------------------------------------------------------
# Core clone
# ---------------------------------------------------------------------------

def clone_repo(clone_url: str, clone_path: str) -> None:
    if os.path.exists(clone_path):
        _log(f"Clone path already exists — removing: {clone_path}")
        subprocess.run(["rm", "-rf", clone_path], check=True)

    _log(f"Cloning {TARGET_ORG}/{TARGET_REPO} → {clone_path} …")
    result = subprocess.run(
        ["git", "clone", "--mirror", clone_url, clone_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        safe_err = stderr_text.replace(TARGET_TOKEN, "***")
        raise RuntimeError(f"git clone failed (exit {result.returncode}): {safe_err}")
    if stderr_text:
        _log(f"git clone output: {stderr_text}")
    _log(f"Clone complete: {clone_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    clone_path = _clone_dir()
    _log(f"Starting — {TARGET_ORG}/{TARGET_REPO} | dest={clone_path}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    clone_repo(_build_clone_url(), clone_path)

    print(json.dumps({
        "status":          "completed",
        "migration_job_id": MIGRATION_JOB_ID,
    }))


main()
