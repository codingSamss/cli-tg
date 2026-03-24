# Repository Guidelines

## 项目结构与模块组织
核心代码位于 `src/`，按职责分层：`config/`（配置加载与环境覆盖）、`security/`（鉴权与限流）、`bot/`（Telegram 交互与处理器）、`claude/`（Claude 集成）、`storage/`（SQLite 与仓储模式）、`utils/`（常量与通用工具）。入口为 `src/main.py`。  
测试代码位于 `tests/`，当前以 `tests/unit/` 为主，目录结构尽量镜像 `src/`。文档在 `docs/`，运维与部署相关说明见 `README.md`、`SECURITY.md`、`SYSTEMD_SETUP.md`。

## 构建、测试与开发命令
 - `make dev`：安装开发依赖（Poetry）并尝试安装提交钩子。
 - `make install`：仅安装生产依赖。
 - `make run`：启动机器人。
 - `make run-debug`：调试模式启动，输出更详细日志。
 - `make test`：运行 `pytest` 与覆盖率统计。
 - `make lint`：执行 `black --check`、`isort --check-only`、`flake8`、`mypy`。
 - `make format`：自动格式化 `src` 与 `tests`。

## 重启服务
本仓库在 macOS 上通常没有 systemd，推荐使用项目内脚本重启：
1. 残留进程定义：新进程已启动，但旧 bot 进程未退出，会导致并发轮询或响应异常。
2. 标准做法（推荐 `tmux`）：先 `tmux kill-session -t cli_tg_bot`，再 `tmux new-session -d -s cli_tg_bot -c /Users/suqi3/PycharmProjects/cli-tg './scripts/restart-bot.sh'`。
3. 验证仅有一个 bot 进程：`ps -Ao pid,ppid,command | rg -i 'cli-tg-bot|claude-telegram-bot|src.main' | rg -v 'rg -i'`。
4. 验证轮询正常：`tmux capture-pane -t cli_tg_bot -p | tail -n 80`，应持续看到 `getUpdates 200 OK`，且无异常栈。
5. 执行约束：默认不自动重启。只有用户明确要求“重启”时，才按上述流程执行。
6. 用户侧无响应排查顺序：先确认服务在线（`tmux` 会话、唯一进程、`getUpdates 200 OK`），再检查命令格式与路由。
7. 用户名一致性：`/engine@<bot_username>` 里的用户名必须与 Telegram `getMe` 返回一致；当前应为 `CodingSam_bot`，并保持 `.env` 的 `TELEGRAM_BOT_USERNAME` 同步。
8. `./scripts/restart-bot.sh` 内部会 `pkill -f cli-tg` 后执行 `poetry run claude-telegram-bot`，对 Claude CLI 与 Codex CLI 都生效。

## 代码风格与命名约定
使用 Python 3.10+，统一 4 空格缩进，行宽 88（Black 规则）。导入顺序由 isort（与 Black 兼容配置）管理。  
命名规范：模块/函数使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。  
类型标注为强制要求（mypy 开启 `disallow_untyped_defs`），新增或修改接口时必须补齐类型。优先复用 `src/exceptions.py` 中的异常层级与结构化日志模式。

## 测试指南
测试框架为 `pytest` + `pytest-asyncio` + `pytest-cov`。测试文件命名使用 `test_*.py`，测试函数使用 `test_*`。异步用例显式添加 `@pytest.mark.asyncio`。  
执行 `make test` 后查看终端缺失覆盖率报告与 `htmlcov/`。项目当前总体覆盖率约 85%，新增变更应避免拉低覆盖率（建议保持在 80% 以上）。

## 提交与合并请求规范
提交信息遵循约定式提交（Conventional Commits），历史中常见前缀：`feat:`、`fix:`、`refactor:`、`docs:`、`test:`、`chore:`。示例：`feat: add session export command`。  
发起合并请求（Pull Request）时请包含：变更目的、关联 Issue、测试结果（至少 `make test` 与 `make lint`）、必要文档更新。若变更影响 Telegram 交互流程，附上关键聊天截图或日志片段。

### 提交与主干合并约定（强制）
- Commit 标题可遵循 Conventional Commits 英文前缀，但 **commit 备注正文必须使用中文**，至少说明变更动机、主要改动点与验证结果。
- 合并到主干时，必须保留可追溯的分支合并记录：使用 `git merge --no-ff <branch>`（或等效 Merge PR），禁止使用 fast-forward 直接“抹平”分支历史。
- 当前仓库主干分支为 `master`；若后续切换为 `main`，沿用同样规则。
- 提交并推送到 GitHub 时，必须同步打 `tag`（建议使用语义化版本，如 `vX.Y.Z`），并推送 tag（例如 `git push origin <branch> --follow-tags`）。
- `tag` 必须使用注解标签（annotated tag），注解内容需用中文分点概括改动（示例：`- 新增：...`、`- 修复：...`、`- 重构/测试/文档：...`）。

## 安全与配置提示
从 `.env.example` 复制生成 `.env`，严禁提交令牌、密钥或真实凭据。重点检查 `ALLOWED_USERS` 与 `APPROVED_DIRECTORY`，避免越权访问。涉及 Claude 命令执行路径时，优先使用本地 `claude-wrapper.sh` 并在提交前确认未泄露敏感配置。

### 提交前隐私安全检查（必须执行）
- 提交任何代码前，必须检查本次改动中是否包含敏感信息（token、key、cookie、密码、个人隐私数据、内部链接与账号标识）。
- 至少执行一次基于 diff 的敏感词扫描（例如：`git diff --cached | rg -i 'token|secret|password|apikey|api_key|cookie|authorization|private key'`）。
- 若发现疑似敏感信息，必须先清理或脱敏，再提交；禁止以“后续再改”方式带入仓库历史。
