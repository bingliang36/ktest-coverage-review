# 错误处理

处理脚本错误时，先读取 stdout 最后一行 JSON，再按 `error` 判断。

| error | 含义 | 建议操作 |
|---|---|---|
| MISSING_ARGUMENT | 缺少 cid、feature URL 或 GitLab URL | 向用户补充询问必要参数 |
| SSO_AUTH_FAILED | 内网接口认证失败 | 重新运行；如仍失败，提示用户确认内网权限 |
| API_ERROR | KTest 或 KDev 接口返回非成功状态 | 报告接口路径、HTTP 状态和业务返回，不要猜测 |
| GIT_AUTH_FAILED | GitLab 或 GitHub 拉取代码失败 | 要求用户提供可用只读凭据或改为只生成数据报告 |
| INVALID_URL | 链接无法解析 | 要求用户重新提供标准 URL |
| MISSING_TASK_ID | KDev feature 链接/详情/关联任务列表未提供 Artemis 内部 Long taskId | 向用户询问 taskId 或要求提供带 taskId 的链接；若只发现 `T123...` Team 任务号,不得代入；禁止使用历史示例值 |
| AMBIGUOUS_TASK_ID | feature 详情出现多个 taskId 候选 | 要求用户明确指定 `--task-id` |
| KDEV_DATA_MISMATCH | KDev 返回分支与期望分支不一致 | 报告实际分支和期望分支，要求确认 taskId/链接，不得落盘分析 |
| OUTPUT_ERROR | 产物写入失败 | 检查 `tmp/` 目录权限和磁盘空间 |
| REMOTE_AGENT_REQUIRED | 需要通过远程 Agent 执行长耗时脚本 | 用 `call_remote_agent(method=CALLBACK)` 重试同一命令 |
| UNEXPECTED_ERROR | 未分类异常 | 保留 stderr 诊断和 stdout JSON，交给用户确认是否继续排查 |

脚本退出码:

- `0`: 成功或可继续的引导结果。
- `1`: 失败。
- `2`: 需要用户补充信息或权限。
- `130`: 用户中断。
