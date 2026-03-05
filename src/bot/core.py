"""Main Telegram bot class.

Features:
- Command registration
- Handler management
- Context injection
- Graceful shutdown
"""

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog
from telegram import Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    TypeHandler,
    filters,
)

from ..claude.task_registry import TaskRegistry
from ..config.settings import Settings
from ..exceptions import ClaudeCodeTelegramError
from ..security.validators import SecurityValidator
from ..storage.facade import Storage
from .features.registry import FeatureRegistry
from .inbound_task_queue import InboundTaskQueue
from .utils.cli_engine import ENGINE_CLAUDE
from .utils.command_menu import build_bot_commands_for_engine
from .utils.telegram_send import send_message_resilient
from .utils.update_dedupe import UpdateDedupeCache
from .utils.update_offset_store import UpdateOffsetStore

logger = structlog.get_logger()

_POLLING_WATCHDOG_INTERVAL_SECONDS = 2.0
_POLLING_RECOVERY_ERROR_THRESHOLD = 3
_DUPLICATE_UPDATE_RECOVERY_THRESHOLD = 3
_POLLING_RESTART_COOLDOWN_SECONDS = 8.0
_POLLING_STALL_PENDING_PROBE_INTERVAL_SECONDS = 15.0
_POLLING_STALL_IDLE_THRESHOLD_SECONDS = 90.0
_POLLING_STALL_PENDING_UPDATE_THRESHOLD = 1


class ClaudeCodeBot:
    """Main bot orchestrator."""

    def __init__(self, settings: Settings, dependencies: Dict[str, Any]):
        """Initialize bot with settings and dependencies."""
        self.settings = settings
        self.deps = dependencies
        self.app: Optional[Application] = None
        self.is_running = False
        self.feature_registry: Optional[FeatureRegistry] = None
        # Polling error tracking for rate-limited logging
        self._polling_error_count: int = 0
        self._polling_error_window_start: float = 0.0
        self._last_polling_error_log: float = 0.0
        self._polling_restart_requested: bool = False
        self._last_polling_restart_monotonic: float = 0.0
        self._last_update_activity_monotonic: float = 0.0
        self._last_pending_probe_monotonic: float = 0.0
        self._last_pending_update_count: int = 0
        # Update dedupe and persisted offset tracking
        self._update_dedupe_cache = UpdateDedupeCache(ttl_seconds=300, max_size=5000)
        self._update_offset_store: Optional[UpdateOffsetStore] = None
        self._startup_min_update_id: Optional[int] = None
        self._duplicate_update_id: Optional[int] = None
        self._duplicate_update_repeat_count: int = 0

    def _require_app(self) -> Application:
        """Return initialized Telegram application or raise."""
        if self.app is None:
            raise ClaudeCodeTelegramError("Telegram application is not initialized")
        return self.app

    async def initialize(self) -> None:
        """Initialize bot application."""
        logger.info("Initializing Telegram bot")

        # Create application
        builder = Application.builder()
        builder.token(self.settings.telegram_token_str)

        # Configure connection settings
        builder.connect_timeout(30)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.pool_timeout(30)

        # Enable concurrent update processing so that permission button
        # callbacks can be handled while a Claude request is waiting for
        # user approval (without this the default serial processing causes
        # a deadlock where the callback_query update is queued behind the
        # blocked message update).
        builder.concurrent_updates(True)

        self.app = builder.build()
        app = self._require_app()

        # Initialize feature registry
        storage = self.deps.get("storage")
        security = self.deps.get("security_validator") or self.deps.get("security")
        if not isinstance(storage, Storage):
            raise ClaudeCodeTelegramError("Missing or invalid storage dependency")
        if not isinstance(security, SecurityValidator):
            raise ClaudeCodeTelegramError("Missing or invalid security dependency")
        self.feature_registry = FeatureRegistry(
            config=self.settings,
            storage=storage,
            security=security,
        )

        # Add feature registry to dependencies
        self.deps["features"] = self.feature_registry

        # Initialize task registry for cancel support
        self.deps["task_registry"] = TaskRegistry()
        self.deps["inbound_task_queue"] = InboundTaskQueue()
        self._initialize_update_tracking()

        # Set bot commands for menu
        await self._set_bot_commands()

        # Register handlers
        self._register_handlers()

        # Add middleware
        self._add_middleware()

        # Set error handler
        app.add_error_handler(self._error_handler)

        # Schedule periodic image cleanup
        self._schedule_image_cleanup()

        # Check .gitignore for .claude-images/
        self._check_gitignore()

        logger.info("Bot initialization complete")

    async def _set_bot_commands(self) -> None:
        """Set bot command menu (non-fatal on failure)."""
        app = self._require_app()
        try:
            commands = build_bot_commands_for_engine(ENGINE_CLAUDE)
            await app.bot.set_my_commands(commands)
            logger.info("Bot commands set", commands=[cmd.command for cmd in commands])
        except Exception as e:
            logger.warning(
                "Failed to set bot commands, will retry on next startup",
                error=str(e),
                error_type=type(e).__name__,
            )

    def _register_handlers(self) -> None:
        """Register all command and message handlers."""
        from .handlers import callback, command, message

        app = self._require_app()

        # Global update guard (dedupe + stale offset filtering)
        app.add_handler(
            TypeHandler(Update, self._handle_update_guard),
            group=-10,
        )

        # Command handlers
        handlers = [
            ("help", command.help_command),
            ("new", command.new_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("projects", command.show_projects),
            ("context", command.session_status),
            ("status", command.status_command),
            ("engine", command.switch_engine),
            ("export", command.export_session),
            ("git", command.git_command),
            ("cancel", command.cancel_task),
            ("queue", command.queue_status_command),
            ("dequeue", command.dequeue_command),
            ("resume", command.resume_command),
            ("model", command.model_command),
            ("codexdiag", command.codex_diag_command),
            ("provider", command.switch_provider),
        ]

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Message handlers with priority groups
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )

        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )

        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )

        # Message reaction handler (emoji reactions on messages)
        app.add_handler(
            MessageReactionHandler(
                self._inject_deps(message.handle_message_reaction),
                message_reaction_types=(
                    MessageReactionHandler.MESSAGE_REACTION_UPDATED
                    | MessageReactionHandler.MESSAGE_REACTION_COUNT_UPDATED
                ),
            ),
            group=10,
        )
        # Generic fallback for reaction updates in case specialized handler misses.
        app.add_handler(
            TypeHandler(
                Update,
                self._inject_deps(message.handle_reaction_update_fallback),
            ),
            group=10,
        )

        # Callback query handler
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Bot handlers registered")

    def _build_update_offset_state_file(self) -> Optional[Path]:
        """Build persisted update offset state file path."""
        approved_directory = getattr(self.settings, "approved_directory", None)
        if not isinstance(approved_directory, Path):
            return None
        return approved_directory / "data/state/telegram/update-offset.json"

    def _initialize_update_tracking(self) -> None:
        """Initialize update dedupe and persisted offset tracking."""
        state_file = self._build_update_offset_state_file()
        if state_file is None:
            logger.warning(
                "Approved directory missing, update offset persistence disabled"
            )
            self._update_offset_store = None
            self._startup_min_update_id = None
            return

        store = UpdateOffsetStore(state_file)
        self._update_offset_store = store

        try:
            last_update_id = store.load()
        except Exception as exc:
            logger.warning(
                "Failed to load Telegram update offset, starting without persisted offset",
                state_file=str(state_file),
                error=str(exc),
            )
            self._startup_min_update_id = None
            return

        self._startup_min_update_id = (
            last_update_id + 1 if isinstance(last_update_id, int) else None
        )
        logger.info(
            "Telegram update tracking initialized",
            state_file=str(state_file),
            last_update_id=last_update_id,
            startup_min_update_id=self._startup_min_update_id,
        )

    async def _handle_update_guard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Drop stale/duplicate updates before entering business handlers."""
        update_id = getattr(update, "update_id", None)
        if not isinstance(update_id, int):
            return
        self._mark_update_activity()

        if (
            self._startup_min_update_id is not None
            and update_id < self._startup_min_update_id
        ):
            logger.debug(
                "Skipping stale Telegram update below persisted offset",
                update_id=update_id,
                startup_min_update_id=self._startup_min_update_id,
            )
            raise ApplicationHandlerStop

        if self._update_dedupe_cache.check_and_mark(update_id):
            if self._duplicate_update_id == update_id:
                self._duplicate_update_repeat_count += 1
            else:
                self._duplicate_update_id = update_id
                self._duplicate_update_repeat_count = 1

            if (
                self._duplicate_update_repeat_count
                >= _DUPLICATE_UPDATE_RECOVERY_THRESHOLD
                and not self._polling_restart_requested
            ):
                self._polling_restart_requested = True
                logger.warning(
                    "Duplicate update self-recovery flagged",
                    update_id=update_id,
                    repeat_count=self._duplicate_update_repeat_count,
                    threshold=_DUPLICATE_UPDATE_RECOVERY_THRESHOLD,
                )

            logger.debug(
                "Skipping duplicate Telegram update",
                update_id=update_id,
                repeat_count=self._duplicate_update_repeat_count,
            )
            raise ApplicationHandlerStop

        self._reset_duplicate_update_recovery_state()

        if self._update_offset_store is not None:
            try:
                self._update_offset_store.record(update_id)
            except Exception as exc:
                logger.warning(
                    "Failed to persist Telegram update offset",
                    update_id=update_id,
                    error=str(exc),
                )

    def _inject_deps(self, handler: Callable) -> Callable:
        """Inject dependencies into handlers."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
            # Add dependencies to context
            for key, value in self.deps.items():
                context.bot_data[key] = value

            # Add settings
            context.bot_data["settings"] = self.settings

            return await handler(update, context)

        return wrapped

    def _add_middleware(self) -> None:
        """Add middleware to application."""
        from .middleware.auth import auth_middleware
        from .middleware.security import security_middleware

        app = self._require_app()

        # Middleware runs in order of group numbers (lower = earlier)
        # Security middleware first (validate inputs)
        app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(security_middleware)
            ),
            group=-3,
        )

        # Authentication second
        app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(auth_middleware)
            ),
            group=-2,
        )

        logger.info("Middleware added to bot")

    def _create_middleware_handler(self, middleware_func: Callable) -> Callable:
        """Create middleware handler that injects dependencies."""

        async def middleware_wrapper(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> Any:
            # Inject dependencies into context
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings

            # Create a dummy handler that does nothing (middleware will handle everything)
            async def dummy_handler(event: Any, data: Any) -> None:
                return None

            # Call middleware with Telegram-style parameters
            return await middleware_func(dummy_handler, update, context.bot_data)

        return middleware_wrapper

    def _schedule_image_cleanup(self) -> None:
        """Register periodic image cleanup job."""
        app = self._require_app()
        if not app.job_queue:
            logger.warning("Job queue not available, skipping image cleanup scheduling")
            return

        from .features.image_handler import ImageHandler

        async def _cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
            deleted = ImageHandler.cleanup_old_images(
                self.settings.approved_directory,
                self.settings.image_cleanup_max_age_hours,
            )
            if deleted:
                logger.info("Image cleanup completed", deleted=deleted)

        app.job_queue.run_repeating(
            _cleanup_job, interval=3600, first=60, name="image_cleanup"
        )
        logger.info("Image cleanup job scheduled", interval_hours=1)

    async def _finalize_running_tasks_before_shutdown(self) -> None:
        """Mark running tasks as interrupted and clear stale cancel buttons."""
        if not self.app:
            return
        task_registry = self.deps.get("task_registry")
        if not isinstance(task_registry, TaskRegistry):
            return

        running_tasks = await task_registry.list_running()
        if not running_tasks:
            return

        logger.info(
            "Finalizing running tasks before shutdown", count=len(running_tasks)
        )

        for active in running_tasks:
            try:
                await task_registry.cancel(active.user_id, scope_key=active.scope_key)
            except Exception as exc:
                logger.warning(
                    "Failed to cancel running task during shutdown",
                    user_id=active.user_id,
                    scope_key=active.scope_key,
                    error=str(exc),
                )

            if active.chat_id and active.progress_message_id:
                try:
                    await self.app.bot.edit_message_text(
                        chat_id=active.chat_id,
                        message_id=active.progress_message_id,
                        text="⚠️ 服务已重启，本次任务已中断。请重新发送消息继续。",
                        reply_markup=None,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to mark progress message as interrupted",
                        chat_id=active.chat_id,
                        message_id=active.progress_message_id,
                        error=str(exc),
                    )
                    try:
                        await self.app.bot.edit_message_reply_markup(
                            chat_id=active.chat_id,
                            message_id=active.progress_message_id,
                            reply_markup=None,
                        )
                    except Exception:
                        pass

            await task_registry.remove(active.user_id, scope_key=active.scope_key)

    def _check_gitignore(self) -> None:
        """Warn if .claude-images/ is not in .gitignore."""
        gitignore = self.settings.approved_directory / ".gitignore"
        if not gitignore.is_file():
            logger.warning(
                ".gitignore not found, consider adding .claude-images/ to it",
                dir=str(self.settings.approved_directory),
            )
            return
        try:
            content = gitignore.read_text(encoding="utf-8")
            if ".claude-images" not in content:
                logger.warning(
                    ".claude-images/ not in .gitignore, uploaded images may be committed",
                    gitignore=str(gitignore),
                )
        except OSError:
            pass

    def _reset_polling_recovery_state(self) -> None:
        """Reset polling network error counters after successful recovery."""
        self._polling_error_count = 0
        self._polling_error_window_start = 0.0
        self._last_polling_error_log = 0.0
        self._polling_restart_requested = False
        self._reset_duplicate_update_recovery_state()

    def _reset_duplicate_update_recovery_state(self) -> None:
        """Reset duplicate-update recovery trackers."""
        self._duplicate_update_id = None
        self._duplicate_update_repeat_count = 0

    def _mark_update_activity(self) -> None:
        """Mark latest local Telegram update processing activity timestamp."""
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            import time

            now = time.monotonic()
        self._last_update_activity_monotonic = now

    async def _start_polling(self, *, drop_pending_updates: bool) -> None:
        """Start Telegram polling with shared options."""
        app = self._require_app()
        updater = getattr(app, "updater", None)
        if updater is None:
            raise ClaudeCodeTelegramError("Telegram updater is not available")
        self._mark_update_activity()
        self._last_pending_probe_monotonic = 0.0
        self._last_pending_update_count = 0

        await updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=drop_pending_updates,
            bootstrap_retries=10,
            error_callback=self._polling_error_callback,
        )

    async def _restart_polling(self, *, reason: str) -> bool:
        """Restart polling loop after network/proxy disruptions."""
        app = self._require_app()
        updater = getattr(app, "updater", None)
        if updater is None:
            logger.error(
                "Cannot restart polling: updater is unavailable", reason=reason
            )
            return False

        now = asyncio.get_running_loop().time()
        if (
            now - self._last_polling_restart_monotonic
            < _POLLING_RESTART_COOLDOWN_SECONDS
        ):
            logger.debug(
                "Skip polling restart due to cooldown",
                reason=reason,
                cooldown_seconds=_POLLING_RESTART_COOLDOWN_SECONDS,
            )
            return False

        self._last_polling_restart_monotonic = now
        logger.warning("Attempting polling self-recovery", reason=reason)

        try:
            if updater.running:
                await updater.stop()

            # Clear in-memory dedupe state so a previously stuck update can be retried
            # after the polling transport is restarted.
            self._update_dedupe_cache.clear()
            self._reset_duplicate_update_recovery_state()

            # Keep pending Telegram updates during self-heal restart so
            # transient network issues do not drop user messages.
            await self._start_polling(drop_pending_updates=False)
        except Exception as exc:
            self._polling_restart_requested = True
            logger.error(
                "Polling self-recovery failed",
                reason=reason,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

        self._reset_polling_recovery_state()
        logger.info("Polling self-recovery succeeded", reason=reason)
        return True

    async def _polling_watchdog_tick(self) -> None:
        """Watch polling status and trigger self-recovery when needed."""
        if getattr(self.settings, "webhook_url", None) or self.app is None:
            return

        updater = getattr(self.app, "updater", None)
        if updater is None:
            return

        if not updater.running:
            await self._restart_polling(reason="updater_not_running")
            return

        if self._polling_restart_requested:
            await self._restart_polling(reason="network_error_threshold")
            return

        if await self._is_polling_stalled_by_pending_updates():
            await self._restart_polling(reason="pending_updates_stall")

    async def _is_polling_stalled_by_pending_updates(self) -> bool:
        """Detect silent polling stalls via pending updates + local idle time."""
        app = self.app
        if app is None:
            return False

        loop = asyncio.get_running_loop()
        now = loop.time()
        idle_seconds = now - self._last_update_activity_monotonic

        if idle_seconds < _POLLING_STALL_IDLE_THRESHOLD_SECONDS:
            return False

        if (
            now - self._last_pending_probe_monotonic
            < _POLLING_STALL_PENDING_PROBE_INTERVAL_SECONDS
        ):
            return False

        self._last_pending_probe_monotonic = now
        bot = getattr(app, "bot", None)
        if bot is None:
            return False

        try:
            webhook_info = await bot.get_webhook_info()
        except Exception as exc:
            logger.debug(
                "Pending-update stall probe failed",
                error=str(exc),
                idle_seconds=round(idle_seconds, 2),
            )
            return False

        raw_pending = getattr(webhook_info, "pending_update_count", 0)
        try:
            pending_count = int(raw_pending)
        except (TypeError, ValueError):
            pending_count = 0
        if pending_count < 0:
            pending_count = 0
        self._last_pending_update_count = pending_count

        if pending_count < _POLLING_STALL_PENDING_UPDATE_THRESHOLD:
            return False

        logger.warning(
            "Polling stall suspected from pending updates backlog",
            pending_update_count=pending_count,
            idle_seconds=round(idle_seconds, 2),
            idle_threshold_seconds=_POLLING_STALL_IDLE_THRESHOLD_SECONDS,
        )
        return True

    async def start(self) -> None:
        """Start the bot."""
        if self.is_running:
            logger.warning("Bot is already running")
            return

        await self.initialize()

        logger.info(
            "Starting bot", mode="webhook" if self.settings.webhook_url else "polling"
        )

        try:
            self.is_running = True
            app = self._require_app()

            if self.settings.webhook_url:
                # Webhook mode
                app.run_webhook(
                    listen="0.0.0.0",
                    port=self.settings.webhook_port,
                    url_path=self.settings.webhook_path,
                    webhook_url=self.settings.webhook_url,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
            else:
                # Polling mode - initialize and start polling manually
                await app.initialize()
                await app.start()
                # Cold process start keeps legacy behavior: discard backlog and
                # only serve fresh updates after boot.
                await self._start_polling(drop_pending_updates=True)
                self._reset_polling_recovery_state()

                # Keep running until manually stopped
                while self.is_running:
                    await asyncio.sleep(_POLLING_WATCHDOG_INTERVAL_SECONDS)
                    await self._polling_watchdog_tick()
        except Exception as e:
            logger.error("Error running bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to start bot: {str(e)}") from e
        finally:
            self.is_running = False

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if not self.is_running:
            logger.warning("Bot is not running")
            return

        logger.info("Stopping bot")

        try:
            self.is_running = False  # Stop the main loop first

            # Best effort: notify users and clear stale "Cancel" buttons
            # before the app is torn down.
            await self._finalize_running_tasks_before_shutdown()

            # Shutdown feature registry
            if self.feature_registry:
                self.feature_registry.shutdown()

            if self._update_offset_store is not None:
                try:
                    self._update_offset_store.flush(force=True)
                except Exception as exc:
                    logger.warning(
                        "Failed to flush Telegram update offset on shutdown",
                        error=str(exc),
                    )

            if self.app:
                app = self._require_app()
                # Stop the updater if it's running
                updater = getattr(app, "updater", None)
                if updater and updater.running:
                    await updater.stop()

                # Stop the application
                await app.stop()
                await app.shutdown()

            logger.info("Bot stopped successfully")
        except Exception as e:
            logger.error("Error stopping bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to stop bot: {str(e)}") from e

    def _polling_error_callback(self, error: Exception) -> None:
        """Handle network errors during polling (sync callback, required by PTB)."""
        import time

        now = time.monotonic()

        # Reset sliding window (60s window)
        if now - self._polling_error_window_start > 60:
            self._polling_error_count = 0
            self._polling_error_window_start = now

        self._polling_error_count += 1

        if (
            self._polling_error_count >= _POLLING_RECOVERY_ERROR_THRESHOLD
            and not self._polling_restart_requested
        ):
            self._polling_restart_requested = True
            logger.warning(
                "Polling self-recovery flagged due to repeated network errors",
                error_count_in_window=self._polling_error_count,
                threshold=_POLLING_RECOVERY_ERROR_THRESHOLD,
            )

        # Rate limit: at most one log entry per 30 seconds
        if now - self._last_polling_error_log < 30:
            return

        self._last_polling_error_log = now
        log_fn = logger.error if self._polling_error_count > 5 else logger.warning
        log_fn(
            "Polling network error (PTB will retry automatically)",
            error=str(error),
            error_type=type(error).__name__,
            error_count_in_window=self._polling_error_count,
        )

    async def _reply_update_message_resilient(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> Any:
        """Reply to effective message with fallback to resilient send helper."""
        message = getattr(update, "effective_message", None)
        if message is None:
            return None

        try:
            return await message.reply_text(text)
        except Exception:
            bot = getattr(context, "bot", None)
            if bot is None and self.app is not None:
                bot = self.app.bot

            chat = getattr(update, "effective_chat", None)
            chat_id = getattr(chat, "id", None)
            if bot is None or not isinstance(chat_id, int):
                raise

            return await send_message_resilient(
                bot=bot,
                chat_id=chat_id,
                text=text,
                reply_to_message_id=getattr(message, "message_id", None),
                message_thread_id=getattr(message, "message_thread_id", None),
                chat_type=getattr(chat, "type", None),
            )

    async def _error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle errors globally."""
        error = context.error
        update_obj = update if isinstance(update, Update) else None
        logger.error(
            "Global error handler triggered",
            error=str(error),
            update_type=type(update_obj).__name__ if update_obj else None,
            user_id=(
                update_obj.effective_user.id
                if update_obj and update_obj.effective_user
                else None
            ),
        )

        # Determine error message for user
        from ..exceptions import (
            AuthenticationError,
            ConfigurationError,
            RateLimitExceeded,
            SecurityError,
        )

        error_messages: list[tuple[type[BaseException], str]] = [
            (
                AuthenticationError,
                "🔒 Authentication required. Please contact the administrator.",
            ),
            (
                SecurityError,
                "🛡️ Security violation detected. This incident has been logged.",
            ),
            (
                RateLimitExceeded,
                "⏱️ Rate limit exceeded. Please wait before sending more messages.",
            ),
            (
                ConfigurationError,
                "⚙️ Configuration error. Please contact the administrator.",
            ),
            (
                asyncio.TimeoutError,
                "⏰ Operation timed out. Please try again with a simpler request.",
            ),
            (
                Conflict,
                "⚠️ 检测到同一 Bot Token 存在并发实例，请仅保留一个运行中的实例后重试。",
            ),
        ]

        error_type: type[Exception]
        if isinstance(error, Exception):
            error_type = type(error)
        else:
            error_type = Exception
        user_message = "❌ An unexpected error occurred. Please try again."
        if isinstance(error, BaseException):
            for match_type, message in error_messages:
                if isinstance(error, match_type):
                    user_message = message
                    break

        # Try to notify user
        if update_obj and update_obj.effective_message:
            try:
                await self._reply_update_message_resilient(
                    update_obj, context, user_message
                )
            except Exception:
                logger.exception("Failed to send error message to user")

        # Log to audit system if available
        from ..security.audit import AuditLogger

        audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
        if audit_logger and update_obj and update_obj.effective_user:
            try:
                await audit_logger.log_security_violation(
                    user_id=update_obj.effective_user.id,
                    violation_type="system_error",
                    details=f"Error type: {error_type.__name__}, Message: {str(error)}",
                    severity="medium",
                )
            except Exception:
                logger.exception("Failed to log error to audit system")

    async def get_bot_info(self) -> Dict[str, Any]:
        """Get bot information."""
        if not self.app:
            return {"status": "not_initialized"}

        try:
            me = await self.app.bot.get_me()
            return {
                "status": "running" if self.is_running else "initialized",
                "username": me.username,
                "first_name": me.first_name,
                "id": me.id,
                "can_join_groups": me.can_join_groups,
                "can_read_all_group_messages": me.can_read_all_group_messages,
                "supports_inline_queries": me.supports_inline_queries,
                "webhook_url": self.settings.webhook_url,
                "webhook_port": (
                    self.settings.webhook_port if self.settings.webhook_url else None
                ),
            }
        except Exception as e:
            logger.error("Failed to get bot info", error=str(e))
            return {"status": "error", "error": str(e)}

    async def health_check(self) -> bool:
        """Perform health check."""
        try:
            if not self.app:
                return False

            # Try to get bot info
            await self.app.bot.get_me()
            return True
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return False
