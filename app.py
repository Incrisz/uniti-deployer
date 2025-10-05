from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, render_template, request, url_for

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = Path("/root/uniti-model-service")
SCRIPT_PATH = BASE_DIR / "deploy_pipeline.sh"
DEFAULT_PATH_SEGMENTS = [
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
    "/snap/bin",
]
REQUIRED_BINARIES = ("git",)


@dataclass
class CommandResult:
    label: str
    command: str
    returncode: int
    stdout: str
    stderr: str


def build_execution_env(extra_path: Optional[str] = None) -> Dict[str, str]:
    env = os.environ.copy()
    existing_paths = env.get("PATH", "")
    segments: List[str] = []
    for segment in DEFAULT_PATH_SEGMENTS + existing_paths.split(":"):
        if segment and segment not in segments:
            segments.append(segment)
    if extra_path:
        for segment in extra_path.split(":"):
            if segment and segment not in segments:
                segments.insert(0, segment)
    env["PATH"] = ":".join(segments)
    return env


def find_missing_binaries(env: Dict[str, str]) -> List[str]:
    missing: List[str] = []
    search_path = env.get("PATH", "")
    for binary in REQUIRED_BINARIES:
        if shutil.which(binary, path=search_path) is None:
            missing.append(binary)
    return missing


def run_command(
    label: str,
    command: str,
    env: Optional[Dict[str, str]] = None,
) -> CommandResult:
    """Run the given command inside the model service repo using bash for PATH loading."""
    exec_env = env or build_execution_env()
    try:
        process = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            check=False,
            env=exec_env,
        )
        stdout = process.stdout.strip()
        stderr = process.stderr.strip()
        returncode = process.returncode
    except FileNotFoundError as exc:
        stdout = ""
        stderr = f"Command not found while running '{command}': {exc}"
        returncode = -1

    return CommandResult(
        label=label,
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def git_capture(args: List[str], env: Optional[Dict[str, str]] = None) -> str:
    exec_env = env or build_execution_env()
    try:
        process = subprocess.run(
            ["git", *args],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            check=False,
            env=exec_env,
        )
    except FileNotFoundError as exc:
        return f"(git not available: {exc})"

    if process.returncode != 0:
        return f"(git {' '.join(args)} failed)\n{process.stderr.strip()}"
    return process.stdout.strip()


@app.route("/")
def index() -> str:
    return render_template("index.html", deploy_url=url_for("deploy"))


@app.route("/deploy", methods=["GET", "POST"])
def deploy() -> str:
    if request.method == "GET":
        return render_template(
            "deploy_form.html",
            repo_path=REPO_PATH,
            script_path=SCRIPT_PATH,
        )

    confirm = request.form.get("confirm")
    if confirm != "yes":
        return render_template(
            "deploy_result.html",
            success=False,
            message="Deployment cancelled",
            command_results=[],
            git_summary={},
            back_url=url_for("deploy"),
        )

    if not REPO_PATH.exists():
        return render_template(
            "deploy_result.html",
            success=False,
            message=f"Repository path {REPO_PATH} not found",
            command_results=[],
            git_summary={},
            back_url=url_for("deploy"),
        )

    if not SCRIPT_PATH.exists():
        return render_template(
            "deploy_result.html",
            success=False,
            message=f"Deployment script {SCRIPT_PATH} not found",
            command_results=[],
            git_summary={},
            back_url=url_for("deploy"),
        )

    exec_env = build_execution_env()
    missing = find_missing_binaries(exec_env)
    if missing:
        missing_list = ", ".join(missing)
        return render_template(
            "deploy_result.html",
            success=False,
            message=(
                "Deployment prerequisites missing: "
                f"{missing_list}. Ensure these binaries are installed and on the PATH "
                "for the service user."
            ),
            command_results=[],
            git_summary={},
            back_url=url_for("deploy"),
        )

    commands = [
        ("Run deployment script", f"bash \"{SCRIPT_PATH}\""),
    ]

    results: List[CommandResult] = []
    success = True
    for label, command in commands:
        result = run_command(label, command, env=exec_env)
        results.append(result)
        if result.returncode != 0:
            success = False
            break

    git_summary: Dict[str, str] = {
        "branch": git_capture(["rev-parse", "--abbrev-ref", "HEAD"], env=exec_env),
        "latest_commit": git_capture(["log", "-1", "--pretty=format:%h %s"], env=exec_env),
        "changes": git_capture(["status", "--short"], env=exec_env),
    }

    message = "Deployment complete" if success else "Deployment failed"
    return render_template(
        "deploy_result.html",
        success=success,
        message=message,
        command_results=results,
        git_summary=git_summary,
        back_url=url_for("deploy"),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
