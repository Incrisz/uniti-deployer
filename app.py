import base64
import json
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Dict, Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, redirect, session, url_for
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

app = Flask(__name__)
app.secret_key = (
    os.environ.get("FLASK_SECRET_KEY")
    or os.environ.get("SECRET_KEY")
    or "dev-secret-key"
)

STATIC_USERNAME = os.environ.get("APP_USERNAME", "admin")
STATIC_PASSWORD = os.environ.get("APP_PASSWORD", "admin")
SESSION_AUTH_FLAG = "user_authenticated"
SESSION_USER_KEY = "username"

DATABASE_URL = (os.environ.get("DATABASE_URL") or os.environ.get("DB_URL") or "").strip()
metadata = sa.MetaData()
_engine: Optional[Engine] = None
_db_warning_logged = False

cron_jobs_table = sa.Table(
    "cron_jobs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("job_key", sa.String(50), nullable=False, unique=True),
    sa.Column("schedule_type", sa.String(20), nullable=False),
    sa.Column("minute", sa.String(16), nullable=False, server_default="0"),
    sa.Column("hour", sa.String(16), nullable=False, server_default="*"),
    sa.Column("day_of_month", sa.String(16), nullable=False, server_default="*"),
    sa.Column("month", sa.String(16), nullable=False, server_default="*"),
    sa.Column("day_of_week", sa.String(16), nullable=False, server_default="*"),
    sa.Column("daily_time", sa.String(16)),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("next_run", sa.DateTime(timezone=True)),
    sa.Column("next_run_display", sa.String(64)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at_display", sa.String(64)),
)

job_status_table = sa.Table(
    "cron_job_status",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("is_running", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("current_trigger", sa.String(32)),
    sa.Column("last_run_at", sa.DateTime(timezone=True)),
    sa.Column("last_run_display", sa.String(64)),
    sa.Column("last_success", sa.Boolean),
    sa.Column("last_message", sa.Text),
    sa.Column("run_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_details", sa.Text),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at_display", sa.String(64)),
)

LOCAL_TZ = datetime.now().astimezone().tzinfo

CRON_JOB_ID = "web_cron_job"
DAILY_JOB_ID = "web_daily_job"
DEFAULT_SCHEDULE = {
    "minute": "0",
    "hour": "*",
    "day_of_month": "*",
    "month": "*",
    "day_of_week": "*",
}

scheduler = BackgroundScheduler(timezone=LOCAL_TZ)
_scheduler_lock = threading.Lock()
_scheduler_started = False

job_state: Dict[str, Any] = {
    "is_running": False,
    "current_trigger": None,
    "last_run_at": None,
    "last_run_display": None,
    "last_success": None,
    "last_message": "",
    "run_count": 0,
    "last_details": {},
}
_state_lock = threading.Lock()

schedule_state: Dict[str, Any] = {
    "cron": {
        "enabled": False,
        "schedule": DEFAULT_SCHEDULE.copy(),
        "next_run": None,
        "next_run_display": None,
    },
    "daily": {
        "enabled": False,
        "time": None,
        "next_run": None,
        "next_run_display": None,
    },
}


def _get_engine() -> Optional[Engine]:
    """Create (or reuse) a SQLAlchemy engine for the configured database."""
    global _engine  # pylint: disable=global-statement
    if _engine is not None:
        return _engine
    if not DATABASE_URL:
        return None
    try:
        _engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to initialise database engine: {0}".format(exc))
        _engine = None
    return _engine


def _ensure_seed_rows(db_engine: Engine) -> None:
    """Ensure baseline rows exist for cron/daily schedules and status tracking."""
    now = datetime.now(LOCAL_TZ)
    cron_defaults = DEFAULT_SCHEDULE.copy()
    with db_engine.begin() as connection:
        for job_key, schedule_type in (("cron", "cron"), ("daily", "daily")):
            exists = connection.execute(
                sa.select(cron_jobs_table.c.id).where(cron_jobs_table.c.job_key == job_key)
            ).first()
            if exists:
                continue
            values = {
                "job_key": job_key,
                "schedule_type": schedule_type,
                "minute": cron_defaults["minute"],
                "hour": cron_defaults["hour"],
                "day_of_month": cron_defaults["day_of_month"],
                "month": cron_defaults["month"],
                "day_of_week": cron_defaults["day_of_week"],
                "daily_time": None,
                "enabled": False,
                "next_run": None,
                "next_run_display": None,
                "updated_at": now,
                "updated_at_display": _format_display_time(now),
            }
            connection.execute(sa.insert(cron_jobs_table).values(**values))

        status_exists = connection.execute(
            sa.select(job_status_table.c.id).where(job_status_table.c.id == 1)
        ).first()
        if not status_exists:
            connection.execute(
                sa.insert(job_status_table).values(
                    id=1,
                    is_running=False,
                    current_trigger=None,
                    last_run_at=None,
                    last_run_display=None,
                    last_success=None,
                    last_message="",
                    run_count=0,
                    last_details=None,
                    updated_at=now,
                    updated_at_display=_format_display_time(now),
                )
            )


def _load_state_from_db(db_engine: Engine) -> None:
    """Load persisted scheduler state into memory."""
    try:
        with db_engine.connect() as connection:
            schedule_rows = connection.execute(sa.select(cron_jobs_table)).fetchall()
            status_row = connection.execute(
                sa.select(job_status_table).where(job_status_table.c.id == 1)
            ).first()
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to load scheduler state from database: {0}".format(exc))
        return

    with _state_lock:
        for row in schedule_rows:
            if row.schedule_type == "cron":
                schedule_state["cron"]["enabled"] = bool(row.enabled)
                schedule_state["cron"]["schedule"] = {
                    "minute": row.minute or DEFAULT_SCHEDULE["minute"],
                    "hour": row.hour or DEFAULT_SCHEDULE["hour"],
                    "day_of_month": row.day_of_month or DEFAULT_SCHEDULE["day_of_month"],
                    "month": row.month or DEFAULT_SCHEDULE["month"],
                    "day_of_week": row.day_of_week or DEFAULT_SCHEDULE["day_of_week"],
                }
                display_value = row.next_run_display
                if row.next_run:
                    local_next = row.next_run.astimezone(LOCAL_TZ)
                    schedule_state["cron"]["next_run"] = local_next.isoformat()
                    schedule_state["cron"]["next_run_display"] = display_value or _format_display_time(local_next)
                elif display_value:
                    schedule_state["cron"]["next_run"] = None
                    schedule_state["cron"]["next_run_display"] = display_value
                else:
                    schedule_state["cron"]["next_run"] = None
                    schedule_state["cron"]["next_run_display"] = None
            elif row.schedule_type == "daily":
                schedule_state["daily"]["enabled"] = bool(row.enabled)
                schedule_state["daily"]["time"] = row.daily_time
                if row.daily_time:
                    schedule_state["daily"]["time"] = row.daily_time
                display_value = row.next_run_display
                if row.next_run:
                    local_next = row.next_run.astimezone(LOCAL_TZ)
                    schedule_state["daily"]["next_run"] = local_next.isoformat()
                    schedule_state["daily"]["next_run_display"] = display_value or _format_display_time(local_next)
                elif display_value:
                    schedule_state["daily"]["next_run"] = None
                    schedule_state["daily"]["next_run_display"] = display_value
                else:
                    schedule_state["daily"]["next_run"] = None
                    schedule_state["daily"]["next_run_display"] = None

        if status_row:
            job_state["is_running"] = False
            job_state["current_trigger"] = None
            if status_row.last_run_at:
                last_local = status_row.last_run_at.astimezone(LOCAL_TZ)
                job_state["last_run_at"] = last_local.isoformat()
                job_state["last_run_display"] = status_row.last_run_display or _format_display_time(last_local)
            elif status_row.last_run_display:
                job_state["last_run_at"] = None
                job_state["last_run_display"] = status_row.last_run_display
            else:
                job_state["last_run_at"] = None
                job_state["last_run_display"] = None
            job_state["last_success"] = status_row.last_success
            job_state["last_message"] = status_row.last_message or ""
            job_state["run_count"] = status_row.run_count or 0
            if status_row.last_details:
                try:
                    job_state["last_details"] = json.loads(status_row.last_details)
                except json.JSONDecodeError:
                    job_state["last_details"] = {"raw": status_row.last_details}
            else:
                job_state["last_details"] = {}

    try:
        with db_engine.begin() as connection:
            update_time = datetime.now(LOCAL_TZ)
            connection.execute(
                sa.update(job_status_table)
                .where(job_status_table.c.id == 1)
                .values(
                    is_running=False,
                    current_trigger=None,
                    updated_at=update_time,
                    updated_at_display=_format_display_time(update_time),
                )
            )
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.warning("Unable to reset running flag in database: %s", exc)


def _persist_job_running_state(is_running: bool, trigger: Optional[str]) -> None:
    """Persist only the running flag and trigger information."""
    db_engine = _get_engine()
    if db_engine is None:
        return
    now = datetime.now(LOCAL_TZ)
    formatted_now = _format_display_time(now)
    payload = {
        "is_running": is_running,
        "current_trigger": trigger,
        "updated_at": now,
        "updated_at_display": formatted_now,
    }
    try:
        with db_engine.begin() as connection:
            result = connection.execute(
                sa.update(job_status_table).where(job_status_table.c.id == 1).values(**payload)
            )
            if result.rowcount == 0:
                insert_payload = {
                    "id": 1,
                    "last_run_at": None,
                    "last_success": None,
                    "last_message": "",
                    "run_count": 0,
                    "last_details": None,
                    **payload,
                }
                connection.execute(sa.insert(job_status_table).values(**insert_payload))
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to persist running state: %s", exc)


def _persist_job_state_snapshot(current_time: datetime) -> None:
    """Persist the in-memory job state to the database."""
    db_engine = _get_engine()
    if db_engine is None:
        return
    try:
        details_payload = job_state["last_details"] or {}
        details_text = json.dumps(details_payload)
    except (TypeError, ValueError):
        details_text = json.dumps({"error": "Unable to serialise job details."})

    payload = {
        "is_running": job_state["is_running"],
        "current_trigger": job_state["current_trigger"],
        "last_run_at": current_time,
        "last_success": job_state["last_success"],
        "last_message": job_state["last_message"],
        "run_count": job_state["run_count"],
        "last_details": details_text,
        "updated_at": current_time,
        "last_run_display": _format_display_time(current_time),
        "updated_at_display": _format_display_time(current_time),
    }

    try:
        with db_engine.begin() as connection:
            result = connection.execute(
                sa.update(job_status_table).where(job_status_table.c.id == 1).values(**payload)
            )
            if result.rowcount == 0:
                insert_payload = {"id": 1, **payload}
                connection.execute(sa.insert(job_status_table).values(**insert_payload))
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to persist job state: %s", exc)


def _persist_cron_schedule(schedule_fields: Dict[str, str], enabled: bool, next_run: Optional[datetime]) -> None:
    """Persist cron schedule configuration to the database."""
    db_engine = _get_engine()
    if db_engine is None:
        return
    now = datetime.now(LOCAL_TZ)
    formatted_next_run = _format_display_time(next_run) if next_run else None
    formatted_now = _format_display_time(now)

    payload = {
        "minute": schedule_fields.get("minute", DEFAULT_SCHEDULE["minute"]),
        "hour": schedule_fields.get("hour", DEFAULT_SCHEDULE["hour"]),
        "day_of_month": schedule_fields.get("day_of_month", DEFAULT_SCHEDULE["day_of_month"]),
        "month": schedule_fields.get("month", DEFAULT_SCHEDULE["month"]),
        "day_of_week": schedule_fields.get("day_of_week", DEFAULT_SCHEDULE["day_of_week"]),
        "daily_time": None,
        "enabled": enabled,
        "next_run": next_run,
        "next_run_display": formatted_next_run,
        "updated_at": now,
        "updated_at_display": formatted_now,
    }
    try:
        with db_engine.begin() as connection:
            result = connection.execute(
                sa.update(cron_jobs_table).where(cron_jobs_table.c.job_key == "cron").values(**payload)
            )
            if result.rowcount == 0:
                insert_payload = {"job_key": "cron", "schedule_type": "cron", **payload}
                connection.execute(sa.insert(cron_jobs_table).values(**insert_payload))
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to persist cron schedule: %s", exc)


def _persist_daily_schedule(daily_time: Optional[str], enabled: bool, next_run: Optional[datetime]) -> None:
    """Persist daily schedule configuration to the database."""
    db_engine = _get_engine()
    if db_engine is None:
        return
    now = datetime.now(LOCAL_TZ)
    formatted_now = _format_display_time(now)

    hour_value = DEFAULT_SCHEDULE["hour"]
    minute_value = DEFAULT_SCHEDULE["minute"]
    if daily_time:
        try:
            hour_value, minute_value = daily_time.split(":", 1)
        except ValueError:
            hour_value = DEFAULT_SCHEDULE["hour"]
            minute_value = DEFAULT_SCHEDULE["minute"]

    formatted_next_run = _format_display_time(next_run) if next_run else None

    payload = {
        "minute": minute_value,
        "hour": hour_value,
        "day_of_month": DEFAULT_SCHEDULE["day_of_month"],
        "month": DEFAULT_SCHEDULE["month"],
        "day_of_week": DEFAULT_SCHEDULE["day_of_week"],
        "daily_time": daily_time,
        "enabled": enabled,
        "next_run": next_run,
        "next_run_display": formatted_next_run,
        "updated_at": now,
        "updated_at_display": formatted_now,
    }

    try:
        with db_engine.begin() as connection:
            result = connection.execute(
                sa.update(cron_jobs_table).where(cron_jobs_table.c.job_key == "daily").values(**payload)
            )
            if result.rowcount == 0:
                insert_payload = {"job_key": "daily", "schedule_type": "daily", **payload}
                connection.execute(sa.insert(cron_jobs_table).values(**insert_payload))
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to persist daily schedule: %s", exc)


def _get_safe_redirect_target(target: Optional[str]) -> str:
    """Return a safe in-app redirect target."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


def _login_redirect_response() -> Any:
    """Redirect the user to the login page while preserving the requested path."""
    next_target = request.full_path if request.query_string else request.path
    next_target = next_target.rstrip("?")
    return redirect(url_for("login", next=next_target))


def login_required(json_response: bool = False):
    """Decorator that enforces authentication for protected views."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get(SESSION_AUTH_FLAG):
                return view(*args, **kwargs)
            if json_response:
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            return _login_redirect_response()

        return wrapped

    return decorator


def _initialise_state_from_database() -> None:
    """Initialise scheduler state from the configured database, if available."""
    global _db_warning_logged  # pylint: disable=global-statement
    if not DATABASE_URL:
        return

    db_engine = _get_engine()
    if db_engine is None:
        if not _db_warning_logged:
            app.logger.warning(
                "DATABASE_URL is set but the connection could not be established. Falling back to in-memory state."
            )
            _db_warning_logged = True
        return

    try:
        metadata.create_all(db_engine, checkfirst=True)
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to create database tables: %s", exc)
        return

    try:
        _ensure_seed_rows(db_engine)
        _load_state_from_db(db_engine)
    except SQLAlchemyError as exc:  # pragma: no cover - logged for visibility
        app.logger.error("Failed to prepare database state: %s", exc)


def _format_display_time(dt: datetime) -> str:
    local_dt = dt.astimezone(LOCAL_TZ)
    date_part = local_dt.strftime("%d %b, %Y %I:%M")
    ampm = local_dt.strftime("%p").lower()
    formatted = f"{date_part}{ampm}"
    tz_name = local_dt.tzname()
    if tz_name:
        formatted = f"{formatted} {tz_name}"
    return formatted


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

    current_time = datetime.now(LOCAL_TZ)
    now = current_time.isoformat()
    with _state_lock:
        job_state["is_running"] = False
        job_state["last_run_at"] = now
        job_state["last_run_display"] = _format_display_time(current_time)
        job_state["last_success"] = False
        job_state["last_message"] = f"Unhandled scheduler error: {event.exception}"
        job_state["current_trigger"] = "scheduler"
    _persist_job_state_snapshot(current_time)


def _mark_job_start(trigger: str) -> bool:
    """Set state to running if no job is currently active."""
    with _state_lock:
        if job_state["is_running"]:
            return False
        job_state["is_running"] = True
        job_state["current_trigger"] = trigger
    _persist_job_running_state(True, trigger)
    return True


def _finalize_job_run(success: bool, message: str, trigger: str, details: Optional[Dict[str, Any]]) -> None:
    """Persist the outcome of a job run."""
    current_time = datetime.now(LOCAL_TZ)
    now = current_time.isoformat()
    ending_message = message or ("Job completed successfully." if success else "Job finished.")

    with _state_lock:
        job_state["is_running"] = False
        job_state["last_run_at"] = now
        job_state["last_run_display"] = _format_display_time(current_time)
        job_state["last_success"] = success
        job_state["last_message"] = ending_message
        job_state["current_trigger"] = trigger
        job_state["run_count"] += 1
        job_state["last_details"] = dict(details) if details else {}
    _persist_job_state_snapshot(current_time)


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
    now = datetime.now(LOCAL_TZ)
    try:
        trigger = CronTrigger(
            month=schedule_fields["month"],
            day=schedule_fields["day_of_month"],
            day_of_week=schedule_fields["day_of_week"],
            hour=schedule_fields["hour"],
            minute=schedule_fields["minute"],
            second="0",
            timezone=LOCAL_TZ,
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

    local_next = next_run_time.astimezone(LOCAL_TZ)
    display = _format_display_time(local_next)
    iso_value = local_next.isoformat()

    with _state_lock:
        schedule_state["cron"]["enabled"] = True
        schedule_state["cron"]["schedule"] = schedule_fields.copy()
        schedule_state["cron"]["next_run"] = iso_value
        schedule_state["cron"]["next_run_display"] = display
    _persist_cron_schedule(schedule_fields, True, local_next)

    app.logger.info("Scheduled cron job with fields=%s next_run=%s", schedule_fields, display)
    return iso_value


def _schedule_daily_job(daily_time_value: str) -> Optional[str]:
    """Create or update the daily scheduled job."""
    now = datetime.now(LOCAL_TZ)
    try:
        parsed_time = datetime.strptime(daily_time_value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid time format: {daily_time_value}") from exc

    target_next_run = now.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
    if target_next_run <= now:
        target_next_run += timedelta(days=1)

    trigger = CronTrigger(hour=str(parsed_time.hour), minute=str(parsed_time.minute), second="0", timezone=LOCAL_TZ)

    _ensure_scheduler_started()
    scheduler.add_job(
        _scheduled_job_run,
        trigger=trigger,
        id=DAILY_JOB_ID,
        replace_existing=True,
        next_run_time=target_next_run,
        kwargs={"source": "daily"},
    )

    next_local = target_next_run.astimezone(LOCAL_TZ)
    next_iso = next_local.isoformat()
    with _state_lock:
        schedule_state["daily"]["enabled"] = True
        schedule_state["daily"]["time"] = daily_time_value
        schedule_state["daily"]["next_run"] = next_iso
        schedule_state["daily"]["next_run_display"] = _format_display_time(next_local)
    _persist_daily_schedule(daily_time_value, True, next_local)

    app.logger.info("Scheduled daily job for %s next_run=%s", daily_time_value, _format_display_time(next_local))
    return next_iso


def _disable_schedule(schedule_type: str) -> None:
    """Disable a schedule by type ('cron' or 'daily')."""
    job_id = CRON_JOB_ID if schedule_type == "cron" else DAILY_JOB_ID

    if _scheduler_started:
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    schedule_snapshot: Dict[str, Any] = {}
    with _state_lock:
        target_state = schedule_state.get(schedule_type)
        if target_state is not None:
            target_state["enabled"] = False
            target_state["next_run"] = None
            target_state["next_run_display"] = None
            schedule_snapshot = target_state.copy()

    if schedule_type == "cron":
        cron_schedule = schedule_snapshot.get("schedule") if schedule_snapshot else DEFAULT_SCHEDULE.copy()
        _persist_cron_schedule(cron_schedule, False, None)
    else:
        daily_time = schedule_snapshot.get("time") if schedule_snapshot else None
        _persist_daily_schedule(daily_time, False, None)


def _get_scheduler_status() -> Dict[str, Any]:
    """Return a snapshot of scheduler and job state."""
    with _state_lock:
        snapshot = {
            "is_running": job_state["is_running"],
            "current_trigger": job_state["current_trigger"],
            "last_run_at": job_state["last_run_at"],
            "last_run_display": job_state["last_run_display"],
            "last_success": job_state["last_success"],
            "last_message": job_state["last_message"],
            "run_count": job_state["run_count"],
            "last_details": job_state["last_details"].copy(),
            "cron": {
                "enabled": schedule_state["cron"]["enabled"],
                "schedule": schedule_state["cron"]["schedule"].copy(),
                "next_run": schedule_state["cron"]["next_run"],
                "next_run_display": schedule_state["cron"]["next_run_display"],
            },
            "daily": {
                "enabled": schedule_state["daily"]["enabled"],
                "time": schedule_state["daily"]["time"],
                "next_run": schedule_state["daily"]["next_run"],
                "next_run_display": schedule_state["daily"]["next_run_display"],
            },
        }

    server_now = datetime.now(LOCAL_TZ)
    snapshot["scheduler_running"] = _scheduler_started and scheduler.running
    snapshot["server_time"] = server_now.isoformat()
    snapshot["server_time_display"] = _format_display_time(server_now)
    snapshot["server_timezone"] = server_now.tzname()
    offset = server_now.utcoffset()
    snapshot["server_utc_offset_minutes"] = int(offset.total_seconds() // 60) if offset else 0

    next_candidates = []
    if _scheduler_started:
        cron_job = scheduler.get_job(CRON_JOB_ID)
        daily_job = scheduler.get_job(DAILY_JOB_ID)
        if cron_job and cron_job.next_run_time:
            cron_dt = cron_job.next_run_time.astimezone(LOCAL_TZ)
            display = _format_display_time(cron_dt)
            iso_value = cron_dt.isoformat()
            snapshot["cron"]["next_run"] = iso_value
            snapshot["cron"]["next_run_display"] = display
            schedule_state["cron"]["next_run"] = iso_value
            schedule_state["cron"]["next_run_display"] = display
            next_candidates.append((cron_dt, iso_value, display))
        if daily_job and daily_job.next_run_time:
            daily_dt = daily_job.next_run_time.astimezone(LOCAL_TZ)
            display = _format_display_time(daily_dt)
            iso_value = daily_dt.isoformat()
            snapshot["daily"]["next_run"] = iso_value
            snapshot["daily"]["next_run_display"] = display
            schedule_state["daily"]["next_run"] = iso_value
            schedule_state["daily"]["next_run_display"] = display
            next_candidates.append((daily_dt, iso_value, display))

    if next_candidates:
        next_candidates.sort(key=lambda item: item[0])
        snapshot["next_run"] = next_candidates[0][1]
        snapshot["next_run_display"] = next_candidates[0][2]
    else:
        snapshot["next_run"] = None
        snapshot["next_run_display"] = None

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

    next_run_dt: Optional[datetime] = None
    schedule_snapshot: Dict[str, Any] = {}

    with _state_lock:
        if source in {"cron", "daily"} and _scheduler_started:
            job_id = CRON_JOB_ID if source == "cron" else DAILY_JOB_ID
            job = scheduler.get_job(job_id)
            if job and job.next_run_time:
                run_dt = job.next_run_time.astimezone(LOCAL_TZ)
                schedule_state[source]["next_run"] = run_dt.isoformat()
                schedule_state[source]["next_run_display"] = _format_display_time(run_dt)
                next_run_dt = run_dt
            else:
                schedule_state[source]["next_run"] = None
                schedule_state[source]["next_run_display"] = None
                next_run_dt = None
            schedule_snapshot = schedule_state[source].copy()
            if source == "cron":
                schedule_snapshot["schedule"] = schedule_state["cron"]["schedule"].copy()

    if source == "cron" and schedule_snapshot:
        cron_fields = schedule_snapshot.get("schedule") or DEFAULT_SCHEDULE.copy()
        _persist_cron_schedule(cron_fields, bool(schedule_snapshot.get("enabled")), next_run_dt)
    elif source == "daily" and schedule_snapshot:
        daily_time_value = schedule_snapshot.get("time")
        _persist_daily_schedule(daily_time_value, bool(schedule_snapshot.get("enabled")), next_run_dt)


_initialise_state_from_database()


@app.route("/", methods=["GET"])
@login_required()
def index() -> str:
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get(SESSION_AUTH_FLAG):
        return redirect(url_for("index"))

    next_target = request.args.get("next") or request.form.get("next") or url_for("index")
    next_target = _get_safe_redirect_target(next_target)

    error = None
    username = ""

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if username == STATIC_USERNAME and password == STATIC_PASSWORD:
            session[SESSION_AUTH_FLAG] = True
            session[SESSION_USER_KEY] = username
            session.permanent = False
            return redirect(next_target)

        error = "Invalid username or password. Please try again."

    return render_template(
        "login.html",
        error=error,
        next=next_target,
        username=username,
        expected_username=STATIC_USERNAME,
    )


@app.route("/logout", methods=["POST"])
@login_required()
def logout():
    session.pop(SESSION_AUTH_FLAG, None)
    session.pop(SESSION_USER_KEY, None)
    return redirect(url_for("login"))


@app.route("/job/run", methods=["POST"])
@login_required(json_response=True)
def run_job():
    result = _run_job("manual")
    status_code = result.pop("status_code", 200)
    return jsonify(result), status_code


@app.route("/job/status", methods=["GET"])
@login_required(json_response=True)
def job_status():
    return jsonify(_get_scheduler_status())


@app.route("/job/schedule", methods=["POST"])
@login_required(json_response=True)
def update_schedule():
    payload = request.get_json(silent=True) or {}
    schedule_fields = _normalize_schedule_payload(payload)
    try:
        next_run_time = _schedule_cron_job(schedule_fields)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    response = _get_scheduler_status()
    response["cron_next_run"] = next_run_time
    response["cron_next_run_display"] = schedule_state["cron"]["next_run_display"]
    response["success"] = True
    return jsonify(response)


@app.route("/job/daily", methods=["POST"])
@login_required(json_response=True)
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
    response["daily_next_run_display"] = schedule_state["daily"]["next_run_display"]
    response["success"] = True
    return jsonify(response)


@app.route("/job/start", methods=["POST"])
@login_required(json_response=True)
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
            responses["cron_next_run_display"] = schedule_state["cron"]["next_run_display"]
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
                responses["daily_next_run_display"] = schedule_state["daily"]["next_run_display"]
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
@login_required(json_response=True)
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
