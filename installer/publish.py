r"""Builds an update of the installed program and publishes it to your GitHub project, where the app on your other
computers finds it. Usage:  publish_update.bat "what changed"     (or --dry-run to only build the package)"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

updater = build.updater


def default_notes() -> str:
    try:
        out = subprocess.run(["git", "log", "-1", "--pretty=%s"], capture_output=True, text=True, cwd=build.ROOT, timeout=10)
        return out.stdout.strip() or "Updated."
    except (OSError, subprocess.SubprocessError):
        return "Updated."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Publish an update to your other computers.")
    parser.add_argument("notes", nargs="?", default="", help="what changed (shown before installing)")
    parser.add_argument("--dry-run", action="store_true", help="build and save the package but don't publish it")
    parser.add_argument("--branch", default=updater.BRANCH, help="branch to publish to (default: %(default)s)")
    args = parser.parse_args(argv)

    baseline_path = build.RELEASE / "baseline.json"
    if not baseline_path.exists():
        print("There is no installer baseline yet. Run build_installer.bat once first, install that on your other\n"
              "computers, and after that publish_update.bat can update them.")
        return 1
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    state_path = build.RELEASE / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"ever_changed": [], "published": []}
    private = updater.load_private_key(build.RELEASE / "update_signing_key.pem")

    number = build.new_build_number()
    repo = build.repo_name()
    info = updater.BuildInfo(build.app_version(), number, baseline["base"], repo)
    app_dir = build.build_app(info)
    notes = args.notes.strip() or default_notes()
    package, manifest, ever_changed = updater.build_package(app_dir, baseline["files"], info, notes, set(state["ever_changed"]))
    print(f"\nUpdate {info.version} build {number}: {len(manifest['included'])} changed files, {len(package) / 1e6:.1f} MB")

    if args.dry_run:
        out = build.RELEASE / f"update-{number}.zip"
        out.write_bytes(package)
        print(f"Saved {out} (not published).")
        return 0

    updater.publish(package, manifest, private, f"https://github.com/{repo}.git", args.branch)
    state["ever_changed"] = sorted(ever_changed)
    state["published"].append({"build": number, "version": info.version, "notes": notes})
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(f"Published to github.com/{repo} (branch {args.branch}). Your other computers will offer it the next time they check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
