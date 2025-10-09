import os
from typing import Dict, List

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

DEFAULT_PROFILE = os.environ.get("AWS_PROFILE", "default")
DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _configure(field: str, value: str, profile: str) -> Dict[str, str]:
    from subprocess import run, CalledProcessError

    if not value:
        return {
            "command": f"aws configure set {field} <empty> --profile {profile}",
            "stdout": "skipped empty value",
            "stderr": "",
            "returncode": 0,
        }

    command = [
        "aws",
        "configure",
        "set",
        field,
        value,
        "--profile",
        profile,
    ]
    completed = run(command, capture_output=True, text=True, check=False)
    return {
        "command": " ".join(command),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "returncode": completed.returncode,
    }


@app.route("/", methods=["GET"])
def index() -> str:
    return render_template("index.html", default_profile=DEFAULT_PROFILE, default_region=DEFAULT_REGION)


@app.route("/aws/credentials", methods=["POST"])
def aws_credentials():
    payload = request.get_json(silent=True) or {}
    profile = (payload.get("profile") or DEFAULT_PROFILE).strip()
    access_key = (payload.get("aws_access_key_id") or "").strip()
    secret_key = (payload.get("aws_secret_access_key") or "").strip()
    session_token = (payload.get("aws_session_token") or "").strip()
    region = (payload.get("region") or DEFAULT_REGION).strip()

    if not access_key or not secret_key:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "aws_access_key_id and aws_secret_access_key are required.",
                    "steps": [],
                }
            ),
            400,
        )

    steps: List[Dict[str, str]] = []
    steps.append(_configure("aws_access_key_id", access_key, profile))
    steps.append(_configure("aws_secret_access_key", secret_key, profile))

    if session_token:
        steps.append(_configure("aws_session_token", session_token, profile))

    steps.append(_configure("region", region, profile))

    success = all(step["returncode"] == 0 for step in steps)
    message = "AWS credentials updated successfully." if success else "Failed to update one or more AWS fields."

    return jsonify({"success": success, "message": message, "steps": steps, "profile": profile}), 200 if success else 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8082")), debug=True)
