# Lambda Deployment Web UI

This project provides a minimal Flask application that runs `git pull` on a target repository and then deploys the bundle to an AWS Lambda function via the AWS CLI. Trigger the workflow from a single button in the web UI.

## Requirements

- Python 3.9+
- AWS CLI configured with credentials that can update the target Lambda function
- Network access from the host running this app to AWS

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Configuration

Set the following environment variables before starting the server (values placed in a `.env` file are loaded automatically):

- `LAMBDA_FUNCTION_NAME` **(required)** – name of the Lambda function to update.
- `REPO_PATH` – path to the Git repository to pull from. Defaults to `/root/tobi`.
- `LAMBDA_PACKAGE_PATH` – absolute path for the generated deployment zip. Defaults to `<REPO_PATH>/lambda_bundle.zip`.
- `GIT_REMOTE` – remote to pull from. Defaults to `origin`.
- `GIT_BRANCH` – branch to pull. Defaults to `main`.
- `PORT` – port that Flask should listen on. Defaults to `8080`.

The application assumes the AWS CLI is installed and on the `PATH`.

## Run

```bash
export LAMBDA_FUNCTION_NAME=my-lambda
export REPO_PATH=/root/tobi
flask --app app run --host 0.0.0.0 --port 8080
```

Then open `http://localhost:8080` and click **Deploy**. A modal log will show the output of each step (git pull, packaging, and AWS CLI update).

> **Note:** `aws lambda update-function-code` replaces the entire code package for the Lambda function. AWS automatically extracts the uploaded zip to refresh `/var/task`, so there is no need to manually delete old files or run an additional unzip step.
