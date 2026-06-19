"""perforce_to_github_validation_p4_integrations.py — Fetch P4 integration records for a depot path and write them to a JSON file.

STEP 1 of 3 in the P4→GitHub integration/merge-history validation pipeline.
Connects to Perforce via the p4 CLI, runs ``p4 integrated`` to collect every
branch-integration record for the depot path, then enriches each record with
the originating changelist number by running ``p4 changes`` on the target
revision range.  Results are written to
``{output_dir}/{migration_job_id}_p4_integrations.json`` for the comparison step.

If the depot type reported by ``perforce_to_github_validation_depotype`` is
``local``, this step is skipped — local depots have no stream integrations so
there is no merge-history data to fetch.  The result is recorded as MATCH.

No placeholders are declared here — all inputs come from the preceding steps.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.source_url}}        — Perforce server address
  {{steps.perforce_to_github_validation_inputs.source_username}}   — Perforce username
  {{steps.perforce_to_github_validation_inputs.source_password}}   — Perforce password or login ticket
  {{steps.perforce_to_github_validation_inputs.source_repo_path}}  — Depot path (e.g. "//depot/main/...")
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Inputs (from steps.perforce_to_github_validation_depotype):
  {{steps.perforce_to_github_validation_depotype.depot_type}}  — Depot type ('stream' or 'local')

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_p4_integrations.<key>}}:
  status, p4_integrations_path, migration_job_id,
  total_integration_records, unique_cl_numbers, integration_actions
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
SOURCE_URL       = {{steps.perforce_to_github_validation_inputs.source_url}}
SOURCE_USERNAME  = {{steps.perforce_to_github_validation_inputs.source_username}}
SOURCE_PASSWORD  = {{steps.perforce_to_github_validation_inputs.source_password}}
SOURCE_REPO_PATH = {{steps.perforce_to_github_validation_inputs.source_repo_path}}
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ─── Inputs from step: perforce_to_github_validation_depotype ───────────────────────────────────────
DEPOT_TYPE       = {{steps.perforce_to_github_validation_depotype.depot_type}}
# ───────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# p4 integrated output line:
#   //target/path#rev - <action> from //source/path#start[,#end]
_INTEGRATED_RE = re.compile(
    r"^(//[^\s#]+)#(\d+)\s+-\s+(\w+)\s+from\s+(//[^\s#]+)#(\d+)(?:,#(\d+))?$"
)

# p4 changes output header line:
#   Change <N> on <YYYY/MM/DD> by <user>@<client> '<short desc>'
_CHANGES_HDR_RE = re.compile(
    r"^Change\s+(\d+)\s+on\s+(\d{4}/\d{2}/\d{2})\s+by\s+(\S+?)@\S+"
)

_LOG_FILE = f"{OUTPUT_DIR}/perforce_to_github_validation_p4_integrations_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_p4_integrations] {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


def _warn(msg: str) -> None:
    line = f"[perforce_to_github_validation_p4_integrations] WARN {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _p4_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["P4PORT"]   = str(SOURCE_URL)
    env["P4USER"]   = str(SOURCE_USERNAME)
    env["P4PASSWD"] = str(SOURCE_PASSWORD)
    env.setdefault("P4TICKETS", "/tmp/.p4tickets")
    env.setdefault("P4TRUST",   "/tmp/.p4trust")
    return env


def _p4_login(env: Dict[str, str]) -> None:
    try:
        trust = subprocess.run(
            ["p4", "trust", "-f", "-y"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        if trust.returncode == 0:
            _log("p4 trust accepted")
        else:
            _warn(f"p4 trust rc={trust.returncode}: {trust.stderr.strip()!r} — continuing")

        result = subprocess.run(
            ["p4", "login", "-a"],
            input=str(SOURCE_PASSWORD),
            capture_output=True, text=True, env=env, timeout=30,
        )
        if result.returncode == 0:
            _log("p4 login succeeded")
        else:
            _warn(
                f"p4 login rc={result.returncode}: {result.stderr.strip()!r} — "
                "continuing (P4PASSWD may be accepted directly)"
            )
    except FileNotFoundError:
        raise RuntimeError(
            "p4 CLI not found. Ensure the Perforce CLI is installed and on PATH."
        )


def _p4_date_to_iso(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y/%m/%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return date_str


# ---------------------------------------------------------------------------
# Parse p4 integrated output
# ---------------------------------------------------------------------------

def _parse_integrated(raw: str) -> List[Dict[str, Any]]:
    """Parse ``p4 integrated <depot_path>`` output into structured records.

    Each line has the form:
      //target/path#rev - <action> from //source/path#start[,#end]
    """
    records: List[Dict[str, Any]] = []
    unparseable = 0

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _INTEGRATED_RE.match(stripped)
        if m:
            target_path, target_rev, action, source_path, from_rev, to_rev = m.groups()
            records.append({
                "target_path": target_path,
                "target_rev":  int(target_rev),
                "action":      action,
                "source_path": source_path,
                "from_rev":    int(from_rev),
                "to_rev":      int(to_rev) if to_rev else int(from_rev),
            })
        else:
            unparseable += 1
            if unparseable <= 5:
                _warn(f"Unparseable integrated line: {stripped[:120]!r}")

    if unparseable > 5:
        _warn(f"… {unparseable - 5} more unparseable lines omitted from warnings")

    return records


# ---------------------------------------------------------------------------
# Resolve CL numbers via p4 filelog on target path#rev
# ---------------------------------------------------------------------------

def _resolve_cl_for_rev(
    target_path: str,
    target_rev: int,
    env: Dict[str, str],
) -> Optional[str]:
    """Return the CL number that submitted target_path#target_rev, or None.

    Uses ``p4 filelog -m1 <path>#<rev>`` which directly returns the changelist
    that created that exact revision — correct for both ``branch`` and
    ``integrate`` actions.

    The previous approach of ``p4 changes -m1 <path>@<rev>,<rev>`` used a
    *label/CL-range* syntax, not a *file-revision* range, which silently
    returns nothing for most depot paths.
    """
    spec = f"{target_path}#{target_rev}"
    try:
        result = subprocess.run(
            ["p4", "filelog", "-m", "1", spec],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            _warn(
                f"p4 filelog returned nothing for {spec} "
                f"(rc={result.returncode}): {result.stderr.strip()[:120]!r}"
            )
            return None
        # p4 filelog output first content line:
        #   ... #<rev> change <CL> <action> on <date> by <user>@<client> ...
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("...") and "change" in stripped:
                parts = stripped.split()
                # parts: ['...', '#N', 'change', 'CL', 'action', ...]
                try:
                    cl_idx = parts.index("change") + 1
                    return parts[cl_idx]
                except (ValueError, IndexError):
                    pass
        _warn(f"Could not parse CL from p4 filelog output for {spec}: {result.stdout[:200]!r}")
        return None
    except Exception as exc:
        _warn(f"p4 filelog failed for {spec}: {exc}")
        return None


def _enrich_with_cl_numbers(
    records: List[Dict[str, Any]],
    env: Dict[str, str],
    max_lookups: int = 2000,
) -> None:
    """Populate ``cl_number`` on each record by querying p4 filelog.

    To keep runtime bounded, only the first *max_lookups* unique
    (target_path, target_rev) pairs are resolved; the rest receive ``null``.
    """
    seen: Dict[tuple, Optional[str]] = {}
    resolved = 0

    for rec in records:
        key = (rec["target_path"], rec["target_rev"])
        if key in seen:
            rec["cl_number"] = seen[key]
            continue

        if resolved >= max_lookups:
            rec["cl_number"] = None
            seen[key] = None
            continue

        cl = _resolve_cl_for_rev(rec["target_path"], rec["target_rev"], env)
        rec["cl_number"] = cl
        seen[key] = cl
        resolved += 1

        if resolved % 100 == 0:
            _log(f"  CL-lookup progress: {resolved}/{min(len(records), max_lookups)}")

    _log(f"CL lookups completed: {resolved} resolved, {len(records) - resolved} skipped (cap={max_lookups})")


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def fetch_p4_integrations(depot_path: str) -> List[Dict[str, Any]]:
    env = _p4_env()
    _p4_login(env)

    _log(f"Running: p4 integrated {depot_path}")
    result = subprocess.run(
        ["p4", "integrated", depot_path],
        capture_output=True, text=True, env=env, timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"p4 integrated failed (rc={result.returncode}): {result.stderr.strip()}"
        )

    records = _parse_integrated(result.stdout)
    _log(f"Parsed {len(records)} integration records")

    if not records:
        _warn(
            f"p4 integrated returned 0 records for {depot_path!r}. "
            "The depot may have no integration history, or the path may be incorrect."
        )
        return []

    # Enrich with CL numbers (bounded to keep runtime reasonable)
    _log("Resolving changelist numbers for integration records …")
    _enrich_with_cl_numbers(records, env)

    # Aggregate action counts and unique CL set
    action_counts: Dict[str, int] = defaultdict(int)
    unique_cls: Set[str] = set()
    for rec in records:
        action_counts[rec["action"]] += 1
        if rec.get("cl_number"):
            unique_cls.add(rec["cl_number"])

    _log(
        f"Actions: {dict(action_counts)} | "
        f"Unique CLs resolved: {len(unique_cls)}"
    )

    # Build the final output records
    output_records: List[Dict[str, Any]] = []
    for rec in records:
        output_records.append({
            # Source side (where the change came from)
            "source_path":  rec["source_path"],
            "from_rev":     rec["from_rev"],
            "to_rev":       rec["to_rev"],
            # Target side (where it was integrated to)
            "target_path":  rec["target_path"],
            "target_rev":   rec["target_rev"],
            # Integration metadata
            "action":       rec["action"],
            "cl_number":    rec.get("cl_number"),
            # Derived identifiers for comparison
            "source_id":    rec.get("cl_number"),
            "source_short_id": f"CL-{rec['cl_number']}" if rec.get("cl_number") else None,
            "source_type":  "integration",
            "source_repo_id": MIGRATION_JOB_ID,
        })

    return output_records


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _log(f"Starting — depot={SOURCE_REPO_PATH} | depot_type={DEPOT_TYPE} | out={OUTPUT_DIR}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Local-depot short-circuit ───────────────────────────────────────────
    # Local depots have no P4 stream integrations — p4-fusion migrates the
    # entire depot as a linear history to a single branch with no
    # branch-to-branch merge operations.  There is no integration history
    # to fetch, so this step is not applicable and is recorded as MATCH.
    if str(DEPOT_TYPE).lower() == "local":
        evidence = (
            "Depot type is 'local'. Local depots do not have P4 stream integrations — "
            "p4-fusion migrates the entire depot as a linear history to a single branch "
            "with no branch-to-branch merge operations. "
            "There is no integration history available to migrate as merge history "
            "to GitHub, so this validation step is not applicable and is recorded as MATCH."
        )
        _log(f"Local depot — skipping P4 integration fetch. {evidence}")

        out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_p4_integrations.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        _log(f"Saved empty integrations → {out_path}")

        print(json.dumps({
            "status":                    "completed",
            "p4_integrations_path":      out_path,
            "repo_id":                   MIGRATION_JOB_ID,
            "depot_type":                "local",
            "total_integration_records": 0,
            "unique_cl_numbers":         0,
            "integration_actions":       {},
            "overall_status":            "MATCH",
            "evidence_message":          evidence,
        }))
        return
    # ── End local-depot short-circuit ──────────────────────────────────────

    probe = os.path.join(OUTPUT_DIR, f".write_probe_{MIGRATION_JOB_ID}")
    with open(probe, "w") as pf:
        pf.write("ok")
    os.remove(probe)
    _log(f"Output dir writable: {OUTPUT_DIR}")

    _log("Fetching P4 integration records …")
    records = fetch_p4_integrations(SOURCE_REPO_PATH)
    _log(f"Fetched {len(records)} integration records")

    # Build summary stats for the output manifest
    action_counts: Dict[str, int] = defaultdict(int)
    unique_cls: Set[str] = set()
    for r in records:
        action_counts[r["action"]] += 1
        if r.get("cl_number"):
            unique_cls.add(r["cl_number"])

    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_p4_integrations.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, default=str)
    _log(f"Saved → {out_path} ({os.path.getsize(out_path)} bytes)")

    print(json.dumps({
        "status":                   "completed",
        "p4_integrations_path":     out_path,
        "repo_id":                  MIGRATION_JOB_ID,
        "total_integration_records": len(records),
        "unique_cl_numbers":        len(unique_cls),
        "integration_actions":      dict(action_counts),
    }))


main()
