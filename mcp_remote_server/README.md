# MCP Remote SSH Local Server

基于 FastMCP + Paramiko 的 Windows 原生 local MCP server，特性：

- 启动时创建全局 `PersistentSSH`，后续 tool 复用同一个 SSH Transport 长连接。
- 每条命令只新建 channel，结束即关闭 channel，不重建 SSH 连接。
- 本地 timeout 后返回结构化结果 + partial 输出，不自动重试。
- 内置危险命令 denylist + 本地 audit 日志。

## MCP Tools

- `ssh_exec(cmd, timeout=30)`：执行短命令。
- `ssh_exec_script(script, timeout=300)`：执行单次多行脚本（`bash -lc`）。
- `review_prepare(repo_path, target, review_tips="")`：按 workflow 生成 `.review` 审查上下文文件。
- `prepare_review_context(...)`：`review_prepare` 的兼容别名。
- `review_write_result(repo_path, review_result)`：保存 Agent 最终审查结果到 `.review/review_result.md`。

## Review Workflow (由 MCP tools 约束)

`review_prepare` 会自动生成：

```text
.review/
├── review_target.md
├── structure.md
├── review_related.md
├── review_tips.md
└── review_prompt.md
```

然后 Agent 读取以上文件进行审查，最后通过 `review_write_result` 写入：

```text
.review/review_result.md
```

## target 支持

- 函数名：`udk_parse_cmd`
- 文件路径：`drivers/foo/bar.c`
- 当前 diff：`current diff`
- staged diff：`staged diff`
- commit：`commit abc123`
- patch 文件：`patch /tmp/xxx.patch`

## 环境变量

- `SSH_HOST` (required)
- `SSH_PORT` (default `22`)
- `SSH_USERNAME`
- `SSH_KEY_FILENAME`
- `SSH_PASSWORD` (optional)
- `SSH_PASSPHRASE` (optional)
- `SSH_CONNECT_TIMEOUT` (default `10`)
- `SSH_AUTH_TIMEOUT` (default `15`)
- `SSH_BANNER_TIMEOUT` (default `15`)
- `SSH_KEEPALIVE_INTERVAL` (default `30`)
- `MCP_STDIO` (default `1`)
- `MCP_HOST` (http mode default `127.0.0.1`)
- `MCP_PORT` (http mode default `8000`)

## 运行

```bash
python -m mcp_remote_server.server
```

## timeout 返回示例

```json
{
  "ok": false,
  "status": "timeout",
  "timeout": true,
  "timeout_seconds": 300,
  "cmd": "...",
  "stdout_partial": "...",
  "stderr_partial": "...",
  "next_actions": [
    "retry_with_longer_timeout",
    "ask_user_for_more_info",
    "stop"
  ],
  "message_for_user": "The remote command timed out before completion. Do you want me to rerun it with a longer timeout, provide more information before retrying, or stop?"
}
```
