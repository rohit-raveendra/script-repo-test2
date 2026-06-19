"""perforce_to_github_validation_p4_shelves.py — Fetch all shelved changelists from Perforce and write them to a JSON file.

STEP 1 of 3 in the P4→GitHub shelved-changelist validation pipeline.

Connects to Perforce via the p4 CLI, runs ``p4 changes -s shelved`` to discover
every shelved changelist under the configured depot path, then calls
``p4 describe -s -S <cl>`` for each to collect the full file list and action
per shelved file.  Results are written to
``{output_dir}/{migration_job_id}_p4_shelves.json`` for the comparison step.

No placeholders are declared here — all inputs come from the preceding
``perforce_to_github_validation_inputs`` step via ``{{steps.perforce_to_github_validation_inputs.<key>}}``.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.source_url}}        — Perforce server address
  {{steps.perforce_to_github_validation_inputs.source_username}}   — Perforce username
  {{steps.perforce_to_github_validation_inputs.source_password}}   — Perforce password or login ticket
  {{steps.perforce_to_github_validation_inputs.source_repo_path}}  — Depot path (e.g. "//depot/main/...")
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_p4_shelves.<key>}}:
  status, p4_shelves_path, migration_job_id, total_shelved_cls
"""
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
SOURCE_URL       = {{steps.perforce_to_github_validation_inputs.source_url}}
SOURCE_USERNAME  = {{steps.perforce_to_github_validation_inputs.source_username}}
SOURCE_PASSWORD  = {{steps.perforce_to_github_validation_inputs.source_password}}
SOURCE_REPO_PATH = {{steps.perforce_to_github_validation_inputs.source_repo_path}}
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ───────────────────────────────────────────────────────────────────────────

# p4 changes header line:  Change <N> on <YYYY/MM/DD> by <user>@<client>
_CHANGES_HDR_RE = re.compile(r"^Change\s+(\d+)\s+on\s+(\S+)\s+by\s+([^\s@]+)")

# p4 describe shelved-file line:  ... //depot/path#rev <action>
_FILE_LINE_RE = re.compile(r"^\.\.\.\s+(//[^\s#]+)#\d+\s+(\S+)")

_LOG_FILE = f"{OUTPUT_DIR}/perforce_to_github_validation_p4_shelves_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_p4_shelves] {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


def _warn(msg: str) -> None:
    line = f"[perforce_to_github_validation_p4_shelves] WARN {msg}\n"
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
                "continuing (SOURCE_PASSWORD may be accepted directly)"
            )
    except FileNotFoundError:
        raise RuntimeError(
            "p4 CLI not found. Ensure the Perforce CLI is installed and on PATH."
        )


def _normalise_depot_root(depot_path: str) -> str:
    """Strip trailing /... to get the bare depot root, e.g. //depot/main."""
    return depot_path.rstrip("/").removesuffix("...").rstrip("/")


def _fetch_shelved_cls(env: Dict[str, str], depot_path: str) -> List[Dict[str, str]]:
    """Return a list of {cl, date, user} dicts for all shelved CLs under depot_path."""
    result = subprocess.run(
        ["p4", "changes", "-s", "shelved", depot_path],
        capture_output=True, text=True, env=env, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"p4 changes -s shelved failed (rc={result.returncode}): "
            f"{result.stderr.strip()!r}"
        )

    shelves: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        m = _CHANGES_HDR_RE.match(line)
        if m:
            shelves.append({
                "cl":   m.group(1),
                "date": m.group(2),
                "user": m.group(3),
            })
    return shelves


def _describe_shelf(
    env: Dict[str, str],
    cl: str,
    depot_root: str,
) -> Optional[Dict[str, Any]]:
    """Run ``p4 describe -s -S <cl>`` and return a structured record.

    Returns ``None`` if the CL has no shelved files under *depot_root*.
    """
    result = subprocess.run(
        ["p4", "describe", "-s", "-S", cl],
        capture_output=True, text=True, env=env, timeout=60,
    )
    if result.returncode != 0:
        _warn(f"p4 describe -s -S {cl} failed (rc={result.returncode}): {result.stderr.strip()!r}")
        return None

    # Parse description text (everything between header and "Shelved files ...")
    description_lines: List[str] = []
    files: List[Dict[str, str]] = []
    in_desc = False
    in_files = False

    for line in result.stdout.splitlines():
        if line.startswith("Change ") and not in_desc:
            in_desc = True
            continue
        if line.startswith("Shelved files ..."):
            in_desc = False
            in_files = True
            continue
        if in_desc and line.startswith("\t"):
            description_lines.append(line.strip())
            continue
        if in_files and line.startswith("... "):
            m = _FILE_LINE_RE.match(line.strip())
            if m:
                depot_file = m.group(1).split("#")[0]
                action     = m.group(2)
                # Only include files under this depot
                if depot_file.startswith(f"{depot_root}/"):
                    files.append({"depot_file": depot_file, "action": action})

    if not files:
        return None

    return {
        "cl":          cl,
        "files":       files,
        "file_count":  len(files),
        "description": " ".join(description_lines).strip(),
    }


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def fetch_p4_shelves() -> List[Dict[str, Any]]:
    env = _p4_env()
    _p4_login(env)

    depot_root = _normalise_depot_root(SOURCE_REPO_PATH)
    depot_path = f"{depot_root}/..."

    _log(f"Querying shelved CLs under {depot_path}")
    raw_shelves = _fetch_shelved_cls(env, depot_path)
    _log(f"Found {len(raw_shelves)} shelved CL(s)")

    records: List[Dict[str, Any]] = []

    for entry in raw_shelves:
        cl   = entry["cl"]
        user = entry["user"]
        date = entry["date"]

        _log(f"Describing shelved CL {cl} by {user} ...")
        detail = _describe_shelf(env, cl, depot_root)

        if detail is None:
            _warn(f"CL {cl} has no files under {depot_root} — skipping")
            continue

        # Derive the expected GitHub feature-branch name using the same
        # convention as perforce_to_github_migration_migrate_shelves
        expected_github_branch = f"shelved/{user}/{cl}"

        records.append({
            "cl":                    cl,
            "user":                  user,
            "date":                  date,
            "description":           detail["description"],
            "file_count":            detail["file_count"],
            "files":                 detail["files"],
            "expected_github_branch": expected_github_branch,
        })

    return records


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        records = fetch_p4_shelves()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        print(
            json.dumps({
                "status":            "failure",
                "error":             str(exc),
                "p4_shelves_path":   "",
                "migration_job_id":  MIGRATION_JOB_ID,
                "total_shelved_cls": 0,
            })
        )
        sys.exit(1)

    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_p4_shelves.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)

    _log(f"Wrote {len(records)} shelved CL record(s) to {out_path}")

    print(
        json.dumps({
            "status":            "success",
            "p4_shelves_path":   out_path,
            "migration_job_id":  MIGRATION_JOB_ID,
            "total_shelved_cls": len(records),
        })
    )


if __name__ == "__main__":
    main()
