# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Telegram Bot，通过 Long Polling 提供对 CLI 编码智能体（Claude / Codex）的远程访问。Python 3.10+，Poetry 构建，`python-telegram-bot` 处理 Telegram 交互，`claude-agent-sdk` 处理 Claude Code 集成。

## 命令

```bash
make dev              # 安装所有依赖（含开发依赖）
make install          # 仅安装生产依赖
make run              # 运行 bot（poetry run claude-telegram-bot）
make run-debug        # 以调试日志模式运行
make test             # 运行测试并生成覆盖率
make lint             # Black + isort + flake8 + mypy
make format           # 使用 black + isort 自动格式化

# 运行单个测试文件
poetry run pytest tests/unit/test_config.py -v

# 运行匹配名称的测试
poetry run pytest tests/unit/test_config.py -k test_name -v

# 仅类型检查
poetry run mypy src
```

## 手动重启服务（macOS）
"重启后有残留进程"的含义：新进程已经拉起，但旧的 bot 进程没有被正确回收，导致可能出现并发轮询、消息无响应或日志判断混乱。

推荐使用 `tmux` 的标准重启流程（避免残留），**所有步骤必须连续执行，不得中断**：

```bash
# 步骤 1: 停掉旧会话（忽略不存在的错误）
tmux kill-session -t cli_tg_bot 2>/dev/null || true

# 步骤 2: 等待进程完全退出，确保无残留
sleep 2

# 步骤 3: 二次确认 tmux 会话已销毁（防止竞争条件）
tmux kill-session -t cli_tg_bot 2>/dev/null || true

# 步骤 4: 新建独立会话并启动
tmux new-session -d -s cli_tg_bot -c /Users/suqi3/PycharmProjects/cli-tg './scripts/restart-bot.sh'
```

启动后验证：
```bash
# 验证进程唯一性：确认只有一个 bot 主进程
ps -Ao pid,ppid,command | rg -i 'cli-tg-bot|claude-telegram-bot|src.main' | rg -v 'rg -i'

# 验证运行日志：确认持续出现 Telegram getUpdates 200 OK 且无异常栈
tmux capture-pane -t cli_tg_bot -p | tail -n 80
```

重要约束：
- **步骤 1-4 必须在同一次操作中连续执行**，不要在中间停下来等待用户确认。可以用 `&&` 串联或在一个 bash 调用中完成。
- 执行约束：修改 cli-tg 代码后**不要主动重启服务**，必须等用户明确说"重启"才执行重启流程。
- `./scripts/restart-bot.sh` 会执行 `pkill` 停止旧进程后再 `poetry run claude-telegram-bot`，对 Claude CLI 与 Codex CLI 都生效。
- 无响应排查顺序：先确认服务在线（`tmux` 会话、唯一进程、`getUpdates 200 OK`），再检查命令格式与路由。
- 用户名一致性：`/engine@<bot_username>` 里的用户名必须与 Telegram `getMe` 返回一致；当前应为 `CodingSam_bot`，并保持 `.env` 的 `TELEGRAM_BOT_USERNAME` 同步。

## 架构

### 多引擎集成（Claude + Codex）

系统支持多个 CLI 引擎，通过 `src/bot/utils/cli_engine.py` 统一抽象：

- **引擎选择**：`get_cli_integration(bot_data, scope_state)` 根据当前 scope 的 `active_cli_engine` 状态解析对应的集成实例
- **引擎能力声明**：`EngineCapabilities` 数据类描述各引擎支持的功能（模型选择、诊断命令等）
- **命令可见性**：`COMMAND_ENGINE_VISIBILITY` 控制哪些命令在哪个引擎下显示

每个引擎的集成实例通过 `context.bot_data["cli_integrations"]` 字典访问（键为引擎名），向后兼容 `context.bot_data["claude_integration"]`。

### Claude 双后端（SDK 优先，CLI 回退）

`ClaudeIntegration`（门面层，`src/claude/facade.py`）封装两个后端：
- **`ClaudeSDKManager`**（`src/claude/sdk_integration.py`）— 主要方式。使用 `claude-agent-sdk` 异步 `query()` 和流式传输。会话 ID 来自 Claude 的 `ResultMessage`，而非本地生成。
- **`ClaudeProcessManager`**（`src/claude/integration.py`）— CLI 子进程回退方式。

回退策略：
- **可重试错误**（触发 SDK→CLI 回退）：`ClaudeTimeoutError`、`CLIConnectionError`、`CLIJSONDecodeError`、`ClaudeParsingError`、TaskGroup/ExceptionGroup
- **不可重试错误**（直接失败）：ValueError、权限拒绝、验证失败
- **不回退的场景**：权限审批中的请求（仅 SDK 支持）、图片处理请求（CLI 不支持）

### 请求流程

```
Telegram 消息 → 安全中间件 (group -3) → 认证中间件 (group -2)
→ 限流 (group -1) → 命令/消息处理器 (group 10)
→ CLI 引擎解析 → ClaudeIntegration.run_command() → SDK（带 CLI 回退）
→ 响应解析 → Storage.save_claude_interaction() → 返回 Telegram
```

### Per-Topic 作用域状态

`src/bot/utils/scope_state.py` 实现按 `user_id:chat_id:thread_id` 维度的独立状态隔离，支持群组论坛的多 topic 并行会话：

- 状态存储在 `context.user_data["scope_state"][scope_key]`
- **继承白名单**：新 scope 仅继承 `current_directory` 和 `claude_model`，会话标识（session_id、force_new_session）不继承
- 新 scope 自动设置 `force_new_session = True`，防止拉取父 topic 的会话
- 处理器通过 `get_scope_state_from_update()` / `get_scope_state_from_query()` 获取当前 scope

### 服务层

`src/services/` 提取了处理器的业务逻辑，避免 handler 臃肿：

- **`SessionInteractionService`**（815 行）— 核心交互逻辑：上下文渲染、状态/用量展示、会话信息查询。定义 `SessionInteractionMessage`、`ContextViewSpec`、`ContextRenderResult` 数据类
- **`SessionLifecycleService`** — 会话创建、终止、生命周期事件
- **`SessionService`** — 会话操作（切换目录、模型设置）
- **`ApprovalService`** — 工具权限审批工作流
- **`EventService`** — 事件追踪

### 依赖注入

`ClaudeCodeBot._inject_deps()` 将所有依赖注入 `context.bot_data`：

```python
# 核心依赖
context.bot_data["cli_integrations"]     # dict[str, Integration] 多引擎
context.bot_data["claude_integration"]   # 向后兼容的 Claude 集成
context.bot_data["storage"]              # Storage 门面
context.bot_data["features"]             # FeatureFlags
context.bot_data["task_registry"]        # 任务注册（用于取消）

# 安全
context.bot_data["auth_manager"]         # AuthenticationManager
context.bot_data["security_validator"]   # SecurityValidator
context.bot_data["rate_limiter"]         # RateLimiter
context.bot_data["audit_logger"]         # AuditLogger

# 服务
context.bot_data["permission_manager"]   # PermissionManager
context.bot_data["approval_service"]     # ApprovalService
context.bot_data["session_lifecycle_service"]
context.bot_data["session_interaction_service"]
context.bot_data["event_service"]
context.bot_data["session_service"]
```

### 存储层

`src/storage/` 使用 Repository 模式 + aiosqlite：

- **`Storage`**（`storage/facade.py`）— 高级门面，`save_claude_interaction()` 原子性保存消息、工具用量、费用、用户/会话统计、事件、审计日志
- **Repositories**（`storage/repositories.py`）— 每个实体一个 Repository：User、Session、Message、ToolUsage、SessionEvent、AuditLog、ApprovalRequest、CostTracking、Analytics
- **`SQLiteSessionStorage`**（`storage/session_storage.py`）— Claude 会话持久化，支持自动恢复

### 会话管理

`SessionManager`（`src/claude/session.py`）管理会话生命周期：
- 临时 ID 格式 `temp_*`，首次响应后替换为 Claude 分配的真实 ID
- 自动恢复：按 user_id + project_path 匹配，排除临时 ID，检查超时
- 桌面会话采纳：通过 `adopt_external_session()` 标记 `source="desktop_adopted"`

### 工具权限系统

`PermissionManager`（`src/claude/permissions.py`）桥接 SDK `can_use_tool` 回调与 Telegram 审批按钮：
- `request_permission()` 创建 `asyncio.Future`，用户通过内联按钮审批
- `claude_allowed_tools` 配置项自动放行白名单工具
- 默认超时 120 秒

### 安全模型

5 层纵深防御：认证（白名单/令牌，group -2）→ 输入验证（阻止 `..`、`;`、`&&`、`$()` 等，group -3）→ 目录隔离（`APPROVED_DIRECTORY` + 路径遍历防护）→ 限流（令牌桶，group -1）→ 审计日志。

### 配置

Pydantic Settings v2 从环境变量加载（`src/config/settings.py`）。必需项：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_BOT_USERNAME`、`APPROVED_DIRECTORY`。

重要可选项：`ALLOWED_USERS`（逗号分隔 Telegram ID）、`USE_SDK`（默认 true）、`ENABLE_CODEX_CLI`、`CODEX_CLI_PATH`、`ANTHROPIC_API_KEY`、`ENABLE_MCP`、`MCP_CONFIG_PATH`。

功能开关位于 `src/config/features.py`（`FeatureFlags` 类），通过 `is_feature_enabled(name)` 查询。

### 应用生命周期

入口 `src/main.py`：
1. 解析参数（`--debug`、`--config-file`）→ 加载配置 → 初始化 structlog（带敏感信息脱敏）
2. 初始化存储 → 创建安全组件 → 创建 Claude 组件和服务 → 创建 Bot
3. 可选创建 Codex CLI 适配器（独立配置，共享存储）
4. Bot 启动 Polling 或 Webhook → 运行直到收到 SIGINT/SIGTERM
5. 优雅关闭：停止 updater → 终止活跃进程 → 清理过期会话 → 关闭数据库

## 代码风格

- Black（88 字符行宽）、isort（black profile）、flake8、mypy 严格模式（`disallow_untyped_defs = true`）
- pytest-asyncio，`asyncio_mode = "auto"`，测试中用 `AsyncMock` 模拟异步方法
- structlog 结构化日志（生产 JSON，开发控制台），`SensitiveLogFilter` 脱敏 bot token

## Git 提交与合并约定（强制）

- Commit 标题保持 Conventional Commits 风格；**commit 备注正文必须使用中文**，明确记录动机、改动范围、验证结论。
- 合并到主干时必须保留分支轨迹：使用 `git merge --no-ff <branch>`（或等效 Merge PR），不要 fast-forward 抹平历史。
- 当前主干分支是 `master`（若后续迁移到 `main`，同样遵循以上规则）。

## 提交前隐私安全检查（强制）

- 提交前必须检查本次变更是否包含敏感信息：`token`、`secret`、`password`、`api key`、`cookie`、`authorization`、私钥、个人隐私数据。
- 推荐先检查暂存区差异：`git diff --cached | rg -i 'token|secret|password|apikey|api_key|cookie|authorization|private key'`。
- 一旦发现敏感信息，先完成清理/脱敏再提交；禁止将敏感内容写入 Git 历史后再修补。

## 添加新 Bot 命令

1. 在 `src/bot/handlers/command.py` 中添加处理函数
2. 在 `src/bot/core.py` 的 `_register_handlers()` 中注册
3. 添加到 `_set_bot_commands()` 以显示在 Telegram 命令菜单中
4. 如命令仅对特定引擎可见，在 `cli_engine.py` 的 `COMMAND_ENGINE_VISIBILITY` 中配置
5. 为该命令添加审计日志
