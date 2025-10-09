import base64
import json
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)

CRON_JOB_ID = "web_cron_job"
DAILY_JOB_ID = "web_daily_job"
DEFAULT_SCHEDULE = {
    "minute": "0",
    "hour": "*",
    "day_of_month": "*",
    "month": "*",
    "day_of_week": "*",
}

scheduler = BackgroundScheduler()
_scheduler_lock = threading.Lock()
_scheduler_started = False

job_state: Dict[str, Any] = {
    "is_running": False,
    "current_trigger": None,
    "last_run_at": None,
    "last_success": None,
    "last_message": "",
    "job_enabled": False,
    "schedule": DEFAULT_SCHEDULE.copy(),
    "run_count": 0,
    "last_details": {},
}
_state_lock = threading.Lock()

schedule_state: Dict[str, Any] = {
    "cron": {
        "enabled": False,
        "schedule": DEFAULT_SCHEDULE.copy(),
        "next_run": None,
    },
    "daily": {
        "enabled": False,
        "time": None,
        "next_run": None,
    },
}


def _ensure_scheduler_started() -> None:
    """Start the APScheduler instance once."""
    global _scheduler_started  # pylint: disable=global-statement
    with _scheduler_lock:
        if _scheduler_started:
            return
        scheduler.add_listener(_job_event_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        scheduler.start()
        _scheduler_started = True


def _job_event_listener(event: JobExecutionEvent) -> None:
    """Fallback listener to reset state if an unhandled scheduler failure occurs."""
    if event.job_id not in {CRON_JOB_ID, DAILY_JOB_ID} or not event.exception:
        return

    now = datetime.utcnow().isoformat() + "Z"
    with _state_lock:
        job_state["is_running"] = False
        job_state["last_run_at"] = now
        job_state["last_success"] = False
        job_state["last_message"] = f"Unhandled scheduler error: {event.exception}"
        job_state["current_trigger"] = "scheduler"


def _mark_job_start(trigger: str) -> bool:
    """Set state to running if no job is currently active."""
    with _state_lock:
        if job_state["is_running"]:
            return False
        job_state["is_running"] = True
        job_state["current_trigger"] = trigger
    return True


def _finalize_job_run(success: bool, message: str, trigger: str, details: Optional[Dict[str, Any]]) -> None:
    """Persist the outcome of a job run."""
    now = datetime.utcnow().isoformat() + "Z"
    ending_message = message or ("Job completed successfully." if success else "Job finished.")

    with _state_lock:
        job_state["is_running"] = False
        job_state["last_run_at"] = now
        job_state["last_success"] = success
        job_state["last_message"] = ending_message
        job_state["current_trigger"] = trigger
        job_state["run_count"] += 1
        job_state["last_details"] = dict(details) if details else {}


def _normalize_schedule_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Merge request payload with defaults, ensuring cron fields are strings."""
    payload = payload or {}
    schedule = DEFAULT_SCHEDULE.copy()
    for key in schedule:
        value = payload.get(key, schedule[key])
        text = str(value).strip() if value is not None else schedule[key]
        schedule[key] = text or schedule[key]
    return schedule


def _schedule_cron_job(schedule_fields: Dict[str, str]) -> Optional[str]:
    """Create or update the cron-based scheduled job."""
    now = datetime.now()
    try:
        trigger = CronTrigger(
            month=schedule_fields["month"],
            day=schedule_fields["day_of_month"],
            day_of_week=schedule_fields["day_of_week"],
            hour=schedule_fields["hour"],
            minute=schedule_fields["minute"],
            second="0",
        )
    except ValueError as exc:
        raise ValueError(f"Invalid cron configuration: {exc}") from exc

    next_run_time = trigger.get_next_fire_time(None, now)
    if next_run_time is None:
        raise ValueError("Cron expression will never fire.")

    _ensure_scheduler_started()
    scheduler.add_job(
        _scheduled_job_run,
        trigger=trigger,
        id=CRON_JOB_ID,
        replace_existing=True,
        next_run_time=next_run_time,
        kwargs={"source": "cron"},
    )

    with _state_lock:
        schedule_state["cron"]["enabled"] = True
        schedule_state["cron"]["schedule"] = schedule_fields.copy()
        schedule_state["cron"]["next_run"] = next_run_time.isoformat()

    app.logger.info("Scheduled cron job with fields=%s next_run=%s", schedule_fields, next_run_time)
    return next_run_time.isoformat()


def _schedule_daily_job(daily_time_value: str) -> Optional[str]:
    """Create or update the daily scheduled job."""
    now = datetime.now()
    try:
        parsed_time = datetime.strptime(daily_time_value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid time format: {daily_time_value}") from exc

    target_next_run = now.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
    if target_next_run <= now:
        target_next_run += timedelta(days=1)

    trigger = CronTrigger(hour=str(parsed_time.hour), minute=str(parsed_time.minute), second="0")

    _ensure_scheduler_started()
    scheduler.add_job(
        _scheduled_job_run,
        trigger=trigger,
        id=DAILY_JOB_ID,
        replace_existing=True,
        next_run_time=target_next_run,
        kwargs={"source": "daily"},
    )

    next_iso = target_next_run.isoformat()
    with _state_lock:
        schedule_state["daily"]["enabled"] = True
        schedule_state["daily"]["time"] = daily_time_value
        schedule_state["daily"]["next_run"] = next_iso

    app.logger.info("Scheduled daily job for %s next_run=%s", daily_time_value, target_next_run)
    return next_iso


def _disable_schedule(schedule_type: str) -> None:
    """Disable a schedule by type ('cron' or 'daily')."""
    job_id = CRON_JOB_ID if schedule_type == "cron" else DAILY_JOB_ID

    if _scheduler_started:
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    with _state_lock:
        target_state = schedule_state.get(schedule_type)
        if target_state is not None:
            target_state["enabled"] = False
            if schedule_type == "cron":
                target_state["next_run"] = None
            else:
                target_state["next_run"] = None


def _get_scheduler_status() -> Dict[str, Any]:
    """Return a snapshot of scheduler and job state."""
    with _state_lock:
        snapshot = {
            "is_running": job_state["is_running"],
            "current_trigger": job_state["current_trigger"],
            "last_run_at": job_state["last_run_at"],
            "last_success": job_state["last_success"],
            "last_message": job_state["last_message"],
            "run_count": job_state["run_count"],
            "last_details": job_state["last_details"].copy(),
            "cron": {
                "enabled": schedule_state["cron"]["enabled"],
                "schedule": schedule_state["cron"]["schedule"].copy(),
                "next_run": schedule_state["cron"]["next_run"],
            },
            "daily": {
                "enabled": schedule_state["daily"]["enabled"],
                "time": schedule_state["daily"]["time"],
                "next_run": schedule_state["daily"]["next_run"],
            },
        }

    snapshot["scheduler_running"] = _scheduler_started and scheduler.running
    snapshot["server_time"] = datetime.now().isoformat()

    if _scheduler_started:
        cron_job = scheduler.get_job(CRON_JOB_ID)
        daily_job = scheduler.get_job(DAILY_JOB_ID)
        if cron_job and cron_job.next_run_time:
            snapshot["cron"]["next_run"] = cron_job.next_run_time.isoformat()
        if daily_job and daily_job.next_run_time:
            snapshot["daily"]["next_run"] = daily_job.next_run_time.isoformat()

    return snapshot


def _execute_job_task() -> Dict[str, Any]:
    """Execute the cron job task by invoking an AWS Lambda function."""
    function_name = (os.environ.get("LAMBDA_FUNCTION_NAME") or "").strip()
    payload = (os.environ.get("LAMBDA_PAYLOAD") or "{}").strip() or "{}"
    aws_cli = (os.environ.get("AWS_CLI_PATH") or "aws").strip() or "aws"
    log_type = (os.environ.get("LAMBDA_LOG_TYPE") or "Tail").strip()

    if not function_name:
        message = "Environment variable LAMBDA_FUNCTION_NAME must be set."
        return {
            "success": False,
            "message": message,
            "details": {"error": message},
            "status_code": 400,
        }

    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        output_path = tmp_file.name

    command = [
        aws_cli,
        "lambda",
        "invoke",
        "--function-name",
        function_name,
        "--cli-binary-format",
        "raw-in-base64-out",
        "--payload",
        payload,
    ]

    if log_type:
        command.extend(["--log-type", log_type])

    command.append(output_path)

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()

    try:
        with open(output_path, "r", encoding="utf-8") as response_file:
            raw_response = response_file.read().strip()
    except OSError:
        raw_response = ""
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass

    metadata: Dict[str, Any] = {}
    if stdout:
        try:
            metadata = json.loads(stdout)
        except json.JSONDecodeError:
            metadata = {"raw_stdout": stdout}

    log_output = None
    if isinstance(metadata, dict) and metadata.get("LogResult"):
        try:
            log_output = base64.b64decode(metadata["LogResult"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            log_output = metadata["LogResult"]

    success = completed.returncode == 0
    message = (
        f"Invocation succeeded for {function_name}."
        if success
        else f"Invocation failed for {function_name}."
    )

    details: Dict[str, Any] = {
        "function_name": function_name,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": completed.returncode,
        "function_response": raw_response or "(no payload returned)",
    }

    if log_output:
        details["log_result"] = log_output
    if metadata and "raw_stdout" not in metadata:
        details["metadata"] = metadata

    status_code = 200 if success else 500

    return {
        "success": success,
        "message": message,
        "details": details,
        "status_code": status_code,
    }


def _run_job(trigger: str) -> Dict[str, Any]:
    """Run the cron job and capture its outcome."""
    if not _mark_job_start(trigger):
        return {
            "success": False,
            "message": "Job is already running.",
            "status_code": 409,
            "details": {"error": "Job is already running."},
        }

    success = False
    message = ""
    status_code = 500
    details: Dict[str, Any] = {}

    try:
        result = _execute_job_task()
        success = bool(result.get("success"))
        message = result.get("message") or ("Job completed successfully." if success else "Job finished with issues.")
        details = result.get("details") or {}
        status_code = result.get("status_code", 200 if success else 500)
        return {
            "success": success,
            "message": message,
            "details": details,
            "status_code": status_code,
        }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        message = f"Job failed with unexpected error: {exc}"
        details = {"error": str(exc)}
        return {
            "success": False,
            "message": message,
            "details": details,
            "status_code": 500,
        }
    finally:
        _finalize_job_run(success, message, trigger, details)


def _scheduled_job_run(source: str) -> None:
    """Background job entrypoint for APScheduler."""
    result = _run_job(source or "scheduler")
    if result.get("status_code") == 409:
        app.logger.info("Scheduled run skipped: %s", result.get("message"))
        return

    status = "success" if result.get("success") else "failure"
    app.logger.info("Scheduled run finished with %s", status)
    if not result.get("success"):
        app.logger.error("Scheduled run failed: %s", result.get("message"))
    with _state_lock:
        if source in {"cron", "daily"} and _scheduler_started:
            job_id = CRON_JOB_ID if source == "cron" else DAILY_JOB_ID
            job = scheduler.get_job(job_id)
            next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
            schedule_state[source]["next_run"] = next_run


@app.route("/", methods=["GET"])
def index() -> str:
    return render_template("index.html")


@app.route("/job/run", methods=["POST"])
def run_job():
    result = _run_job("manual")
    status_code = result.pop("status_code", 200)
    return jsonify(result), status_code


@app.route("/job/status", methods=["GET"])
def job_status():
    return jsonify(_get_scheduler_status())


@app.route("/job/schedule", methods=["POST"])
def update_schedule():
    payload = request.get_json(silent=True) or {}
    schedule_fields = _normalize_schedule_payload(payload)
    try:
        next_run_time = _schedule_cron_job(schedule_fields)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    response = _get_scheduler_status()
    response["cron_next_run"] = next_run_time
    response["success"] = True
    return jsonify(response)


@app.route("/job/daily", methods=["POST"])
def update_daily_schedule():
    payload = request.get_json(silent=True) or {}
    raw_time = str(payload.get("time", "")).strip()

    if not raw_time:
        return jsonify({"success": False, "error": "Daily time value is required (HH:MM)."}), 400

    try:
        parsed_time = datetime.strptime(raw_time, "%H:%M").time()
    except ValueError as exc:
        return jsonify({"success": False, "error": f"Invalid time format: {raw_time}"}), 400

    schedule_fields = DEFAULT_SCHEDULE.copy()
    schedule_fields.update(
        {
            "minute": str(parsed_time.minute),
            "hour": str(parsed_time.hour),
        }
    )

    daily_value = parsed_time.strftime("%H:%M")

    try:
        next_run_time = _schedule_daily_job(daily_value)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    response = _get_scheduler_status()
    response["daily_next_run"] = next_run_time
    response["success"] = True
    return jsonify(response)


@app.route("/job/start", methods=["POST"])
def start_scheduler():
    payload = request.get_json(silent=True) or {}
    schedule_type = payload.get("type")

    if schedule_type not in {"cron", "daily", None}:
        return jsonify({"success": False, "error": "Unknown schedule type."}), 400

    responses = {}

    if schedule_type in (None, "cron"):
        with _state_lock:
            cron_config = schedule_state["cron"]["schedule"].copy()
        try:
            responses["cron_next_run"] = _schedule_cron_job(cron_config)
        except ValueError as exc:
            if schedule_type == "cron":
                return jsonify({"success": False, "error": str(exc)}), 400
            responses["cron_error"] = str(exc)

    if schedule_type in (None, "daily"):
        with _state_lock:
            daily_time_value = schedule_state["daily"]["time"]
        if daily_time_value:
            try:
                responses["daily_next_run"] = _schedule_daily_job(daily_time_value)
            except ValueError as exc:
                if schedule_type == "daily":
                    return jsonify({"success": False, "error": str(exc)}), 400
                responses["daily_error"] = str(exc)
        elif schedule_type == "daily":
            return jsonify({"success": False, "error": "No daily time configured yet."}), 400

    response = _get_scheduler_status()
    response.update(responses)
    response["success"] = True
    return jsonify(response)


@app.route("/job/stop", methods=["POST"])
def stop_scheduler():
    payload = request.get_json(silent=True) or {}
    schedule_type = payload.get("type")

    if schedule_type in (None, "cron"):
        _disable_schedule("cron")
    if schedule_type in (None, "daily"):
        _disable_schedule("daily")

    response = _get_scheduler_status()
    response["success"] = True
    return jsonify(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8081")), debug=True)
