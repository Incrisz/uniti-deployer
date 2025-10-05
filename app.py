from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from flask import Flask, render_template, request, url_for

app = Flask(__name__)

REPO_PATH = Path("/root/uniti-model-service")


@dataclass
class CommandResult:
    label: str
    command: str
    returncode: int
    stdout: str
    stderr: str


def run_command(label: str, args: List[str]) -> CommandResult:
    """Run the given command inside the model service repo."""
    process = subprocess.run(
        args,
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        label=label,
        command=" ".join(args),
        returncode=process.returncode,
        stdout=process.stdout.strip(),
        stderr=process.stderr.strip(),
    )


def git_capture(args: List[str]) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        return f"(git {' '.join(args)} failed)\n{process.stderr.strip()}"
    return process.stdout.strip()


@app.route("/deploy", methods=["GET", "POST"])
def deploy() -> str:
    if request.method == "GET":
        return render_template("deploy_form.html", repo_path=REPO_PATH)

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

    commands = [
        ("Stop service", ["make", "down"]),
        ("Pull latest changes", ["git", "pull"]),
        ("Start service", ["make", "up"]),
    ]

    results: List[CommandResult] = []
    success = True
    for label, args in commands:
        result = run_command(label, args)
        results.append(result)
        if result.returncode != 0:
            success = False
            break

    git_summary: Dict[str, str] = {
        "branch": git_capture(["rev-parse", "--abbrev-ref", "HEAD"]),
        "latest_commit": git_capture(["log", "-1", "--pretty=format:%h %s"]),
        "changes": git_capture(["status", "--short"]),
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
