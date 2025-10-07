import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

load_dotenv()

app = Flask(__name__)


def _run_command(command: List[str], cwd: Optional[str] = None) -> Dict[str, str]:
    """Execute a shell command and capture stdout/stderr."""
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "command": " ".join(command),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "returncode": completed.returncode,
    }


def _zip_directory(source_dir: str, output_path: str) -> None:
    """Create a zip archive from source_dir, excluding the .git directory."""
    if os.path.exists(output_path):
        os.remove(output_path)

    with tempfile.TemporaryDirectory() as staging_dir:
        staging_path = os.path.join(staging_dir, "payload")
        shutil.copytree(source_dir, staging_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
        base_name = output_path[:-4] if output_path.endswith(".zip") else output_path
        shutil.make_archive(base_name, "zip", root_dir=staging_path)


@app.route("/", methods=["GET"])
def index() -> str:
    return render_template("index.html")


@app.route("/deploy", methods=["POST"])
def deploy():
    repo_path = os.environ.get("REPO_PATH", "/root/tobi")
    lambda_function_name = os.environ.get("LAMBDA_FUNCTION_NAME")
    output_zip = os.environ.get("LAMBDA_PACKAGE_PATH", os.path.join(repo_path, "lambda_bundle.zip"))

    steps: List[Dict[str, str]] = []

    if not lambda_function_name:
        steps.append(
            {
                "command": "validate environment",
                "stdout": "",
                "stderr": "Environment variable LAMBDA_FUNCTION_NAME must be set.",
                "returncode": 1,
            }
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Environment variable LAMBDA_FUNCTION_NAME must be set.",
                    "steps": steps,
                }
            ),
            400,
        )

    # Step 1: git pull
    git_remote = os.environ.get("GIT_REMOTE", "origin")
    git_branch = os.environ.get("GIT_BRANCH", "main")

    steps.append(_run_command(["git", "pull", git_remote, git_branch], cwd=repo_path))

    if steps[-1]["returncode"] != 0:
        return jsonify({"success": False, "steps": steps}), 500

    # Step 2: package repository
    error: Optional[Dict[str, str]] = None
    try:
        _zip_directory(repo_path, output_zip)
        steps.append(
            {
                "command": f"zip -> {output_zip}",
                "stdout": "Created deployment package.",
                "stderr": "",
                "returncode": 0,
            }
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        error = {
            "command": "zip",
            "stdout": "",
            "stderr": str(exc),
            "returncode": 1,
        }
        steps.append(error)

    if error:
        return jsonify({"success": False, "steps": steps}), 500

    # Step 3: aws lambda update-function-code
    aws_command = [
        "aws",
        "lambda",
        "update-function-code",
        "--function-name",
        lambda_function_name,
        "--zip-file",
        f"fileb://{output_zip}",
    ]
    steps.append(_run_command(aws_command))

    status_code = 200 if steps[-1]["returncode"] == 0 else 500
    return jsonify({"success": status_code == 200, "steps": steps}), status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
