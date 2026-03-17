"""Unified storage interface.

Provides simple API for the rest of the application.
"""

from datetime import datetime
from typing import Any, Dict, Optional

import structlog

from ..claude.integration import ClaudeResponse
from .database import DatabaseManager
from .models import (
    AuditLogModel,
    MessageModel,
    SessionEventModel,
    SessionModel,
    ToolUsageModel,
    UserModel,
)
from .repositories import (
    AnalyticsRepository,
    ApprovalRequestRepository,
    AuditLogRepository,
    CostTrackingRepository,
    CronJobRepository,
    CronRunRepository,
    MessageRepository,
    SessionEventRepository,
    SessionRepository,
    ToolUsageRepository,
    UserRepository,
)

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        """Initialize storage with database URL."""
        self.db_manager = DatabaseManager(database_url)
        self.users = UserRepository(self.db_manager)
        self.sessions = SessionRepository(self.db_manager)
        self.messages = MessageRepository(self.db_manager)
        self.tools = ToolUsageRepository(self.db_manager)
        self.session_events = SessionEventRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)
        self.approvals = ApprovalRequestRepository(self.db_manager)
        self.cron_jobs = CronJobRepository(self.db_manager)
        self.cron_runs = CronRunRepository(self.db_manager)
        self.costs = CostTrackingRepository(self.db_manager)
        self.analytics = AnalyticsRepository(self.db_manager)

    async def initialize(self) -> None:
        """Initialize storage system."""
        logger.info("Initializing storage system")
        await self.db_manager.initialize()
        logger.info("Storage system initialized")

    async def close(self) -> None:
        """Close storage connections."""
        logger.info("Closing storage system")
        await self.db_manager.close()

    async def health_check(self) -> bool:
        """Check storage system health."""
        return await self.db_manager.health_check()

    # High-level operations

    async def save_claude_interaction(
        self,
        user_id: int,
        session_id: str,
        prompt: str,
        response: ClaudeResponse,
        ip_address: Optional[str] = None,
    ) -> None:
        """Save complete Claude interaction."""
        logger.info(
            "Saving Claude interaction",
            user_id=user_id,
            session_id=session_id,
            cost=response.cost,
        )

        # Save message
        message = MessageModel(
            message_id=None,
            session_id=session_id,
            user_id=user_id,
            timestamp=datetime.utcnow(),
            prompt=prompt,
            response=response.content,
            cost=response.cost,
            duration_ms=response.duration_ms,
            error=response.error_type if response.is_error else None,
        )

        message_id = await self.messages.save_message(message)

        # Save tool usage
        if response.tools_used:
            for tool in response.tools_used:
                tool_usage = ToolUsageModel(
                    id=None,
                    session_id=session_id,
                    message_id=message_id,
                    tool_name=tool["name"],
                    tool_input=tool.get("input", {}),
                    timestamp=datetime.utcnow(),
                    success=not response.is_error,
                    error_message=response.error_type if response.is_error else None,
                )
                await self.tools.save_tool_usage(tool_usage)

        # Update cost tracking
        await self.costs.update_daily_cost(user_id, response.cost)

        # Update user stats
        user = await self.users.get_user(user_id)
        if user:
            user.total_cost += response.cost
            user.message_count += 1
            user.last_active = datetime.utcnow()
            await self.users.update_user(user)

        # Update session stats
        session = await self.sessions.get_session(session_id)
        if session:
            session.total_cost += response.cost
            session.total_turns += response.num_turns
            session.message_count += 1
            session.last_used = datetime.utcnow()
            await self.sessions.update_session(session)

        # Save semantic session events for timeline replay.
        interaction_events = self._build_interaction_events(
            user_id=user_id,
            session_id=session_id,
            prompt=prompt,
            message_id=message_id,
            response=response,
        )
        await self.session_events.save_events(interaction_events)

        # Log audit event
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type="claude_interaction",
            event_data={
                "session_id": session_id,
                "cost": response.cost,
                "duration_ms": response.duration_ms,
                "num_turns": response.num_turns,
                "is_error": response.is_error,
                "tools_used": [t["name"] for t in response.tools_used],
            },
            success=not response.is_error,
            timestamp=datetime.utcnow(),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def get_or_create_user(
        self, user_id: int, username: Optional[str] = None
    ) -> UserModel:
        """Get or create user."""
        user = await self.users.get_user(user_id)

        if not user:
            logger.info("Creating new user", user_id=user_id, username=username)
            user = UserModel(
                user_id=user_id,
                telegram_username=username,
                first_seen=datetime.utcnow(),
                last_active=datetime.utcnow(),
                is_allowed=False,  # Default to not allowed
            )
            await self.users.create_user(user)

        return user

    async def create_session(
        self, user_id: int, project_path: str, session_id: str
    ) -> SessionModel:
        """Create new session."""
        session = SessionModel(
            session_id=session_id,
            user_id=user_id,
            project_path=project_path,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
        )

        await self.sessions.create_session(session)
        await self.session_events.save_event(
            SessionEventModel(
                id=None,
                session_id=session_id,
                event_type="session_created",
                event_data={
                    "user_id": user_id,
                    "project_path": project_path,
                },
                created_at=datetime.utcnow(),
            )
        )

        # Update user session count
        user = await self.users.get_user(user_id)
        if user:
            user.session_count += 1
            await self.users.update_user(user)

        return session

    async def log_security_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
        ip_address: Optional[str] = None,
    ) -> None:
        """Log security-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.utcnow(),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def log_bot_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
    ) -> None:
        """Log bot-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.utcnow(),
        )
        await self.audit.log_event(audit_event)

    # Convenience methods

    async def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is allowed."""
        user = await self.users.get_user(user_id)
        return user.is_allowed if user else False

    async def get_user_session_summary(self, user_id: int) -> Dict[str, Any]:
        """Get user session summary."""
        sessions = await self.sessions.get_user_sessions(user_id, active_only=False)
        active_sessions = [s for s in sessions if s.is_active]

        return {
            "total_sessions": len(sessions),
            "active_sessions": len(active_sessions),
            "total_cost": sum(s.total_cost for s in sessions),
            "total_messages": sum(s.message_count for s in sessions),
            "projects": list(set(s.project_path for s in sessions)),
        }

    async def get_session_history(
        self, session_id: str, limit: int = 50
    ) -> Optional[Dict[str, Any]]:
        """Get session history with messages and tools."""
        session = await self.sessions.get_session(session_id)
        if not session:
            return None

        messages = await self.messages.get_session_messages(session_id, limit)
        tools = await self.tools.get_session_tool_usage(session_id)
        events = await self.session_events.get_session_events(session_id, limit=limit)

        return {
            "session": session.to_dict(),
            "messages": [m.to_dict() for m in messages],
            "tool_usage": [t.to_dict() for t in tools],
            "events": [e.to_dict() for e in events],
        }

    async def cleanup_old_data(self, days: int = 30) -> Dict[str, int]:
        """Cleanup old data."""
        logger.info("Starting data cleanup", days=days)

        # Cleanup old sessions
        sessions_cleaned = await self.sessions.cleanup_old_sessions(days)

        logger.info("Data cleanup complete", sessions_cleaned=sessions_cleaned)

        return {"sessions_cleaned": sessions_cleaned}

    async def get_user_dashboard(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get comprehensive user dashboard data."""
        # Get user info
        user = await self.users.get_user(user_id)
        if not user:
            return None

        # Get user stats
        stats = await self.analytics.get_user_stats(user_id)

        # Get recent sessions
        sessions = await self.sessions.get_user_sessions(user_id, active_only=True)

        # Get recent messages
        messages = await self.messages.get_user_messages(user_id, limit=10)

        # Get recent audit log
        audit_logs = await self.audit.get_user_audit_log(user_id, limit=20)

        # Get daily costs
        daily_costs = await self.costs.get_user_daily_costs(user_id, days=30)

        return {
            "user": user.to_dict(),
            "stats": stats,
            "recent_sessions": [s.to_dict() for s in sessions[:5]],
            "recent_messages": [m.to_dict() for m in messages],
            "recent_audit": [a.to_dict() for a in audit_logs],
            "daily_costs": [c.to_dict() for c in daily_costs],
        }

    async def get_admin_dashboard(self) -> Dict[str, Any]:
        """Get admin dashboard data."""
        # Get system stats
        system_stats = await self.analytics.get_system_stats()

        # Get all users
        users = await self.users.get_all_users()

        # Get recent audit log
        recent_audit = await self.audit.get_recent_audit_log(hours=24)

        # Get total costs
        total_costs = await self.costs.get_total_costs(days=30)

        # Get tool stats
        tool_stats = await self.tools.get_tool_stats()

        return {
            "system_stats": system_stats,
            "users": [u.to_dict() for u in users],
            "recent_audit": [a.to_dict() for a in recent_audit],
            "total_costs": total_costs,
            "tool_stats": tool_stats,
        }

    @staticmethod
    def _build_interaction_events(
        *,
        user_id: int,
        session_id: str,
        prompt: str,
        message_id: int,
        response: ClaudeResponse,
    ) -> list[SessionEventModel]:
        """Build semantic events from one Claude interaction."""
        created_at = datetime.utcnow()
        clipped_prompt = Storage._clip_text(prompt)
        clipped_response = Storage._clip_text(response.content)

        events: list[SessionEventModel] = [
            SessionEventModel(
                id=None,
                session_id=session_id,
                event_type="command_exec",
                event_data={
                    "user_id": user_id,
                    "message_id": message_id,
                    "prompt": clipped_prompt,
                    "prompt_length": len(prompt),
                    "duration_ms": response.duration_ms,
                    "cost": response.cost,
                    "num_turns": response.num_turns,
                    "is_error": response.is_error,
                    "error_type": response.error_type,
                },
                created_at=created_at,
            ),
            SessionEventModel(
                id=None,
                session_id=session_id,
                event_type="assistant_text",
                event_data={
                    "message_id": message_id,
                    "content": clipped_response,
                    "content_length": len(response.content),
                    "content_truncated": len(clipped_response) < len(response.content),
                },
                created_at=created_at,
            ),
        ]

        for tool in response.tools_used:
            tool_name = tool.get("name", "unknown")
            tool_input = tool.get("input", {})
            events.append(
                SessionEventModel(
                    id=None,
                    session_id=session_id,
                    event_type="tool_call",
                    event_data={
                        "message_id": message_id,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                    },
                    created_at=created_at,
                )
            )
            events.append(
                SessionEventModel(
                    id=None,
                    session_id=session_id,
                    event_type="tool_result",
                    event_data={
                        "message_id": message_id,
                        "tool_name": tool_name,
                        "success": not response.is_error,
                        "error_type": response.error_type,
                    },
                    created_at=created_at,
                )
            )

        if response.is_error:
            events.append(
                SessionEventModel(
                    id=None,
                    session_id=session_id,
                    event_type="error",
                    event_data={
                        "message_id": message_id,
                        "error_type": response.error_type,
                        "content": clipped_response,
                    },
                    created_at=created_at,
                )
            )

        return events

    @staticmethod
    def _clip_text(text: str, max_chars: int = 4000) -> str:
        """Clip long text payloads to control session event row size."""
        if len(text) <= max_chars:
            return text
        return text[:max_chars]
