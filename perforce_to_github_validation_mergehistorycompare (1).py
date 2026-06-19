"""perforce_to_github_validation_mergehistorycompare.py — Compare P4 integration CLs against GitHub merge commits.

STEP 3 of 3 in the P4→GitHub integration/merge-history validation pipeline.
Loads the JSON files written by ``perforce_to_github_validation_p4_integrations`` and
``perforce_to_github_validation_gh_merge_history``, matches each unique P4 integration changelist to a
GitHub merge commit via the embedded p4-fusion trailer, and writes the full
comparison result to
``{output_dir}/{migration_job_id}_merge_history_comparison.json``.  No network calls
are made.

If the depot type reported by ``perforce_to_github_validation_depotype`` is
``local``, this step is skipped — local depots have no stream integrations
and therefore no merge-history data to compare.  The result is recorded as MATCH.

Matching logic:
  ``perforce_to_github_validation_p4_integrations`` resolves a ``cl_number`` (= ``source_id``) for each
  file-level integration record via ``p4 changes``.  Multiple integration
  records can share the same CL number because one ``p4 integrate`` submission
  can touch many files.  This script deduplicates to **unique CL numbers**
  before comparing.

  ``perforce_to_github_validation_gh_merge_history`` extracts a ``cl_number`` from the p4-fusion trailer
  in every merge commit message.  Each unique CL number should correspond to
  exactly one merge commit on its target branch.

  A match is recorded when a P4 integration CL number is found in the index
  of GitHub merge commits.  The comparison spans **all branches** — the
  ``target_branch`` field on each merge commit record shows where it lives.

Overall status:
  MATCH         — every P4 integration CL has a corresponding GitHub merge commit.
  PARTIAL_MATCH — some P4 integration CLs matched, others are missing.
  MISMATCH      — no P4 integration CLs matched any GitHub merge commit.

No placeholders are declared here — all inputs come from the preceding steps.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Inputs (from steps.perforce_to_github_validation_depotype):
  {{steps.perforce_to_github_validation_depotype.depot_type}} — Depot type ('stream' or 'local')

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_mergehistorycompare.<key>}}:
  status, comparison_path, migration_job_id, overall_status, summary
"""
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ─── Inputs from step: perforce_to_github_validation_depotype ───────────────────────────────────────
DEPOT_TYPE       = {{steps.perforce_to_github_validation_depotype.depot_type}}
# ───────────────────────────────────────────────────────────────────────────

_LOG_FILE = f"{OUTPUT_DIR}/perforce_to_github_validation_mergehistorycompare_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_mergehistorycompare] {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


def _warn(msg: str) -> None:
    line = f"[perforce_to_github_validation_mergehistorycompare] WARN {msg}\n"
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


def load_p4_integrations() -> List[Dict[str, Any]]:
    path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_p4_integrations.json")
    data = _load_json_file(path, "P4 integrations")
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON array in {path!r}, got {type(data).__name__}.")
    _log(f"Loaded {len(data)} P4 integration records")
    return data


def load_github_merge_history() -> List[Dict[str, Any]]:
    path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_github_merge_history.json")
    data = _load_json_file(path, "GitHub merge history")
    if not isinstance(data, list):
        raise RuntimeError(f"Expected a JSON array in {path!r}, got {type(data).__name__}.")
    _log(f"Loaded {len(data)} GitHub merge commit records (across all branches)")
    return data


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------

def build_comparison(
    p4_integrations: List[Dict[str, Any]],
    gh_merges: List[Dict[str, Any]],
) -> Dict[str, Any]:
    # ── Deduplicate P4 integration records to unique CL numbers ─────────────
    # Many file-level records share the same CL (one p4 integrate submission
    # can touch many files).  Sort by source_path for a stable representative.
    p4_sorted = sorted(p4_integrations, key=lambda r: r.get("source_path", ""))
    unique_cls: Dict[str, Dict[str, Any]] = {}
    for rec in p4_sorted:
        cl_id = rec.get("source_id") or rec.get("cl_number")
        if cl_id and cl_id not in unique_cls:
            unique_cls[cl_id] = rec

    p4_without_cl = [
        r for r in p4_integrations
        if not (r.get("source_id") or r.get("cl_number"))
    ]

    _log(
        f"P4 integrations: {len(p4_integrations)} file records, "
        f"{len(unique_cls)} unique CL numbers, "
        f"{len(p4_without_cl)} records without a resolved CL number"
    )

    # ── Index GitHub merge commits by CL number ─────────────────────────────
    # The same CL can appear on multiple branches (backport / cherry-pick).
    # Keep all occurrences in a list so every branch reference is captured.
    gh_by_cl: Dict[str, List[Dict[str, Any]]] = {}
    gh_no_cl: List[Dict[str, Any]] = []

    for merge in gh_merges:
        cl_num: Optional[str] = merge.get("cl_number")
        if cl_num:
            gh_by_cl.setdefault(cl_num, []).append(merge)
        else:
            gh_no_cl.append(merge)

    cl_with_ref = sum(len(v) for v in gh_by_cl.values())
    _log(
        f"GitHub merge index: {cl_with_ref} merge commit(s) with CL reference "
        f"({len(gh_by_cl)} unique CL numbers), {len(gh_no_cl)} without"
    )

    matched:           List[Dict[str, Any]] = []
    missing_in_target: List[Dict[str, Any]] = []
    matched_cl_ids:    Set[str]             = set()

    for cl_id, rec in sorted(unique_cls.items()):
        gh_list = gh_by_cl.get(cl_id, [])

        if gh_list:
            matched_cl_ids.add(cl_id)
            # Primary match — first merge commit found (deterministic: sorted by branch name)
            gh_list_sorted = sorted(gh_list, key=lambda c: c.get("target_branch", ""))
            gh = gh_list_sorted[0]

            if len(gh_list) > 1:
                _warn(
                    f"CL {cl_id!r} matched on {len(gh_list)} branches: "
                    + ", ".join(c.get("target_branch", "?") for c in gh_list_sorted)
                    + " — using branch "
                    + gh.get("target_branch", "?")
                )

            matched.append({
                "source_id":            cl_id,
                "source_short_id":      rec.get("source_short_id") or f"CL-{cl_id}",
                "source_path":          rec.get("source_path"),
                "target_path":          rec.get("target_path"),
                "action":               rec.get("action"),
                "source_type":          rec.get("source_type", "integration"),
                "source_repo_id":       rec.get("source_repo_id"),
                "target_id":            gh["target_id"],
                "target_short_id":      gh.get("target_short_id"),
                "target_title":         gh["target_title"],
                "target_url":           gh.get("target_url", ""),
                "target_type":          gh.get("target_type", "merge_commit"),
                "target_branch":        gh.get("target_branch"),
                "target_author":        gh.get("target_author"),
                "target_authored_date": gh.get("target_authored_date"),
                "parent_ids":           gh.get("parent_ids", []),
                "parent_count":         gh.get("parent_count"),
                "status": "MATCH",
            })
        else:
            missing_in_target.append({
                "source_id":       cl_id,
                "source_short_id": rec.get("source_short_id") or f"CL-{cl_id}",
                "source_path":     rec.get("source_path"),
                "target_path":     rec.get("target_path"),
                "action":          rec.get("action"),
                "source_type":     rec.get("source_type", "integration"),
                "status": "MISSING_IN_TARGET",
                "note": (
                    f"P4 integration CL {cl_id} (action={rec.get('action')!r}, "
                    f"source={rec.get('source_path')!r} → target={rec.get('target_path')!r}) "
                    "has no corresponding GitHub merge commit with a matching p4-fusion trailer."
                ),
            })

    # GitHub merge commits whose CL number was not in the P4 integrations list
    extra_in_target: List[Dict[str, Any]] = [
        {
            "target_id":       gh["target_id"],
            "target_short_id": gh.get("target_short_id"),
            "target_title":    gh["target_title"],
            "target_url":      gh.get("target_url", ""),
            "target_branch":   gh.get("target_branch"),
            "source_id":       cl_id,
            "status": "EXTRA_IN_TARGET",
            "note": (
                f"GitHub merge commit references CL {cl_id} via p4-fusion trailer but "
                "that CL was not returned by the P4 integration query "
                "(different depot path, deleted CL, or lookup cap reached)."
            ),
        }
        for cl_id, gh_list in gh_by_cl.items()
        if cl_id not in matched_cl_ids
        for gh in gh_list
    ]

    # GitHub merge commits with no p4-fusion trailer at all
    unannotated_merges: List[Dict[str, Any]] = [
        {
            "target_id":       c["target_id"],
            "target_short_id": c.get("target_short_id"),
            "target_title":    c["target_title"],
            "target_url":      c.get("target_url", ""),
            "target_branch":   c.get("target_branch"),
            "parent_ids":      c.get("parent_ids", []),
            "note": (
                "No p4-fusion changelist trailer found in merge commit message. "
                "This merge may have been created directly in GitHub after migration, "
                "or it is a GitHub pull-request merge not originating from a P4 integrate."
            ),
        }
        for c in gh_no_cl
    ]

    # P4 integration records that had no resolved CL number
    unresolved_p4: List[Dict[str, Any]] = [
        {
            "source_path": r.get("source_path"),
            "target_path": r.get("target_path"),
            "target_rev":  r.get("target_rev"),
            "action":      r.get("action"),
            "note": (
                "P4 integration record could not be matched — no CL number was resolved "
                "(p4 changes lookup failed, lookup cap reached, or record has no source_id)."
            ),
        }
        for r in p4_without_cl
    ]

    total_unique_p4_cls = len(unique_cls)
    total_matched       = len(matched)

    _log(
        f"Comparison done: {total_matched}/{total_unique_p4_cls} integration CLs matched, "
        f"{len(missing_in_target)} missing, {len(extra_in_target)} extra, "
        f"{len(unannotated_merges)} unannotated merges, "
        f"{len(unresolved_p4)} unresolved P4 records"
    )

    return {
        "total": {
            "source_integration_records": len(p4_integrations),
            "source_unique_cls":          total_unique_p4_cls,
            "target_merge_commits":       len(gh_merges),
            "matched":                    total_matched,
            "match":                      total_matched == total_unique_p4_cls,
            "diff":                       total_matched - total_unique_p4_cls,
            "note": (
                f"{len(missing_in_target)} integration CL(s) not found as GitHub merge commits."
                if missing_in_target else None
            ),
        },
        "matched":            matched,
        "missing_in_target":  missing_in_target,
        "extra_in_target":    extra_in_target,
        "unannotated_merges": unannotated_merges,
        "unresolved_p4":      unresolved_p4,
    }


# ---------------------------------------------------------------------------
# Summary + overall status
# ---------------------------------------------------------------------------

def _calculate_summary(comparison: Dict[str, Any]) -> Dict[str, Any]:
    total       = comparison["total"]
    missing     = comparison["missing_in_target"]
    extra       = comparison["extra_in_target"]
    unannotated = comparison.get("unannotated_merges", [])
    unresolved  = comparison.get("unresolved_p4", [])

    summary: Dict[str, Any] = {
        "total_integration_records_source":    total["source_integration_records"],
        "total_unique_cls_source":             total["source_unique_cls"],
        "total_merge_commits_target":          total["target_merge_commits"],
        "integration_cls_matched":             total["matched"],
        "integration_cls_missing_in_target":   len(missing),
        "merge_commits_extra_in_target":       len(extra),
        "merge_commits_without_cl_reference":  len(unannotated),
        "p4_records_without_cl_resolved":      len(unresolved),
    }

    if unannotated:
        summary["evidence_message"] = (
            f"{len(unannotated)} GitHub merge commit(s) have no p4-fusion CL trailer — "
            "these may be post-migration GitHub pull-request merges and are not counted "
            "as missing or extra integration records."
        )

    return summary


def _determine_overall_status(summary: Dict[str, Any]) -> str:
    missing = summary["integration_cls_missing_in_target"]
    matched = summary["integration_cls_matched"]
    if missing == 0:
        return "MATCH"
    elif matched > 0:
        return "PARTIAL_MATCH"
    else:
        return "MISMATCH"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _log(f"Starting — repo={MIGRATION_JOB_ID} | depot_type={DEPOT_TYPE} | out={OUTPUT_DIR}")

    # ── Local-depot short-circuit ───────────────────────────────────────────
    # Merge-history comparison is only meaningful for stream depots.
    # Local depots have no P4 branch integrations and therefore no GitHub
    # merge commits originating from P4 — the result is automatically MATCH.
    if str(DEPOT_TYPE).lower() == "local":
        evidence = (
            "Depot type is 'local'. Local depots do not have P4 stream integrations — "
            "p4-fusion migrates the entire depot as a linear history to a single branch "
            "with no branch-to-branch merge operations. "
            "There is no integration history available to migrate as merge history "
            "to GitHub, so this validation step is not applicable and is recorded as MATCH."
        )
        _log(f"Local depot — skipping merge history comparison. {evidence}")

        summary = {
            "total_integration_records_source":   0,
            "total_unique_cls_source":            0,
            "total_merge_commits_target":         0,
            "integration_cls_matched":            0,
            "integration_cls_missing_in_target":  0,
            "merge_commits_extra_in_target":      0,
            "merge_commits_without_cl_reference": 0,
            "p4_records_without_cl_resolved":     0,
            "evidence_message":                   evidence,
        }
        overall_status = "MATCH"

        result: Dict[str, Any] = {
            "repo_id":                  MIGRATION_JOB_ID,
            "comparison_timestamp":     datetime.now(timezone.utc).isoformat(),
            "source_data_freshness":    "pre-fetched",
            "depot_type":               "local",
            "merge_history_comparison": {
                "total": {
                    "source_integration_records": 0,
                    "source_unique_cls":          0,
                    "target_merge_commits":       0,
                    "matched":                    0,
                    "match":                      True,
                    "diff":                       0,
                    "note":                       None,
                },
                "matched":            [],
                "missing_in_target":  [],
                "extra_in_target":    [],
                "unannotated_merges": [],
                "unresolved_p4":      [],
            },
            "summary":        summary,
            "overall_status": overall_status,
        }

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_merge_history_comparison.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        _log(f"Saved → {out_path} ({os.path.getsize(out_path)} bytes)")

        print(json.dumps({
            "status":          "completed",
            "comparison_path": out_path,
            "repo_id":         MIGRATION_JOB_ID,
            "overall_status":  overall_status,
            "summary":         summary,
        }))
        return
    # ── End local-depot short-circuit ──────────────────────────────────────

    p4_integrations = load_p4_integrations()
    gh_merges       = load_github_merge_history()

    _log(
        f"P4 integrations: {len(p4_integrations)} records | "
        f"GitHub merge commits: {len(gh_merges)}"
    )

    _log("Running merge history comparison …")
    comparison = build_comparison(p4_integrations, gh_merges)

    summary        = _calculate_summary(comparison)
    overall_status = _determine_overall_status(summary)
    _log(f"Overall status: {overall_status}")

    result: Dict[str, Any] = {
        "repo_id":                  MIGRATION_JOB_ID,
        "comparison_timestamp":     datetime.now(timezone.utc).isoformat(),
        "source_data_freshness":    "pre-fetched",
        "merge_history_comparison": comparison,
        "summary":                  summary,
        "overall_status":           overall_status,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_merge_history_comparison.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    _log(f"Saved → {out_path} ({os.path.getsize(out_path)} bytes)")

    print(json.dumps({
        "status":          "completed",
        "comparison_path": out_path,
        "repo_id":         MIGRATION_JOB_ID,
        "overall_status":  overall_status,
        "summary":         summary,
    }))


main()
