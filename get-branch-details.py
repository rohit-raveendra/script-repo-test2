#!/usr/bin/env python3

import argparse
import json
import subprocess
from typing import Any, Dict, List


def run_git_command(args: List[str]) -> str:
	"""Run a git command and return stdout."""
	try:
		result = subprocess.run(
			["git", *args],
			check=True,
			text=True,
			capture_output=True,
		)
		return result.stdout
	except subprocess.CalledProcessError as exc:
		stderr = (exc.stderr or "").strip()
		msg = stderr if stderr else str(exc)
		raise RuntimeError(f"Failed to run git command: {' '.join(args)}\n{msg}") from exc


def get_repo_name() -> str:
	"""Get the repository name from the remote URL, fallback to current folder name."""
	try:
		remote_url = run_git_command(["remote", "get-url", "origin"]).strip()
		name = remote_url.rstrip("/").split("/")[-1]
		if name.endswith(".git"):
			name = name[:-4]
		return name
	except RuntimeError:
		return run_git_command(["rev-parse", "--show-toplevel"]).strip().split("/")[-1]


def get_current_branch() -> str:
	"""Return the currently checked out branch."""
	return run_git_command(["branch", "--show-current"]).strip()


def get_branch_details(include_remote: bool = True) -> List[Dict[str, Any]]:
	"""Return details for branches in the current git repository."""
	refs = ["refs/heads"]
	if include_remote:
		refs.append("refs/remotes")

	fmt = "|".join(
		[
			"%(refname)",
			"%(refname:short)",
			"%(objectname)",
			"%(authorname)",
			"%(authoremail)",
			"%(authordate:iso8601)",
			"%(subject)",
			"%(upstream:short)",
			"%(upstream:trackshort)",
		]
	)

	output = run_git_command(["for-each-ref", f"--format={fmt}", "--sort=-authordate", *refs])

	details: List[Dict[str, Any]] = []
	for line in output.splitlines():
		if not line.strip():
			continue

		parts = line.split("|", maxsplit=8)
		if len(parts) != 9:
			continue

		refname = parts[0]
		branch_name = parts[1]
		if branch_name.endswith("/HEAD"):
			continue
		if refname.startswith("refs/remotes/") and branch_name.count("/") < 2:
			continue

		is_remote = refname.startswith("refs/remotes/")
		details.append(
			{
				"name": branch_name,
				"type": "remote" if is_remote else "local",
				"latest_commit": {
					"sha": parts[2],
					"author": parts[3],
					"email": parts[4],
					"date": parts[5],
					"message": parts[6],
				},
				"upstream": parts[7] or None,
				"tracking_status": parts[8] or None,
			}
		)

	return details


def main() -> None:
	parser = argparse.ArgumentParser(description="Get branch details from the current git repository.")
	parser.add_argument(
		"--local-only",
		action="store_true",
		help="Include only local branches.",
	)
	parser.add_argument(
		"--pretty",
		action="store_true",
		help="Pretty-print JSON output.",
	)
	args = parser.parse_args()

	result = {
		"repository": get_repo_name(),
		"current_branch": get_current_branch(),
		"branches": get_branch_details(include_remote=not args.local_only),
	}

	if args.pretty:
		print(json.dumps(result, indent=2))
	else:
		print(json.dumps(result))


if __name__ == "__main__":
	main()
