"""Data access layer using repository pattern.

Features:
- Clean data access API
- Query optimization
- Error handling
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

from .database import DatabaseManager
from .models import (
    AuditLogModel,
    CostTrackingModel,
    CronJobModel,
    CronRunModel,
    MessageModel,
    SessionEventModel,
    SessionModel,
    ToolUsageModel,
    UserModel,
)

logger = structlog.get_logger()


def _require_lastrowid(lastrowid: Optional[int]) -> int:
    """Return SQLite lastrowid as int or raise when missing."""
    if lastrowid is None:
        raise RuntimeError("insert operation did not return lastrowid")
    return int(lastrowid)


class UserRepository:
    """User data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user(self, user_id: int) -> Optional[UserModel]:
        """Get user by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return UserModel.from_row(row) if row else None

    async def create_user(self, user: UserModel) -> UserModel:
        """Create new user."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, telegram_username, first_seen, last_active, is_allowed)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    user.user_id,
                    user.telegram_username,
                    user.first_seen or datetime.utcnow(),
                    user.last_active or datetime.utcnow(),
                    user.is_allowed,
                ),
            )
            await conn.commit()

            logger.info(
                "Created user", user_id=user.user_id, username=user.telegram_username
            )
            return user

    async def update_user(self, user: UserModel) -> None:
        """Update user data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET telegram_username = ?, last_active = ?,
                    total_cost = ?, message_count = ?, session_count = ?
                WHERE user_id = ?
            """,
                (
                    user.telegram_username,
                    user.last_active or datetime.utcnow(),
                    user.total_cost,
                    user.message_count,
                    user.session_count,
                    user.user_id,
                ),
            )
            await conn.commit()

    async def get_allowed_users(self) -> List[int]:
        """Get list of allowed user IDs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE is_allowed = TRUE"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def set_user_allowed(self, user_id: int, allowed: bool) -> None:
        """Set user allowed status."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET is_allowed = ? WHERE user_id = ?", (allowed, user_id)
            )
            await conn.commit()

            logger.info("Updated user permissions", user_id=user_id, allowed=allowed)

    async def get_all_users(self) -> List[UserModel]:
        """Get all users."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM users ORDER BY first_seen DESC")
            rows = await cursor.fetchall()
            return [UserModel.from_row(row) for row in rows]


class SessionRepository:
    """Session data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_session(self, session_id: str) -> Optional[SessionModel]:
        """Get session by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = await cursor.fetchone()
            return SessionModel.from_row(row) if row else None

    async def create_session(self, session: SessionModel) -> SessionModel:
        """Create new session."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                (session_id, user_id, project_path, created_at, last_used)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    session.session_id,
                    session.user_id,
                    session.project_path,
                    session.created_at,
                    session.last_used,
                ),
            )
            await conn.commit()

            logger.info(
                "Created session",
                session_id=session.session_id,
                user_id=session.user_id,
            )
            return session

    async def update_session(self, session: SessionModel) -> None:
        """Update session data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET last_used = ?, total_cost = ?, total_turns = ?,
                    message_count = ?, is_active = ?
                WHERE session_id = ?
            """,
                (
                    session.last_used,
                    session.total_cost,
                    session.total_turns,
                    session.message_count,
                    session.is_active,
                    session.session_id,
                ),
            )
            await conn.commit()

    async def get_user_sessions(
        self, user_id: int, active_only: bool = True
    ) -> List[SessionModel]:
        """Get sessions for user."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM sessions WHERE user_id = ?"
            params = [user_id]

            if active_only:
                query += " AND is_active = TRUE"

            query += " ORDER BY last_used DESC"

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]

    async def cleanup_old_sessions(self, days: int = 30) -> int:
        """Mark old sessions as inactive."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET is_active = FALSE
                WHERE last_used < datetime('now', '-' || ? || ' days')
                  AND is_active = TRUE
            """,
                (days,),
            )
            await conn.commit()

            affected = cursor.rowcount
            logger.info("Cleaned up old sessions", count=affected, days=days)
            return affected

    async def get_sessions_by_project(self, project_path: str) -> List[SessionModel]:
        """Get sessions for a specific project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM sessions
                WHERE project_path = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (project_path,),
            )
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]


class MessageRepository:
    """Message data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_message(self, message: MessageModel) -> int:
        """Save message and return ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO messages
                (session_id, user_id, timestamp, prompt, response, cost, duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    message.session_id,
                    message.user_id,
                    message.timestamp,
                    message.prompt,
                    message.response,
                    message.cost,
                    message.duration_ms,
                    message.error,
                ),
            )
            await conn.commit()
            return _require_lastrowid(cursor.lastrowid)

    async def get_session_messages(
        self, session_id: str, limit: int = 50
    ) -> List[MessageModel]:
        """Get messages for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_user_messages(
        self, user_id: int, limit: int = 100
    ) -> List[MessageModel]:
        """Get messages for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_recent_messages(self, hours: int = 24) -> List[MessageModel]:
        """Get recent messages."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE timestamp > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
            """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]


class ToolUsageRepository:
    """Tool usage data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_tool_usage(self, tool_usage: ToolUsageModel) -> int:
        """Save tool usage and return ID."""
        async with self.db.get_connection() as conn:
            tool_input_json = (
                json.dumps(tool_usage.tool_input) if tool_usage.tool_input else None
            )

            cursor = await conn.execute(
                """
                INSERT INTO tool_usage
                (session_id, message_id, tool_name, tool_input, timestamp, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    tool_usage.session_id,
                    tool_usage.message_id,
                    tool_usage.tool_name,
                    tool_input_json,
                    tool_usage.timestamp,
                    tool_usage.success,
                    tool_usage.error_message,
                ),
            )
            await conn.commit()
            return _require_lastrowid(cursor.lastrowid)

    async def get_session_tool_usage(self, session_id: str) -> List[ToolUsageModel]:
        """Get tool usage for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM tool_usage
                WHERE session_id = ?
                ORDER BY timestamp DESC
            """,
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_user_tool_usage(self, user_id: int) -> List[ToolUsageModel]:
        """Get tool usage for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT tu.* FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                ORDER BY tu.timestamp DESC
            """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_tool_stats(self) -> List[Dict[str, Any]]:
        """Get tool usage statistics."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used,
                    SUM(CASE WHEN success = TRUE THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN success = FALSE THEN 1 ELSE 0 END) as error_count
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
            """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AuditLogRepository:
    """Audit log data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def log_event(self, audit_log: AuditLogModel) -> int:
        """Log audit event and return ID."""
        async with self.db.get_connection() as conn:
            event_data_json = (
                json.dumps(audit_log.event_data) if audit_log.event_data else None
            )

            cursor = await conn.execute(
                """
                INSERT INTO audit_log
                (user_id, event_type, event_data, success, timestamp, ip_address)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    audit_log.user_id,
                    audit_log.event_type,
                    event_data_json,
                    audit_log.success,
                    audit_log.timestamp,
                    audit_log.ip_address,
                ),
            )
            await conn.commit()
            return _require_lastrowid(cursor.lastrowid)

    async def get_user_audit_log(
        self, user_id: int, limit: int = 100
    ) -> List[AuditLogModel]:
        """Get audit log for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]

    async def get_recent_audit_log(self, hours: int = 24) -> List[AuditLogModel]:
        """Get recent audit log entries."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE timestamp > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
            """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]

    async def get_events(
        self,
        user_id: Optional[int] = None,
        event_type: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[AuditLogModel]:
        """Get audit events with optional filters."""
        query = "SELECT * FROM audit_log"
        conditions: List[str] = []
        params: List[Any] = []

        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)

        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type)

        if start_time is not None:
            conditions.append("timestamp >= ?")
            params.append(start_time)

        if end_time is not None:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self.db.get_connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]

    async def get_security_violations(
        self,
        user_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[AuditLogModel]:
        """Get security violation events."""
        return await self.get_events(
            user_id=user_id,
            event_type="security_violation",
            limit=limit,
        )


class SessionEventRepository:
    """Session event data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_event(self, event: SessionEventModel) -> int:
        """Save one session event and return ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO session_events
                (session_id, event_type, event_data, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.session_id,
                    event.event_type,
                    json.dumps(event.event_data or {}),
                    event.created_at,
                ),
            )
            await conn.commit()
            return _require_lastrowid(cursor.lastrowid)

    async def save_events(self, events: List[SessionEventModel]) -> int:
        """Save multiple session events and return persisted count."""
        if not events:
            return 0

        rows = [
            (
                event.session_id,
                event.event_type,
                json.dumps(event.event_data or {}),
                event.created_at,
            )
            for event in events
        ]

        async with self.db.get_connection() as conn:
            await conn.executemany(
                """
                INSERT INTO session_events
                (session_id, event_type, event_data, created_at)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
            await conn.commit()

        return len(rows)

    async def get_session_events(
        self,
        session_id: str,
        event_types: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[SessionEventModel]:
        """Get session events ordered by newest first."""
        query = "SELECT * FROM session_events WHERE session_id = ?"
        params: List[Any] = [session_id]

        if event_types:
            placeholders = ", ".join(["?"] * len(event_types))
            query += f" AND event_type IN ({placeholders})"
            params.extend(event_types)

        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)

        async with self.db.get_connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [SessionEventModel.from_row(row) for row in rows]


class ApprovalRequestRepository:
    """Approval request persistence for permission workflow."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def create_request(
        self,
        *,
        request_id: str,
        user_id: int,
        session_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        expires_at: datetime,
    ) -> None:
        """Create a pending approval request."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO approval_requests
                (request_id, user_id, session_id, tool_name, tool_input, status, expires_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    request_id,
                    user_id,
                    session_id,
                    tool_name,
                    json.dumps(tool_input) if tool_input else None,
                    expires_at,
                ),
            )
            await conn.commit()

    async def resolve_request(
        self,
        *,
        request_id: str,
        status: str,
        decision: Optional[str],
        resolved_at: datetime,
    ) -> bool:
        """Resolve a pending request into approved/denied/expired.

        Returns True when the request transitioned from pending to target status.
        Returns False when request was not found or already resolved.
        """
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, decision = ?, resolved_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (status, decision, resolved_at, request_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def expire_all_pending(self, *, resolved_at: datetime) -> int:
        """Expire all pending requests on startup recovery."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE approval_requests
                SET status = 'expired', decision = NULL, resolved_at = ?
                WHERE status = 'pending'
                """,
                (resolved_at,),
            )
            await conn.commit()
            return cursor.rowcount


class CronJobRepository:
    """Cron job data access."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def create_job(self, job: CronJobModel) -> CronJobModel:
        """Persist one cron job and return model with generated id."""
        now = datetime.utcnow()
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO cron_jobs (
                    user_id, job_type, schedule_type, cron_expr, run_at,
                    payload_text, engine, chat_id, thread_id, scope_key, project_dir,
                    session_id, status, fail_count, last_error,
                    last_run_at, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.user_id,
                    job.job_type,
                    job.schedule_type,
                    job.cron_expr,
                    job.run_at,
                    job.payload_text,
                    job.engine,
                    job.chat_id,
                    job.thread_id,
                    job.scope_key,
                    job.project_dir,
                    job.session_id,
                    job.status,
                    job.fail_count,
                    job.last_error,
                    job.last_run_at,
                    job.next_run_at,
                    job.created_at or now,
                    job.updated_at or now,
                ),
            )
            await conn.commit()
            job.id = _require_lastrowid(cursor.lastrowid)
            if not job.created_at:
                job.created_at = now
            if not job.updated_at:
                job.updated_at = now
            return job

    async def get_job(
        self, job_id: int, *, user_id: Optional[int] = None
    ) -> Optional[CronJobModel]:
        """Get job by id."""
        query = "SELECT * FROM cron_jobs WHERE id = ?"
        params: list[Any] = [job_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
            return CronJobModel.from_row(row) if row else None

    async def list_jobs(
        self, *, user_id: int, include_inactive: bool = True
    ) -> List[CronJobModel]:
        """List jobs for one user."""
        query = "SELECT * FROM cron_jobs WHERE user_id = ?"
        params: list[Any] = [user_id]
        if not include_inactive:
            query += " AND status = 'enabled'"
        query += " ORDER BY created_at DESC, id DESC"
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [CronJobModel.from_row(row) for row in rows]

    async def count_user_active_jobs(self, *, user_id: int) -> int:
        """Count enabled/paused jobs for one user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) FROM cron_jobs
                WHERE user_id = ?
                  AND status IN ('enabled', 'paused')
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def list_enabled_jobs(self) -> List[CronJobModel]:
        """List enabled jobs for scheduler bootstrap."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM cron_jobs
                WHERE status = 'enabled'
                ORDER BY next_run_at ASC, id ASC
                """
            )
            rows = await cursor.fetchall()
            return [CronJobModel.from_row(row) for row in rows]

    async def set_status(
        self,
        *,
        job_id: int,
        status: str,
        user_id: Optional[int] = None,
        next_run_at: Optional[datetime] = None,
        last_error: Optional[str] = None,
    ) -> bool:
        """Update job status and metadata."""
        query = """
            UPDATE cron_jobs
            SET status = ?, next_run_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
        """
        params: list[Any] = [status, next_run_at, last_error, datetime.utcnow(), job_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(query, params)
            await conn.commit()
            return cursor.rowcount > 0

    async def update_runtime(
        self,
        *,
        job_id: int,
        last_run_at: Optional[datetime],
        next_run_at: Optional[datetime],
        fail_count: int,
        last_error: Optional[str],
        session_id: Optional[str] = None,
    ) -> bool:
        """Update runtime fields after one execution."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE cron_jobs
                SET last_run_at = ?,
                    next_run_at = ?,
                    fail_count = ?,
                    last_error = ?,
                    session_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    last_run_at,
                    next_run_at,
                    fail_count,
                    last_error,
                    session_id,
                    datetime.utcnow(),
                    job_id,
                ),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def mark_deleted(self, *, job_id: int, user_id: int) -> bool:
        """Soft-delete job."""
        return await self.set_status(
            job_id=job_id,
            user_id=user_id,
            status="deleted",
            next_run_at=None,
            last_error=None,
        )


class CronRunRepository:
    """Cron execution run record access."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def create_run(self, run: CronRunModel) -> int:
        """Persist one execution run and return run id."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO cron_runs (
                    job_id, user_id, started_at, finished_at, success,
                    output_preview, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.job_id,
                    run.user_id,
                    run.started_at,
                    run.finished_at,
                    run.success,
                    run.output_preview,
                    run.error_message,
                ),
            )
            await conn.commit()
            run_id = _require_lastrowid(cursor.lastrowid)
            run.id = run_id
            return run_id

    async def list_runs(self, *, job_id: int, limit: int = 20) -> List[CronRunModel]:
        """List run history for one job."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM cron_runs
                WHERE job_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (job_id, limit),
            )
            rows = await cursor.fetchall()
            return [CronRunModel.from_row(row) for row in rows]


class CostTrackingRepository:
    """Cost tracking data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def update_daily_cost(
        self, user_id: int, cost: float, date: Optional[str] = None
    ) -> None:
        """Update daily cost for user."""
        if not date:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO cost_tracking (user_id, date, daily_cost, request_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, date)
                DO UPDATE SET
                    daily_cost = daily_cost + ?,
                    request_count = request_count + 1
            """,
                (user_id, date, cost, cost),
            )
            await conn.commit()

    async def get_user_daily_costs(
        self, user_id: int, days: int = 30
    ) -> List[CostTrackingModel]:
        """Get user's daily costs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM cost_tracking
                WHERE user_id = ? AND date >= date('now', '-' || ? || ' days')
                ORDER BY date DESC
            """,
                (user_id, days),
            )
            rows = await cursor.fetchall()
            return [CostTrackingModel.from_row(row) for row in rows]

    async def get_total_costs(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get total costs by day."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    date,
                    SUM(daily_cost) as total_cost,
                    SUM(request_count) as total_requests,
                    COUNT(DISTINCT user_id) as active_users
                FROM cost_tracking
                WHERE date >= date('now', '-' || ? || ' days')
                GROUP BY date
                ORDER BY date DESC
            """,
                (days,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AnalyticsRepository:
    """Analytics and reporting."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Get user statistics."""
        async with self.db.get_connection() as conn:
            # User summary
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(cost) as avg_cost,
                    MAX(timestamp) as last_activity,
                    AVG(duration_ms) as avg_duration
                FROM messages
                WHERE user_id = ?
            """,
                (user_id,),
            )

            summary_row = await cursor.fetchone()
            summary = dict(summary_row) if summary_row is not None else {}

            # Daily usage (last 30 days)
            cursor = await conn.execute(
                """
                SELECT
                    date(timestamp) as date,
                    COUNT(*) as messages,
                    SUM(cost) as cost,
                    COUNT(DISTINCT session_id) as sessions
                FROM messages
                WHERE user_id = ? AND timestamp >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """,
                (user_id,),
            )

            daily_usage = [dict(row) for row in await cursor.fetchall()]

            # Most used tools
            cursor = await conn.execute(
                """
                SELECT
                    tu.tool_name,
                    COUNT(*) as usage_count
                FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                GROUP BY tu.tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """,
                (user_id,),
            )

            top_tools = [dict(row) for row in await cursor.fetchall()]

            return {
                "summary": summary,
                "daily_usage": daily_usage,
                "top_tools": top_tools,
            }

    async def get_system_stats(self) -> Dict[str, Any]:
        """Get system-wide statistics."""
        async with self.db.get_connection() as conn:
            # Overall stats
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(DISTINCT user_id) as total_users,
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(duration_ms) as avg_duration
                FROM messages
            """
            )

            overall_row = await cursor.fetchone()
            overall = dict(overall_row) if overall_row is not None else {}

            # Active users (last 7 days)
            cursor = await conn.execute(
                """
                SELECT COUNT(DISTINCT user_id) as active_users
                FROM messages
                WHERE timestamp > datetime('now', '-7 days')
            """
            )

            active_users_row = await cursor.fetchone()
            active_users = (
                int(active_users_row[0]) if active_users_row is not None else 0
            )
            overall["active_users_7d"] = active_users

            # Top users by cost
            cursor = await conn.execute(
                """
                SELECT
                    u.user_id,
                    u.telegram_username,
                    SUM(m.cost) as total_cost,
                    COUNT(m.message_id) as total_messages
                FROM messages m
                JOIN users u ON m.user_id = u.user_id
                GROUP BY u.user_id
                ORDER BY total_cost DESC
                LIMIT 10
            """
            )

            top_users = [dict(row) for row in await cursor.fetchall()]

            # Tool usage stats
            cursor = await conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """
            )

            tool_stats = [dict(row) for row in await cursor.fetchall()]

            # Daily activity (last 30 days)
            cursor = await conn.execute(
                """
                SELECT
                    date(timestamp) as date,
                    COUNT(DISTINCT user_id) as active_users,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost
                FROM messages
                WHERE timestamp >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """
            )

            daily_activity = [dict(row) for row in await cursor.fetchall()]

            return {
                "overall": overall,
                "top_users": top_users,
                "tool_stats": tool_stats,
                "daily_activity": daily_activity,
            }
