"""perforce_to_github_validation_inputs.py — Validate and forward all inputs for the P4→GitHub validation pipeline.

This is the FIRST script in the pipeline. It receives every configuration value,
validates and normalises them, then publishes them as output keys so that all
downstream scripts can read them via ``{{steps.perforce_to_github_validation_inputs.<key>}}``
without re-declaring any placeholder.

No branch input is declared here — branch discovery is handled automatically
by downstream scripts based on the cloned repository and P4 depot structure.

Placeholders:
  {{SOURCE_URL}}        — Perforce server address (e.g. "perforce.example.com:1666")
  {{SOURCE_USERNAME}}   — Perforce username
  {{SOURCE_PASSWORD}}   — Perforce password or login ticket
  {{SOURCE_REPO_PATH}}  — Depot path (e.g. "//depot/main/...")
  {{TARGET_TOKEN}}      — GitHub personal access token
  {{TARGET_URL}}        — GitHub server URL (e.g. "https://github.com")
  {{TARGET_API_URL}}    — GitHub API base URL (e.g. "https://api.github.com")
  {{TARGET_ORG}}        — Target GitHub organisation
  {{TARGET_REPO}}       — Target GitHub repository name
  {{MIGRATION_JOB_ID}}  — Depot/repo identifier used in file naming (e.g. "my-depot")
  {{EFS_MOUNT_PATH}}    — Shared EFS output directory (e.g. "/mnt/efs/scm_validation/job-abc-123")
                          Also accepted from the EFS_MOUNT_PATH environment variable.

Output keys available to downstream scripts via {{steps.perforce_to_github_validation_inputs.<key>}}:
  source_url, source_username, source_password, source_repo_path
  target_token, target_url, target_api_url
  target_org, target_repo
  output_dir, migration_job_id
"""
import json
import os
import sys

# ─── Inputs ────────────────────────────────────────────────────────────────
SOURCE_URL       = {{SOURCE_URL}}        # NOSONAR
SOURCE_USERNAME  = {{SOURCE_USERNAME}}   # NOSONAR
SOURCE_PASSWORD  = {{SOURCE_PASSWORD}}   # NOSONAR
SOURCE_REPO_PATH = {{SOURCE_REPO_PATH}}  # NOSONAR
TARGET_TOKEN     = {{TARGET_TOKEN}}      # NOSONAR
TARGET_URL       = {{TARGET_URL}}        # NOSONAR
TARGET_API_URL   = {{TARGET_API_URL}}    # NOSONAR
TARGET_ORG       = {{TARGET_ORG}}        # NOSONAR
TARGET_REPO      = {{TARGET_REPO}}       # NOSONAR
MIGRATION_JOB_ID = {{MIGRATION_JOB_ID}} # NOSONAR

# EFS_MOUNT_PATH: prefer the environment variable; fall back to the placeholder
# input so the workflow engine can pass it either way.
_EFS_ENV         = os.environ.get("EFS_MOUNT_PATH", "").strip()
_EFS_PLACEHOLDER = {{EFS_MOUNT_PATH}}    # NOSONAR
OUTPUT_DIR       = _EFS_ENV or (str(_EFS_PLACEHOLDER).strip() if _EFS_PLACEHOLDER else "")
# ───────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _require(value, name: str) -> None:
    if not value:
        raise RuntimeError(f"[perforce_to_github_validation_inputs] Required input '{name}' is missing or empty.")


def _require_https_url(value, name: str) -> None:
    _require(value, name)
    if not str(value).startswith("https://"):
        raise RuntimeError(
            f"[perforce_to_github_validation_inputs] '{name}' must start with 'https://', got: {value!r}"
        )


def _normalise_depot_path(raw: str) -> str:
    path = str(raw).strip()
    if not path.startswith("//"):
        path = "//" + path
    parts = path.lstrip("/").split("/", 1)
    if len(parts) == 1 or (len(parts) == 2 and parts[1].strip("/") in ("", "...")):
        path = f"//{parts[0]}/..."
    return path


def _normalise_output_dir(path: str) -> str:
    path = str(path).strip()
    if path and not path.startswith("/"):
        path = "/" + path
    return path


def _normalise_url(url: str) -> str:
    return str(url).rstrip("/")


def _derive_api_url(target_url: str, api_url_raw: str) -> str:
    """Return a valid GitHub API base URL.

    If TARGET_API_URL was left blank or set to the same value as TARGET_URL
    (a common misconfiguration when the workflow passes github.com as both),
    derive the correct API URL automatically:
      https://github.com          → https://api.github.com
      https://github.example.com  → https://github.example.com/api/v3
    """
    api = _normalise_url(api_url_raw)
    base = _normalise_url(target_url)

    # Blank or identical to TARGET_URL — auto-derive
    if not api or api == base:
        if base in ("https://github.com", "https://www.github.com"):
            api = "https://api.github.com"
        else:
            api = f"{base}/api/v3"
        print(
            f"[perforce_to_github_validation_inputs] TARGET_API_URL derived automatically: {api!r}",
            file=sys.stderr,
        )
    return api


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

print("[perforce_to_github_validation_inputs] Validating inputs …", file=sys.stderr)

_require(SOURCE_URL,       "SOURCE_URL")
_require(SOURCE_USERNAME,  "SOURCE_USERNAME")
_require(SOURCE_PASSWORD,  "SOURCE_PASSWORD")
_require(SOURCE_REPO_PATH, "SOURCE_REPO_PATH")
_require_https_url(TARGET_URL, "TARGET_URL")
_require(TARGET_TOKEN,     "TARGET_TOKEN")
_require(TARGET_ORG,       "TARGET_ORG")
_require(TARGET_REPO,      "TARGET_REPO")
_require(OUTPUT_DIR,       "EFS_MOUNT_PATH")
_require(MIGRATION_JOB_ID, "MIGRATION_JOB_ID")

# ---------------------------------------------------------------------------
# Normalise
# ---------------------------------------------------------------------------

source_repo_path = _normalise_depot_path(SOURCE_REPO_PATH)
output_dir       = _normalise_output_dir(OUTPUT_DIR)
target_url       = _normalise_url(TARGET_URL)
target_api_url   = _derive_api_url(target_url, str(TARGET_API_URL) if TARGET_API_URL else "")

if source_repo_path != str(SOURCE_REPO_PATH).strip():
    print(
        f"[perforce_to_github_validation_inputs] SOURCE_REPO_PATH normalised: {SOURCE_REPO_PATH!r} → {source_repo_path!r}",
        file=sys.stderr,
    )

print(
    f"[perforce_to_github_validation_inputs] Validation complete — "
    f"depot={source_repo_path} | repo={TARGET_ORG}/{TARGET_REPO} | out={output_dir}",
    file=sys.stderr,
)

# ---------------------------------------------------------------------------
# Output — downstream scripts read every key via {{steps.perforce_to_github_validation_inputs.<key>}}
# ---------------------------------------------------------------------------

print(json.dumps({
    "status":           "completed",
    "source_url":       str(SOURCE_URL),
    "source_username":  str(SOURCE_USERNAME),
    "source_password":  str(SOURCE_PASSWORD),
    "source_repo_path": source_repo_path,
    "target_token":     str(TARGET_TOKEN),
    "target_url":       target_url,
    "target_api_url":   target_api_url,
    "target_org":       str(TARGET_ORG),
    "target_repo":      str(TARGET_REPO),
    "output_dir":       output_dir,
    "migration_job_id": str(MIGRATION_JOB_ID),
}))
