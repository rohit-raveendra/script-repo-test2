"""perforce_to_github_validation_p4_changelist.py — Fetch all submitted P4 changelists for a depot path and write them to a JSON file.

STEP 2 of 5 in the P4→GitHub changelist comparison pipeline.
Connects to Perforce via the p4 CLI, runs ``p4 changes -l``, and writes the
result to ``{output_dir}/{migration_job_id}_p4_changelists.json`` for the comparison step.

No placeholders are declared here — all inputs come from the preceding
``perforce_to_github_validation_inputs`` step via ``{{steps.perforce_to_github_validation_inputs.<key>}}``.

Inputs (from steps.perforce_to_github_validation_inputs):
  {{steps.perforce_to_github_validation_inputs.source_url}}        — Perforce server address
  {{steps.perforce_to_github_validation_inputs.source_username}}   — Perforce username
  {{steps.perforce_to_github_validation_inputs.source_password}}   — Perforce password or login ticket
  {{steps.perforce_to_github_validation_inputs.source_repo_path}}  — Depot path (e.g. "//depot/main/...")
  {{steps.perforce_to_github_validation_inputs.output_dir}}        — Shared EFS output directory
  {{steps.perforce_to_github_validation_inputs.migration_job_id}}  — Depot/repo identifier used in file naming

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_p4_changelist.<key>}}:
  status, p4_data_path, migration_job_id, total_changelists
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ─── Inputs from step: perforce_to_github_validation_inputs ─────────────────────────────────────────
SOURCE_URL       = {{steps.perforce_to_github_validation_inputs.source_url}}
SOURCE_USERNAME  = {{steps.perforce_to_github_validation_inputs.source_username}}
SOURCE_PASSWORD  = {{steps.perforce_to_github_validation_inputs.source_password}}
SOURCE_REPO_PATH = {{steps.perforce_to_github_validation_inputs.source_repo_path}}
OUTPUT_DIR       = {{steps.perforce_to_github_validation_inputs.output_dir}}
MIGRATION_JOB_ID = {{steps.perforce_to_github_validation_inputs.migration_job_id}}
# ───────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Regex — p4 changes -l header line
# ---------------------------------------------------------------------------
_P4_HEADER_RE = re.compile(
    r"^Change\s+(\d+)\s+on\s+(\d{4}/\d{2}/\d{2})\s+by\s+(\S+?)@\S+(?:\s+'(.*)')?$"
)

_LOG_FILE = f"/tmp/perforce_to_github_validation_p4_changelist_{MIGRATION_JOB_ID}.log"


def _log(msg: str) -> None:
    line = f"[perforce_to_github_validation_p4_changelist] {msg}\n"
    try:
        with open(_LOG_FILE, "a") as lf:
            lf.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


def _warn(msg: str) -> None:
    line = f"[perforce_to_github_validation_p4_changelist] WARN {msg}\n"
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


def _parse_p4_changes(raw: str) -> List[Dict[str, Any]]:
    changelists: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    desc_lines: List[str] = []

    for line in raw.splitlines():
        header_m = _P4_HEADER_RE.match(line)
        if header_m:
            if current is not None:
                current["description"] = "\n".join(desc_lines).strip() or current["short_desc"]
                changelists.append(current)
            cl_num, date_str, user, short_desc = header_m.groups()
            short_desc = short_desc or ""
            current = {
                "cl_number":   cl_num,
                "date":        date_str,
                "user":        user,
                "short_desc":  short_desc,
                "description": short_desc,
            }
            desc_lines = []
        elif current is not None:
            stripped = line.strip()
            if stripped:
                desc_lines.append(stripped)

    if current is not None:
        current["description"] = "\n".join(desc_lines).strip() or current["short_desc"]
        changelists.append(current)

    return changelists


def _p4_date_to_iso(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y/%m/%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return date_str


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def fetch_p4_changelists(depot_path: str) -> List[Dict[str, Any]]:
    env = _p4_env()
    _p4_login(env)

    def _run(extra_flags: List[str]) -> subprocess.CompletedProcess:
        cmd = ["p4", "changes", "-l"] + extra_flags + [depot_path]
        _log(f"Running: {' '.join(cmd)}")
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)

    result = _run(["-s", "submitted"])
    if result.returncode != 0:
        raise RuntimeError(
            f"p4 changes failed (rc={result.returncode}): {result.stderr.strip()}"
        )

    raw_cls = _parse_p4_changes(result.stdout)
    _log(f"Parsed {len(raw_cls)} changelists (submitted filter)")

    if not raw_cls:
        _warn(
            "No submitted changelists found — retrying without '-s submitted' filter. "
            f"stdout: {result.stdout[:200]!r}  stderr: {result.stderr[:200]!r}"
        )
        result2 = _run([])
        if result2.returncode != 0:
            raise RuntimeError(
                f"p4 changes (no filter) failed (rc={result2.returncode}): "
                f"{result2.stderr.strip()}"
            )
        raw_cls = _parse_p4_changes(result2.stdout)
        _log(f"Parsed {len(raw_cls)} changelists (no status filter)")
        if not raw_cls:
            raise RuntimeError(
                f"p4 changes returned 0 changelists for {depot_path!r}. "
                f"Verify the depot name and that user {SOURCE_USERNAME!r} has read access."
            )

    records: List[Dict[str, Any]] = []
    for cl in raw_cls:
        cl_num   = cl["cl_number"]
        desc     = cl["description"]
        date_iso = _p4_date_to_iso(cl["date"])
        records.append({
            "source_id":              cl_num,
            "source_short_id":        f"CL-{cl_num}",
            "source_title":           desc.splitlines()[0][:255] if desc else "",
            "source_description":     desc,
            "source_author":          cl["user"],
            "source_author_email":    None,
            "source_authored_date":   date_iso,
            "source_committer":       cl["user"],
            "source_committer_email": None,
            "source_type":            "changelist",
            "source_repo_id":         MIGRATION_JOB_ID,
            "parent_ids":             [],
        })

    return records


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _log(f"Starting — depot={SOURCE_REPO_PATH} | out={OUTPUT_DIR}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    probe = os.path.join(OUTPUT_DIR, f".write_probe_{MIGRATION_JOB_ID}")
    with open(probe, "w") as pf:
        pf.write("ok")
    os.remove(probe)
    _log(f"Output dir writable: {OUTPUT_DIR}")

    _log("Fetching P4 changelists …")
    p4_cls = fetch_p4_changelists(SOURCE_REPO_PATH)
    _log(f"Fetched {len(p4_cls)} changelists")

    out_path = os.path.join(OUTPUT_DIR, f"{MIGRATION_JOB_ID}_p4_changelists.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(p4_cls, fh, indent=2, default=str)
    _log(f"Saved → {out_path} ({os.path.getsize(out_path)} bytes)")

    print(json.dumps({
        "status":            "completed",
        "p4_data_path":      out_path,
        "migration_job_id":  MIGRATION_JOB_ID,
        "total_changelists": len(p4_cls),
    }))


main()
