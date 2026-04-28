# SSH 执行与 Review Tool 机制分析

## 1) `ssh_exec` 实现原理与特性

### 调用路径
- MCP 暴露层：`server.py` 中 `ssh_exec(cmd, timeout=30)`，直接调用 `PersistentSSH.run_command`。
- 执行层：`ssh_client.py` 中 `run_command`。
- 底层协议：Paramiko `SSHClient` + `Transport` + `Channel`。

### 执行机制
1. 服务启动时创建全局 `PersistentSSH` 实例并 `connect()`，形成持久 SSH 连接。
2. 每次 `ssh_exec` 调用仅新建 `channel`（`transport.open_session()`），执行单条命令（`chan.exec_command(cmd)`）。
3. 通过轮询读取 `stdout/stderr`（20ms sleep），直到退出或超时。
4. 完成后关闭 channel，但保留 transport，供后续复用。

### 结果结构
- 成功：`ok=true`, `status="ok"`, `returncode=0`, `stdout/stderr`。
- 命令失败：`ok=false`, `status="failed"`, `returncode!=0`。
- 超时：`status="timeout"`，返回 `stdout_partial/stderr_partial` + `next_actions` + `message_for_user`。
- 拒绝：命中 denylist 返回 `status="denied"`。
- 异常：返回 `status="error"` 并关闭连接，等待下次重连。

### 核心特性
- 长连接复用：降低反复握手开销。
- 命令级隔离：每次只复用 transport，不复用 channel，状态污染小。
- 安全门：denylist + 禁用 sudo。
- 可观测性：全量审计日志 `mcp_ssh_audit.log`，记录状态、输出长度、超时等。
- 并发控制：`threading.Lock` 将每次执行串行化，避免同一 transport 并发读写冲突。

## 2) 当前稳定性评估

### 优势（稳定性正向）
- **连接自愈**：`ensure_connected` 发现 transport 失活时自动重连。
- **异常兜底**：执行异常会 `close()`，避免脏连接持续复用。
- **超时可恢复**：不会无限阻塞，超时后返回 partial，便于上层重试策略。
- **输出处理稳健**：decode 使用 `errors="replace"`，可避免非 UTF-8 字节导致崩溃。

### 风险点（稳定性负向）
- **单锁串行导致吞吐受限**：所有命令竞争同一把锁，慢命令会阻塞后续请求。
- **超时判定依赖本地轮询**：不是远端软中断；超时时只是关闭 channel，远端进程是否完全结束依赖 SSH 会话行为。
- **denylist 为模式匹配**：可维护性较好但不是严格沙箱，复杂命令绕过风险理论存在。
- **启动即连接**：`server.py` import 时执行 `connect()`；若网络暂时不可达，服务启动可失败（可用性风险）。
- **审计日志增长**：未见日志轮转机制，长期运行可能带来磁盘与检索成本。

### 综合结论
- 面向“单 Agent、低并发、可重试”的远程执行场景，当前实现**中高稳定性**。
- 面向“高并发、多租户、强隔离安全”场景，建议增加：连接池/队列、日志轮转、命令白名单/沙箱、超时后远端进程回收策略。

## 3) Review Tool 的 input / output / workflow 属性

### 3.1 `review_prepare` 输入
- `repo_path: str`
- `target: str`（支持 function/file/current diff/staged diff/commit/patch）
- `review_tips: str = ""`

### 3.2 `review_prepare` 输出
返回结构化 dict，关键字段：
- `ok/status`
- `repo_path/target/target_type`
- `files`（`.review` 下 5 个 markdown 文件路径）
- `summary`（changed_files/functions/structs/macros/globals）
- `workflow`（推荐阶段步骤）
- `subagent_recommendations`（编排模板、角色、loop 提示）

### 3.3 它是否是一个 workflow？
- **是**。`review_prepare` 本身是“工作流准备器”：
  1) 识别 target 类型
  2) 收集目标内容
  3) 提取符号与文件
  4) 生成结构化上下文文档
  5) 返回后续审查流程建议
- 随后需由 Agent 继续执行“读取上下文 -> 形成审查 -> `review_write_result` 落盘”。

### 3.4 一次调用接口中，是否会“多次给 agent 输出结构化对话”？
- **对 MCP 调用响应语义：不会**。单次 tool 调用只返回**一个** JSON 结果对象。
- **对内部执行语义：会有多阶段处理与循环**（如逐函数抽取），但这些中间阶段不会以“多次 tool 输出”流式回传给 Agent，而是聚合成最终一次返回。
- 若需要“多次结构化交互”，必须由 Agent 进行多次工具调用（例如先 `review_prepare`，再逐步 `ssh_exec`，最后 `review_write_result`）。
