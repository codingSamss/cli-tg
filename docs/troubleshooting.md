# 故障排查

本文聚焦两类高频问题：

1. Telegram 侧看起来“很久没回复”
2. 用户体感变慢，但不确定是队列、Codex/Claude 子进程、本地工具执行，还是 Telegram 发送阶段导致

## 快速结论

遇到明显卡顿时，不要先看 CLI 里显示的 `Context Usage`。它只能作为辅助参考，不能单独解释十几分钟到几十分钟的停顿。

优先按这个顺序定位：

1. 先看有没有排队：`queue_wait_ms`
2. 再看 Codex/Claude 子进程是否长时间静默：`silent_after_last_stdout_ms`
3. 再看本地工具是否真的跑了很久：`command_progress_count`、`first_tool_activity_ms`
4. 最后看 Telegram 发消息是否慢：`tg_progress_timeout_count`、`final_reply_total_ms`

## 最短排查命令

```bash
tmux capture-pane -J -t cli_tg_bot -p -S -800 | rg "Queued inbound text task|Dispatching queued task|Text message timing diagnostics|Claude subprocess output diagnostics"
```

如果要分阶段看，可以直接用下面几条。

## 1. 先看队列

```bash
tmux capture-pane -J -t cli_tg_bot -p -S -800 | rg "Queued inbound text task|Dispatching queued task|queue_wait_ms"
```

关注：

- `Queued inbound text task`：请求已经进入队列
- `Dispatching queued task`：请求开始真正执行
- `queue_wait_ms`：从入队到开始执行的等待时间

判断：

- `queue_wait_ms` 很大：前面已有任务占着，主因是队列堵塞
- 长时间只看到 `Queued inbound text task`，迟迟没有 `Dispatching queued task`：先排查是否已有长任务未结束

## 2. 看 Codex/Claude 子进程有没有“静默卡住”

```bash
tmux capture-pane -J -t cli_tg_bot -p -S -800 | rg "Claude subprocess output diagnostics|process_wall_ms|silent_after_last_stdout_ms|post_result_wait_ms"
```

日志名：`Claude subprocess output diagnostics`

核心字段：

- `process_wall_ms`：整个 CLI 子进程总耗时
- `first_stdout_event_ms`：第一次收到 stdout 事件的时间
- `last_stdout_event_ms`：最后一次收到 stdout 事件的时间
- `result_received_ms`：收到最终结果事件的时间
- `silent_after_last_stdout_ms`：最后一次 stdout 事件之后，又静默等了多久
- `post_result_wait_ms`：结果已经出来后，进程退出前又等了多久
- `command_progress_count`：命令执行类事件数量
- `tool_result_count`：工具结果事件数量

判断：

- `process_wall_ms` 很大，同时 `silent_after_last_stdout_ms` 也很大：更像上游模型侧静默停滞，不是本地工具慢
- `post_result_wait_ms` 很大：结果已经出来了，但 CLI 进程收尾慢
- `command_progress_count` 很低，且长时间没有新 stdout：不要优先怀疑 Telegram

## 3. 看本地工具或命令是否真的跑了很久

```bash
tmux capture-pane -J -t cli_tg_bot -p -S -800 | rg "Text message timing diagnostics|command_progress_count|first_tool_activity_ms|command_wall_ms"
```

日志名：`Text message timing diagnostics`

核心字段：

- `command_wall_ms`：整段引擎执行耗时
- `first_stream_update_ms`：第一次收到流式更新的时间
- `first_assistant_text_ms`：第一次收到纯文本答复片段的时间
- `first_tool_activity_ms`：第一次看到工具活动的时间
- `command_progress_count`：命令执行类进度数量
- `tool_activity_count`：工具活动数量

判断：

- `first_tool_activity_ms` 很快，`command_progress_count` 也持续增长：说明本地工具阶段确实在跑
- `first_tool_activity_ms` 很晚，或者始终没有工具活动：更像模型前面在思考、等待，或上游链路停滞
- `command_wall_ms` 很大，但子进程日志里 `silent_after_last_stdout_ms` 更大：优先归因到上游静默，而不是本地命令

## 4. 最后看 Telegram 发送阶段

```bash
tmux capture-pane -J -t cli_tg_bot -p -S -800 | rg "Text message timing diagnostics|tg_progress_timeout_count|tg_progress_edit_total_ms|final_reply_total_ms"
```

关注：

- `tg_progress_timeout_count`：进度消息编辑超时次数
- `tg_progress_edit_total_ms`：进度消息编辑总耗时
- `tg_progress_refresh_total_ms`：编辑失败后用新消息刷新进度的总耗时
- `final_reply_total_ms`：最终回复发送总耗时

判断：

- `tg_progress_timeout_count` 高：Telegram 进度消息更新存在超时
- `final_reply_total_ms` 很大：最终回复发出阶段慢
- 这些字段都不高：Telegram 不是主因

## 常见归因模式

### 模式 A：队列堵塞

特征：

- `queue_wait_ms` 明显偏大
- 之后真正执行的各阶段反而正常

结论：

前面已有任务占用执行窗口，不是 Telegram 或上游模型本身变慢。

### 模式 B：上游模型或链路静默停滞

特征：

- `process_wall_ms` 很大
- `silent_after_last_stdout_ms` 很大
- `command_progress_count` 不高，甚至很早就不再增长

结论：

更像 Codex/OpenAI 或 Claude 上游在长时间无新事件输出。此时即使 `Context Usage` 看起来不高，也可能出现十几分钟以上停顿。

### 模式 C：本地工具真的跑很久

特征：

- `first_tool_activity_ms` 很快
- `command_progress_count`、`tool_activity_count` 持续增长
- 子进程 stdout 也一直有新事件

结论：

耗时主要发生在本地命令、文件扫描、测试或其他工具执行阶段。

### 模式 D：Telegram 发送抖动

特征：

- `tg_progress_timeout_count` 增长
- `final_reply_total_ms` 偏大
- 但子进程阶段整体正常

结论：

引擎侧已经完成，慢在 Telegram API 交互阶段。

## 代码落点

相关诊断日志由以下代码产出：

- `src/bot/handlers/message.py`
  - `Queued inbound text task`
  - `Dispatching queued task`
  - `Text message timing diagnostics`
- `src/claude/integration.py`
  - `Claude subprocess output diagnostics`

如果要新增字段或调整归因逻辑，优先从这两个文件入手。

## 经验总结

1. `Context Usage` 只能辅助判断上下文负担，不能直接解释长时间静默。
2. 先看 `queue_wait_ms`，再看 `silent_after_last_stdout_ms`，最后才看 Telegram 发送时间。
3. 只看用户侧“很久没回”这个现象，容易把队列堵塞、上游静默和 TG 超时混为一谈；必须结合结构化日志拆阶段判断。
