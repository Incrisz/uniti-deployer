# Lambda Cron Dashboard

A lightweight Flask application that exposes a browser-based dashboard for managing a cron-style background job powered by APScheduler. Each run invokes an AWS Lambda function through the AWS CLI, captures the response, and streams the output back to the UI so you can monitor executions without leaving the browser.

## Requirements

- Python 3.9+
- AWS CLI installed and configured with credentials that can invoke the target Lambda function
- APScheduler dependencies (installed via `requirements.txt`)
- PostgreSQL database available for persisting scheduler state

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Configuration

Environment variables (a `.env` file is automatically loaded):

- `PORT` – port the Flask server listens on. Defaults to `8081`.
- `LAMBDA_FUNCTION_NAME` – name of the Lambda function to invoke. Defaults to `firebase-pull-usage`.
- `LAMBDA_PAYLOAD` – JSON payload sent with each invocation. Defaults to `{}`.
- `LAMBDA_LOG_TYPE` – value for `--log-type`. Defaults to `Tail` so the last 4 KB of logs appear in the UI.
- `AWS_CLI_PATH` – optional path to the AWS CLI binary. Defaults to `aws`.
- `DATABASE_URL` – PostgreSQL connection string used to persist scheduler configuration and job state. When omitted the app falls back to in-memory state (changes are lost on restart).
- `APP_USERNAME` / `APP_PASSWORD` – optional static credentials for the login screen. Both default to `admin`.

## Run

```bash
python app.py
```

Open `http://localhost:8081` to manage the scheduler:

- **Save & Start Cron** persists the cron expression and enables that recurring schedule.
- **Save & Start Daily** sets a single daily run time in 24-hour format (e.g., `21:00` for 9 PM) that can run alongside the cron schedule.
- **Stop Cron / Stop Daily** let you pause either schedule independently without losing its configuration.
- **Run Job Now** executes the Lambda immediately, independent of the schedule.
- The **Status** panel displays the current server time, cron/daily next-run timestamps, the latest Lambda response payload, AWS CLI stdout/stderr, and the decoded CloudWatch log tail when available.
- Default login is `admin` / `admin` unless you override it with the environment variables above.

To customise behaviour beyond the payload, edit `_execute_job_task` in `app.py` to adjust AWS CLI arguments or add pre/post hooks. APScheduler runs inside the Flask process, so use a process manager (Gunicorn, systemd, etc.) in production to keep the scheduler alive.
