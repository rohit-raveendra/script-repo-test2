"""perforce_to_github_validation_shelvescompare.py — Compare P4 shelved changelists against GitHub shelved-CL feature branches.

STEP 3 of 3 in the P4→GitHub shelved-changelist validation pipeline.

No placeholders are declared here — all inputs come from the preceding
``perforce_to_github_validation_inputs`` step via ``{{steps.perforce_to_github_validation_inputs.<key>}}``.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_shelvescompare.<key>}}:
  status, overall_status, summary, matched, missing_in_github,
  file_count_mismatches, orphaned_in_github, comparison_path, migration_job_id
"""
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ───────────────────────────────────────────────────────────────────────────

_LOG_FILE = f"{OUTPUT_DIR}/perforce_to_github_validation_shelvescompare_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_shelvescompare] {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


def _warn(msg: str) -> None:
    line = f"[perforce_to_github_validation_shelvescompare] WARN {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


# ---------------------------------------------------------------------------
# Load pre-fetched data files
# ---------------------------------------------------------------------------

def _load_json_file(path: str, label: str) -> Any:
    if not os.path.exists(path):
        raise RuntimeError(
            f"{label} file not found: {path!r}. "
            "Ensure the preceding fetch steps completed successfully and "
            "OUTPUT_DIR / MIGRATION_JOB_ID are identical across all steps."
        )
    _log(f"Loading {label} from {path} ({os.path.getsize(path)} bytes)")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_p4_shelves() -> List[Dict[str, Any]]:
    path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_p4_shelves.json")
    data = _load_json_file(path, "P4 shelves")
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON array in {path!r}, got {type(data).__name__}.")
    _log(f"Loaded {len(data)} P4 shelved CL record(s)")
    return data


def load_github_shelves() -> List[Dict[str, Any]]:
    path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_github_shelves.json")
    data = _load_json_file(path, "GitHub shelves")
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON array in {path!r}, got {type(data).__name__}.")
    _log(f"Loaded {len(data)} GitHub shelf branch record(s)")
    return data


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------

def build_comparison(
    p4_shelves:  List[Dict[str, Any]],
    gh_shelves:  List[Dict[str, Any]],
) -> Dict[str, Any]:
    # Index GitHub shelves by CL number for O(1) lookup
    gh_by_cl: Dict[str, Dict[str, Any]] = {}
    for record in gh_shelves:
        cl_num: Optional[str] = record.get("cl_number")
        if cl_num:
            gh_by_cl[cl_num] = record

    # Track which GitHub CL numbers were matched so we can detect orphans
    matched_gh_cls = set()

    matched:               List[Dict[str, Any]] = []
    missing_in_github:     List[Dict[str, Any]] = []
    file_count_mismatches: List[Dict[str, Any]] = []

    for p4_rec in p4_shelves:
        cl         = p4_rec["cl"]
        user       = p4_rec["user"]
        p4_count   = p4_rec.get("file_count", 0)
        exp_branch = p4_rec.get("expected_github_branch", f"shelved/{user}/{cl}")

        gh_rec = gh_by_cl.get(cl)

        if gh_rec is None:
            _warn(f"CL {cl} (user={user}) has no GitHub branch '{exp_branch}'")
            missing_in_github.append({
                "cl":                     cl,
                "user":                   user,
                "p4_file_count":          p4_count,
                "expected_github_branch": exp_branch,
                "p4_description":         p4_rec.get("description", ""),
            })
        else:
            matched_gh_cls.add(cl)
            gh_count = gh_rec.get("file_count", 0)

            entry: Dict[str, Any] = {
                "cl":               cl,
                "user":             user,
                "github_branch":    gh_rec["branch"],
                "tip_sha":          gh_rec.get("tip_sha", ""),
                "tip_date":         gh_rec.get("tip_date", ""),
                "p4_file_count":    p4_count,
                "gh_file_count":    gh_count,
                "file_count_match": p4_count == gh_count,
            }

            if p4_count != gh_count:
                _warn(
                    f"CL {cl}: file count mismatch — P4={p4_count}, GitHub={gh_count} "
                    f"on branch '{gh_rec['branch']}'"
                )
                file_count_mismatches.append(entry)
            else:
                matched.append(entry)
                _log(f"CL {cl}: MATCH — branch='{gh_rec['branch']}', files={p4_count}")

    # Orphaned GitHub branches: shelf branches with no corresponding P4 shelf
    p4_cl_set = {r["cl"] for r in p4_shelves}
    orphaned_in_github: List[Dict[str, Any]] = [
        {
            "cl":      r["cl_number"],
            "branch":  r["branch"],
            "user":    r.get("user", ""),
            "tip_sha": r.get("tip_sha", ""),
        }
        for r in gh_shelves
        if r.get("cl_number") and r["cl_number"] not in p4_cl_set
    ]

    if orphaned_in_github:
        _warn(
            f"{len(orphaned_in_github)} GitHub shelf branch(es) have no matching P4 shelf: "
            + ", ".join(r["branch"] for r in orphaned_in_github)
        )

    # Determine overall status
    total_p4     = len(p4_shelves)
    total_matched = len(matched)                   # exact match (branch + file count)
    total_partial = len(file_count_mismatches)     # branch present but file count differs
    total_missing = len(missing_in_github)         # no branch at all

    if total_p4 == 0:
        overall_status = "MATCH"
        _log("No P4 shelved CLs found — trivial MATCH")
    elif total_missing == total_p4 and total_partial == 0:
        overall_status = "MISMATCH"
    elif total_matched == total_p4 and total_partial == 0:
        overall_status = "MATCH"
    else:
        overall_status = "PARTIAL_MATCH"

    return {
        "overall_status":          overall_status,
        "total_p4_shelved_cls":    total_p4,
        "total_gh_shelf_branches": len(gh_shelves),
        "matched":                 matched,
        "file_count_mismatches":   file_count_mismatches,
        "missing_in_github":       missing_in_github,
        "orphaned_in_github":      orphaned_in_github,
        "summary": {
            "matched_count":              total_matched,
            "file_count_mismatch_count":  total_partial,
            "missing_in_github_count":    total_missing,
            "orphaned_in_github_count":   len(orphaned_in_github),
        },
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        p4_shelves = load_p4_shelves()
        gh_shelves = load_github_shelves()
    except Exception as exc:
        _log(f"FATAL loading data files: {exc}")
        print(
            json.dumps({
                "status":          "failure",
                "error":           str(exc),
                "overall_status":  "UNKNOWN",
                "comparison_path": "",
                "migration_job_id": MIGRATION_JOB_ID,
            })
        )
        sys.exit(1)

    try:
        result = build_comparison(p4_shelves, gh_shelves)
    except Exception as exc:
        _log(f"FATAL during comparison: {exc}")
        print(
            json.dumps({
                "status":          "failure",
                "error":           str(exc),
                "overall_status":  "UNKNOWN",
                "comparison_path": "",
                "migration_job_id": MIGRATION_JOB_ID,
            })
        )
        sys.exit(1)

    report: Dict[str, Any] = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "migration_job_id": MIGRATION_JOB_ID,
        **result,
    }

    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_shelves_comparison.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    _log(
        f"Comparison complete — overall_status={result['overall_status']}, "
        f"matched={result['summary']['matched_count']}, "
        f"file_count_mismatches={result['summary']['file_count_mismatch_count']}, "
        f"missing_in_github={result['summary']['missing_in_github_count']}, "
        f"orphaned_in_github={result['summary']['orphaned_in_github_count']}"
    )
    _log(f"Report written to {out_path}")

    print(
        json.dumps({
            "status":                "success",
            "overall_status":        result["overall_status"],
            "summary":               result["summary"],
            "matched":               result["matched"],
            "missing_in_github":     result["missing_in_github"],
            "file_count_mismatches": result["file_count_mismatches"],
            "orphaned_in_github":    result["orphaned_in_github"],
            "comparison_path":       out_path,
            "migration_job_id":      MIGRATION_JOB_ID,
        })
    )


if __name__ == "__main__":
    main()
