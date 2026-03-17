"""Cron/reminder scheduler service."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import structlog
from telegram.ext import Application, ContextTypes

from ..bot.utils.cli_engine import ENGINE_CLAUDE, normalize_cli_engine
from ..bot.utils.formatting import ResponseFormatter
from ..bot.utils.telegram_send import send_message_resilient
from ..config.settings import Settings
from ..security.audit import AuditLogger
from ..storage.models import CronJobModel, CronRunModel
from ..storage.repositories import CronJobRepository, CronRunRepository

try:
    from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - guarded at runtime
    CronTrigger = None  # type: ignore[assignment]

logger = structlog.get_logger()

CRON_JOB_TYPE_REMINDER = "reminder"
CRON_JOB_TYPE_AI_PROMPT = "ai_prompt"
CRON_SCHEDULE_ONCE = "once"
CRON_SCHEDULE_CRON = "cron"

CRON_STATUS_ENABLED = "enabled"
CRON_STATUS_PAUSED = "paused"
CRON_STATUS_COMPLETED = "completed"
CRON_STATUS_DELETED = "deleted"

_CN_DIGIT_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_ALLOWED_CHARS = set(_CN_DIGIT_MAP.keys()) | {"十"}

_REMINDER_AFTER_PREFIX_RE = re.compile(
    r"^(?P<num>[0-9零〇一二两三四五六七八九十]+)\s*"
    r"(?P<unit>分钟|分|小时|时|天)\s*(?:后|之后)\s*"
    r"(?:提醒我|记得提醒我)\s*(?P<content>.+)$",
    re.IGNORECASE,
)
_REMINDER_AFTER_SUFFIX_RE = re.compile(
    r"^(?:提醒我|记得提醒我)\s*(?P<content>.+?)\s*"
    r"(?P<num>[0-9零〇一二两三四五六七八九十]+)\s*"
    r"(?P<unit>分钟|分|小时|时|天)\s*(?:后|之后)$",
    re.IGNORECASE,
)


class CronValidationError(ValueError):
    """Raised when cron/reminder input is invalid."""


@dataclass
class ParsedReminder:
    """Parsed natural-language reminder payload."""

    run_at: datetime
    content: str


def _utc_now() -> datetime:
    return datetime.utcnow()


def _to_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _from_utc_naive(value: Optional[datetime], tz: ZoneInfo) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(tz)
    return value.replace(tzinfo=timezone.utc).astimezone(tz)


def _parse_cn_number(raw: str) -> Optional[int]:
    token = str(raw or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if any(ch not in _CN_ALLOWED_CHARS for ch in token):
        return None
    if token == "十":
        return 10
    if "十" not in token:
        value = 0
        for ch in token:
            value = value * 10 + _CN_DIGIT_MAP[ch]
        return value
    left, right = token.split("十", 1)
    tens = _CN_DIGIT_MAP[left] if left else 1
    ones = _CN_DIGIT_MAP[right] if right else 0
    return tens * 10 + ones


def _timedelta_for_unit(value: int, unit: str) -> Optional[timedelta]:
    normalized = str(unit or "").strip()
    if normalized in {"分钟", "分"}:
        return timedelta(minutes=value)
    if normalized in {"小时", "时"}:
        return timedelta(hours=value)
    if normalized == "天":
        return timedelta(days=value)
    return None


def parse_relative_reminder(
    text: str, *, now: datetime, timezone_hint: ZoneInfo
) -> Optional[ParsedReminder]:
    """Parse relative Chinese reminder text.

    Supported examples:
    - `5分钟后提醒我拿奶茶`
    - `提醒我拿奶茶 5分钟后`
    """
    content_text = str(text or "").strip()
    if not content_text or "提醒我" not in content_text:
        return None

    match = _REMINDER_AFTER_PREFIX_RE.match(content_text)
    if not match:
        match = _REMINDER_AFTER_SUFFIX_RE.match(content_text)
    if not match:
        return None

    amount = _parse_cn_number(match.group("num"))
    if amount is None or amount <= 0:
        return None

    delta = _timedelta_for_unit(amount, match.group("unit"))
    if delta is None:
        return None

    reminder_content = str(match.group("content") or "").strip(" ，。！？,.!?")
    if not reminder_content:
        return None

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc).astimezone(timezone_hint)
    else:
        now = now.astimezone(timezone_hint)

    return ParsedReminder(run_at=now + delta, content=reminder_content)


class CronSchedulerService:
    """Persistent scheduler for reminders and cron jobs."""

    def __init__(
        self,
        *,
        settings: Settings,
        cron_jobs: CronJobRepository,
        cron_runs: CronRunRepository,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self.settings = settings
        self.cron_jobs = cron_jobs
        self.cron_runs = cron_runs
        self.audit_logger = audit_logger
        self._app: Optional[Application] = None
        self._jobs: dict[int, Any] = {}
        self._lock = asyncio.Lock()
        self._timezone = ZoneInfo(str(settings.cron_timezone or "Asia/Shanghai"))

    @property
    def timezone(self) -> ZoneInfo:
        return self._timezone

    async def bootstrap(self, app: Application) -> None:
        """Attach scheduler to application and restore enabled jobs."""
        self._app = app
        if not self.settings.cron_enabled:
            logger.info("Cron scheduler disabled by settings")
            return
        if not app.job_queue:
            logger.warning("Job queue unavailable, cron scheduler disabled")
            return
        if CronTrigger is None:
            logger.warning(
                "apscheduler not available, recurring cron jobs disabled"
            )

        enabled_jobs = await self.cron_jobs.list_enabled_jobs()
        for job in enabled_jobs:
            try:
                await self._schedule_job(job)
            except Exception as exc:
                logger.error(
                    "Failed to restore cron job",
                    job_id=job.id,
                    error=str(exc),
                )

        logger.info("Cron scheduler bootstrapped", restored_jobs=len(enabled_jobs))

    async def create_natural_language_reminder(
        self,
        *,
        text: str,
        user_id: int,
        chat_id: int,
        thread_id: int,
        scope_key: str,
        project_dir: Path,
    ) -> Optional[CronJobModel]:
        """Try parse and create one-shot reminder from text message."""
        parsed = parse_relative_reminder(
            text=text,
            now=datetime.now(self._timezone),
            timezone_hint=self._timezone,
        )
        if parsed is None:
            return None

        now_local = datetime.now(self._timezone)
        min_delay = timedelta(seconds=self.settings.cron_nl_min_delay_seconds)
        if parsed.run_at - now_local < min_delay:
            raise CronValidationError(
                f"提醒时间太近，请至少设置 {self.settings.cron_nl_min_delay_seconds} 秒后。"
            )

        return await self.create_one_shot_reminder(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            scope_key=scope_key,
            project_dir=project_dir,
            reminder_text=parsed.content,
            run_at=parsed.run_at,
        )

    async def create_one_shot_reminder(
        self,
        *,
        user_id: int,
        chat_id: int,
        thread_id: int,
        scope_key: str,
        project_dir: Path,
        reminder_text: str,
        run_at: datetime,
    ) -> CronJobModel:
        """Create one-shot reminder job."""
        await self._enforce_user_job_limit(user_id)
        run_at_utc = _to_utc_naive(run_at)
        now_utc = _utc_now()
        if run_at_utc is None or run_at_utc <= now_utc:
            raise CronValidationError("提醒时间必须晚于当前时间。")

        job = CronJobModel(
            user_id=user_id,
            job_type=CRON_JOB_TYPE_REMINDER,
            schedule_type=CRON_SCHEDULE_ONCE,
            payload_text=str(reminder_text or "").strip(),
            chat_id=chat_id,
            thread_id=int(thread_id or 0),
            scope_key=scope_key,
            project_dir=str(project_dir),
            run_at=run_at_utc,
            status=CRON_STATUS_ENABLED,
            next_run_at=run_at_utc,
        )
        created = await self.cron_jobs.create_job(job)
        await self._schedule_job(created)
        await self._log_command(
            user_id=user_id, command="cron_nl", args=[str(created.id)]
        )
        return created

    async def create_cron_job(
        self,
        *,
        user_id: int,
        chat_id: int,
        thread_id: int,
        scope_key: str,
        project_dir: Path,
        job_type: str,
        cron_expr: str,
        payload_text: str,
        engine: Optional[str] = None,
    ) -> CronJobModel:
        """Create recurring cron job."""
        await self._enforce_user_job_limit(user_id)
        normalized_expr = " ".join(str(cron_expr or "").split())
        if not normalized_expr:
            raise CronValidationError("cron 表达式不能为空。")
        self._validate_cron_expr(normalized_expr)
        self._validate_min_interval(normalized_expr)

        normalized_type = str(job_type or "").strip().lower()
        if normalized_type not in {CRON_JOB_TYPE_REMINDER, CRON_JOB_TYPE_AI_PROMPT}:
            raise CronValidationError("任务类型仅支持 reminder 或 ai。")

        normalized_engine: Optional[str] = None
        if normalized_type == CRON_JOB_TYPE_AI_PROMPT:
            normalized_engine = normalize_cli_engine(engine)

        job = CronJobModel(
            user_id=user_id,
            job_type=normalized_type,
            schedule_type=CRON_SCHEDULE_CRON,
            payload_text=str(payload_text or "").strip(),
            chat_id=chat_id,
            thread_id=int(thread_id or 0),
            scope_key=scope_key,
            project_dir=str(project_dir),
            cron_expr=normalized_expr,
            engine=normalized_engine,
            status=CRON_STATUS_ENABLED,
        )
        created = await self.cron_jobs.create_job(job)
        await self._schedule_job(created)
        await self._log_command(
            user_id=user_id, command="cron_add", args=[str(created.id)]
        )
        return created

    async def list_user_jobs(self, *, user_id: int) -> list[CronJobModel]:
        """List jobs for one user."""
        return await self.cron_jobs.list_jobs(user_id=user_id, include_inactive=True)

    async def pause_job(self, *, user_id: int, job_id: int) -> bool:
        """Pause one job."""
        changed = await self.cron_jobs.set_status(
            job_id=job_id,
            user_id=user_id,
            status=CRON_STATUS_PAUSED,
            next_run_at=None,
            last_error=None,
        )
        if changed:
            await self._unschedule_job(job_id)
            await self._log_command(
                user_id=user_id, command="cron_pause", args=[str(job_id)]
            )
        return changed

    async def resume_job(self, *, user_id: int, job_id: int) -> bool:
        """Resume one paused job."""
        job = await self.cron_jobs.get_job(job_id, user_id=user_id)
        if not job or job.status != CRON_STATUS_PAUSED:
            return False
        changed = await self.cron_jobs.set_status(
            job_id=job_id,
            user_id=user_id,
            status=CRON_STATUS_ENABLED,
            next_run_at=job.next_run_at,
            last_error=job.last_error,
        )
        if changed:
            refreshed = await self.cron_jobs.get_job(job_id, user_id=user_id)
            if refreshed:
                await self._schedule_job(refreshed)
            await self._log_command(
                user_id=user_id, command="cron_resume", args=[str(job_id)]
            )
        return changed

    async def delete_job(self, *, user_id: int, job_id: int) -> bool:
        """Delete one job."""
        changed = await self.cron_jobs.mark_deleted(job_id=job_id, user_id=user_id)
        if changed:
            await self._unschedule_job(job_id)
            await self._log_command(
                user_id=user_id, command="cron_delete", args=[str(job_id)]
            )
        return changed

    async def _schedule_job(self, job: CronJobModel) -> None:
        if not self.settings.cron_enabled:
            return
        if job.id is None:
            return
        if job.status != CRON_STATUS_ENABLED:
            await self._unschedule_job(job.id)
            return
        app = self._app
        if app is None or not app.job_queue:
            return

        async with self._lock:
            await self._unschedule_job(job.id)
            job_queue = app.job_queue
            name = f"cron_job_{job.id}"
            if job.schedule_type == CRON_SCHEDULE_ONCE:
                run_at_local = _from_utc_naive(job.run_at, self._timezone)
                if run_at_local is None:
                    raise CronValidationError("一次性任务缺少触发时间。")
                now_local = datetime.now(self._timezone)
                if run_at_local <= now_local:
                    run_at_local = now_local + timedelta(seconds=1)
                tg_job = job_queue.run_once(
                    self._job_callback,
                    when=run_at_local,
                    name=name,
                    data={"job_id": job.id},
                    chat_id=job.chat_id,
                    user_id=job.user_id,
                )
            else:
                if CronTrigger is None:
                    raise CronValidationError("apscheduler is unavailable")
                if not job.cron_expr:
                    raise CronValidationError("周期任务缺少 cron 表达式。")
                trigger = CronTrigger.from_crontab(
                    job.cron_expr, timezone=self._timezone
                )
                tg_job = job_queue.run_custom(
                    self._job_callback,
                    name=name,
                    data={"job_id": job.id},
                    chat_id=job.chat_id,
                    user_id=job.user_id,
                    job_kwargs={"trigger": trigger},
                )
            self._jobs[job.id] = tg_job

            await self.cron_jobs.update_runtime(
                job_id=job.id,
                last_run_at=job.last_run_at,
                next_run_at=self._extract_next_run_at(tg_job),
                fail_count=job.fail_count,
                last_error=job.last_error,
                session_id=job.session_id,
            )

    async def _unschedule_job(self, job_id: int) -> None:
        scheduled = self._jobs.pop(job_id, None)
        if scheduled is not None:
            try:
                scheduled.schedule_removal()
            except Exception:
                pass

    async def _job_callback(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        payload = getattr(getattr(context, "job", None), "data", None) or {}
        raw_job_id = payload.get("job_id")
        if not isinstance(raw_job_id, int):
            return

        job = await self.cron_jobs.get_job(raw_job_id)
        if not job or job.status != CRON_STATUS_ENABLED or job.id is None:
            return

        started_at = _utc_now()
        success = False
        output_preview: Optional[str] = None
        error_message: Optional[str] = None
        session_id = job.session_id

        try:
            self._ensure_user_still_allowed(job.user_id)
            output_preview, session_id = await self._execute_job(job, context)
            success = True
        except Exception as exc:
            error_message = str(exc)
            logger.error(
                "Cron job execution failed",
                job_id=job.id,
                user_id=job.user_id,
                error=error_message,
            )

        finished_at = _utc_now()
        await self.cron_runs.create_run(
            CronRunModel(
                job_id=job.id,
                user_id=job.user_id,
                started_at=started_at,
                finished_at=finished_at,
                success=success,
                output_preview=output_preview,
                error_message=error_message,
            )
        )

        if success:
            if job.schedule_type == CRON_SCHEDULE_ONCE:
                await self.cron_jobs.set_status(
                    job_id=job.id,
                    status=CRON_STATUS_COMPLETED,
                    next_run_at=None,
                    last_error=None,
                )
                await self._unschedule_job(job.id)
            else:
                scheduled = self._jobs.get(job.id)
                await self.cron_jobs.update_runtime(
                    job_id=job.id,
                    last_run_at=finished_at,
                    next_run_at=self._extract_next_run_at(scheduled),
                    fail_count=0,
                    last_error=None,
                    session_id=session_id,
                )
            return

        fail_count = int(job.fail_count or 0) + 1
        if job.schedule_type == CRON_SCHEDULE_ONCE:
            await self.cron_jobs.set_status(
                job_id=job.id,
                status=CRON_STATUS_COMPLETED,
                next_run_at=None,
                last_error=error_message,
            )
            await self._unschedule_job(job.id)
        else:
            scheduled = self._jobs.get(job.id)
            await self.cron_jobs.update_runtime(
                job_id=job.id,
                last_run_at=finished_at,
                next_run_at=self._extract_next_run_at(scheduled),
                fail_count=fail_count,
                last_error=error_message,
                session_id=session_id,
            )
        await self._send_failure_message(
            context=context, job=job, error_message=error_message
        )

    async def _execute_job(
        self, job: CronJobModel, context: ContextTypes.DEFAULT_TYPE
    ) -> tuple[Optional[str], Optional[str]]:
        if job.job_type == CRON_JOB_TYPE_REMINDER:
            text = f"⏰ 提醒：{job.payload_text}"
            await send_message_resilient(
                bot=context.bot,
                chat_id=job.chat_id,
                text=text,
                message_thread_id=job.thread_id,
                chat_type=None,
            )
            return job.payload_text[:200], job.session_id

        if job.job_type != CRON_JOB_TYPE_AI_PROMPT:
            raise RuntimeError(f"Unsupported cron job type: {job.job_type}")

        bot_data = context.bot_data
        integrations = bot_data.get("cli_integrations") or {}
        engine = normalize_cli_engine(job.engine)
        cli_integration = integrations.get(engine) or integrations.get(ENGINE_CLAUDE)
        if cli_integration is None:
            raise RuntimeError(f"CLI integration unavailable for engine: {engine}")

        approved_dir = Path(self.settings.approved_directory).resolve()
        project_dir = Path(job.project_dir).resolve()
        if not project_dir.is_relative_to(approved_dir):
            raise RuntimeError(
                "cron job working directory is outside approved directory"
            )

        task_registry = bot_data.get("task_registry")
        if task_registry and await task_registry.is_busy(
            job.user_id, scope_key=job.scope_key
        ):
            raise RuntimeError("scope is busy, skip this cron execution")

        run_coro = cli_integration.run_command(
            prompt=job.payload_text,
            working_directory=project_dir,
            user_id=job.user_id,
            session_id=job.session_id,
        )
        task = asyncio.create_task(run_coro)
        if task_registry:
            await task_registry.register(
                user_id=job.user_id,
                task=task,
                prompt_summary=f"[cron#{job.id}] {job.payload_text[:80]}",
                scope_key=job.scope_key,
            )
        try:
            response = await task
            if task_registry:
                await task_registry.complete(job.user_id, scope_key=job.scope_key)
        except Exception:
            if task_registry:
                await task_registry.fail(job.user_id, scope_key=job.scope_key)
            raise
        finally:
            if task_registry:
                await task_registry.remove(job.user_id, scope_key=job.scope_key)

        formatter = ResponseFormatter(self.settings)
        messages = formatter.format_claude_response(
            str(getattr(response, "content", "") or "")
        )
        if not messages:
            messages = [formatter.format_info_message("(empty response)")]

        for msg in messages:
            await send_message_resilient(
                bot=context.bot,
                chat_id=job.chat_id,
                text=msg.text,
                parse_mode=msg.parse_mode,
                reply_markup=msg.reply_markup,
                message_thread_id=job.thread_id,
                chat_type=None,
            )

        preview = str(getattr(response, "content", "") or "").strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        return preview or None, getattr(response, "session_id", None)

    async def _send_failure_message(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        job: CronJobModel,
        error_message: Optional[str],
    ) -> None:
        text = (
            f"⚠️ Cron 任务 #{job.id} 执行失败。\n"
            f"原因：{str(error_message or 'unknown error')[:300]}"
        )
        try:
            await send_message_resilient(
                bot=context.bot,
                chat_id=job.chat_id,
                text=text,
                message_thread_id=job.thread_id,
                chat_type=None,
            )
        except Exception as exc:
            logger.warning(
                "Failed to send cron failure message",
                job_id=job.id,
                error=str(exc),
            )

    def _validate_cron_expr(self, cron_expr: str) -> None:
        if CronTrigger is None:
            raise CronValidationError("apscheduler is unavailable")
        try:
            CronTrigger.from_crontab(cron_expr, timezone=self._timezone)
        except Exception as exc:
            raise CronValidationError(f"非法 cron 表达式: {exc}") from exc

    def _validate_min_interval(self, cron_expr: str) -> None:
        if CronTrigger is None:
            return
        trigger = CronTrigger.from_crontab(cron_expr, timezone=self._timezone)
        anchor = datetime.now(self._timezone).replace(second=0, microsecond=0)
        first = trigger.get_next_fire_time(None, anchor)
        if first is None:
            raise CronValidationError("cron 表达式没有可执行时间。")
        second = trigger.get_next_fire_time(first, first + timedelta(seconds=1))
        if second is None:
            return
        interval = second - first
        min_interval = timedelta(minutes=self.settings.cron_min_interval_minutes)
        if interval < min_interval:
            raise CronValidationError(
                f"周期任务最小间隔为 {self.settings.cron_min_interval_minutes} 分钟。"
            )

    async def _enforce_user_job_limit(self, user_id: int) -> None:
        active_count = await self.cron_jobs.count_user_active_jobs(user_id=user_id)
        if active_count >= self.settings.cron_max_jobs_per_user:
            raise CronValidationError(
                f"已达到任务上限（{self.settings.cron_max_jobs_per_user}）。"
            )

    def _ensure_user_still_allowed(self, user_id: int) -> None:
        allowed_users = self.settings.allowed_users or []
        if allowed_users and user_id not in allowed_users:
            raise RuntimeError("user is no longer allowed to run this bot")

    def _extract_next_run_at(self, scheduled_job: Any) -> Optional[datetime]:
        if scheduled_job is None:
            return None
        next_run = getattr(scheduled_job, "next_t", None)
        if next_run is None:
            next_run = getattr(scheduled_job, "next_run_time", None)
        if not isinstance(next_run, datetime):
            return None
        return _to_utc_naive(next_run)

    async def _log_command(
        self, *, user_id: int, command: str, args: list[str]
    ) -> None:
        if not self.audit_logger:
            return
        await self.audit_logger.log_command(
            user_id=user_id,
            command=command,
            args=args,
            success=True,
        )
