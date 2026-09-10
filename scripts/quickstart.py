#!/usr/bin/env python3
"""One-time setup for local development and deployment.

Steps:
  1. Check the Databricks CLI is installed.
  2. Pick (or create) a CLI profile and verify it authenticates.
  3. Create or reuse an MLflow experiment for this app's traces.
  4. Write .env for local runs.
  5. Write the experiment id into databricks.yml so the app resource resolves.

Usage:
    uv run quickstart [--profile NAME] [--experiment-name PATH] [--yes]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
BUNDLE_PATH = ROOT / "databricks.yml"


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(args)}\n{result.stderr.strip()}")
    return result


def cli(profile: str | None, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["databricks", *args]
    if profile:
        command += ["--profile", profile]
    return run(command, check=check)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def check_prerequisites() -> None:
    if not shutil.which("databricks"):
        raise SystemExit(
            "The Databricks CLI is not installed.\n"
            "  https://docs.databricks.com/dev-tools/cli/install"
        )
    print("✓ Databricks CLI found")


def choose_profile(requested: str | None, assume_yes: bool) -> str:
    if requested:
        profile = requested
    else:
        listed = cli(None, "auth", "profiles", "--output", "json", check=False)
        profiles = []
        if listed.returncode == 0:
            try:
                profiles = [p["name"] for p in json.loads(listed.stdout).get("profiles", [])]
            except (json.JSONDecodeError, KeyError, TypeError):
                profiles = []
        if not profiles:
            raise SystemExit(
                "No Databricks CLI profiles found. Run `databricks auth login` first, "
                "then re-run with --profile <name>."
            )
        if len(profiles) == 1 or assume_yes:
            profile = profiles[0]
        else:
            print("Available profiles:")
            for index, name in enumerate(profiles, start=1):
                print(f"  {index}. {name}")
            choice = input("Choose a profile [1]: ").strip() or "1"
            profile = profiles[int(choice) - 1]

    check = cli(profile, "current-user", "me", "--output", "json", check=False)
    if check.returncode != 0:
        raise SystemExit(
            f"Profile {profile!r} does not authenticate.\n"
            f"Run: databricks auth login --profile {profile}\n{check.stderr.strip()}"
        )
    user = json.loads(check.stdout).get("userName", "unknown")
    print(f"✓ Authenticated as {user} (profile {profile})")
    return profile


def current_username(profile: str) -> str:
    return json.loads(cli(profile, "current-user", "me", "--output", "json").stdout)["userName"]


def ensure_experiment(profile: str, name: str) -> str:
    """Create the experiment, or reuse it if it already exists."""
    created = cli(
        profile,
        "experiments",
        "create-experiment",
        name,
        "--output",
        "json",
        check=False,
    )
    if created.returncode == 0:
        experiment_id = json.loads(created.stdout)["experiment_id"]
        print(f"✓ Created MLflow experiment {name} ({experiment_id})")
        return experiment_id

    existing = cli(
        profile,
        "experiments",
        "get-by-name",
        "--experiment-name",
        name,
        "--output",
        "json",
        check=False,
    )
    if existing.returncode == 0:
        experiment_id = json.loads(existing.stdout)["experiment"]["experiment_id"]
        print(f"✓ Reusing MLflow experiment {name} ({experiment_id})")
        return experiment_id

    raise SystemExit(f"Could not create or find the experiment {name}:\n{created.stderr.strip()}")


def write_env(profile: str, experiment_id: str) -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text(
            ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else "",
            encoding="utf-8",
        )
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updates = {
        "DATABRICKS_CONFIG_PROFILE": profile,
        "MLFLOW_EXPERIMENT_ID": experiment_id,
        "MLFLOW_TRACKING_URI": '"databricks"',
    }
    for key, value in updates.items():
        pattern = re.compile(rf"^#?\s*{key}=.*$")
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ Wrote {ENV_PATH.name}")


def write_experiment_id(experiment_id: str) -> None:
    """Fill `experiment_id: ""` in the app resource, preserving comments."""
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    except ImportError:
        raise SystemExit("ruamel.yaml is required: run `uv sync` first.") from None

    yaml = YAML()
    yaml.preserve_quotes = True
    with BUNDLE_PATH.open(encoding="utf-8") as handle:
        bundle = yaml.load(handle)

    apps = bundle.get("resources", {}).get("apps", {})
    updated = False
    for app in apps.values():
        for resource in app.get("resources", []):
            if "experiment" in resource:
                resource["experiment"]["experiment_id"] = DoubleQuotedScalarString(experiment_id)
                updated = True
    if not updated:
        print("! No experiment resource found in databricks.yml; skipping")
        return

    with BUNDLE_PATH.open("w", encoding="utf-8") as handle:
        yaml.dump(bundle, handle)
    print(f"✓ Set experiment_id in {BUNDLE_PATH.name}")


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Databricks CLI profile to use")
    parser.add_argument("--experiment-name", help="MLflow experiment path")
    parser.add_argument("--yes", action="store_true", help="Never prompt; take the first profile")
    args = parser.parse_args()

    check_prerequisites()
    profile = choose_profile(args.profile, args.yes)
    name = args.experiment_name or f"/Users/{current_username(profile)}/pr-review-agent"
    experiment_id = ensure_experiment(profile, name)
    write_env(profile, experiment_id)
    write_experiment_id(experiment_id)

    print(
        "\nSetup complete.\n"
        "  Run locally:  uv run start-server\n"
        "  Deploy:       databricks bundle deploy --target dev"
        f" --profile {profile}\n"
        "                databricks bundle run pr_review_agent --target dev"
        f" --profile {profile}\n"
    )


if __name__ == "__main__":
    sys.exit(main())
