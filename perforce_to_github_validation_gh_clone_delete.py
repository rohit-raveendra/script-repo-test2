"""perforce_to_github_validation_gh_clone_delete.py — Delete the bare-mirror clone created by perforce_to_github_validation_gh_clone.

CLEANUP STEP in the P4→GitHub changelist comparison pipeline.
Removes the directory ``{output_dir}/{migration_job_id}/`` that was created by the
``perforce_to_github_validation_gh_clone`` step, freeing EFS space once the pipeline has finished.

No placeholders are declared here — all inputs come from the preceding
``perforce_to_github_validation_inputs`` step via ``{{steps.perforce_to_github_validation_inputs.<key>}}``.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_gh_clone_delete.<key>}}:
  status, migration_job_id, deleted_path
"""
import json
import os
import shutil
import sys

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ───────────────────────────────────────────────────────────────────────────

_LOG_FILE = f"/tmp/perforce_to_github_validation_gh_clone_delete_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_gh_clone_delete] {msg}\n"
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
    """Return the bare-clone path that perforce_to_github_validation_gh_clone created: {OUTPUT_DIR}/{MIGRATION_JOB_ID}

    Uses the same formula as perforce_to_github_validation_gh_clone._clone_dir() so both steps always
    agree on the path without passing it between steps.
    """
    return os.path.join(OUTPUT_DIR, MIGRATION_JOB_ID)


# ---------------------------------------------------------------------------
# Core delete
# ---------------------------------------------------------------------------

def delete_clone(clone_path: str) -> None:
    if not os.path.exists(clone_path):
        _log(f"Clone path does not exist — nothing to delete: {clone_path}")
        return

    _log(f"Deleting clone directory: {clone_path} …")
    shutil.rmtree(clone_path)
    _log(f"Deleted: {clone_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    clone_path = _clone_dir()
    _log(f"Starting — {MIGRATION_JOB_ID} | target={clone_path}")

    delete_clone(clone_path)

    print(json.dumps({
        "status":          "completed",
        "migration_job_id": MIGRATION_JOB_ID,
        "deleted_path":    clone_path,
    }))


main()
