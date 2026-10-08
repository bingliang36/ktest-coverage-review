# kdev_fetch —— kdev feature 一键取数(代码 + 流水线日志)

输入 kdev featureDetail 链接,一键拉取:
1. feature 元信息 + 分支/commit/repo
2. **准出流水线所有阶段(job)的全量日志 + 错误日志**
3. 关联 GitLab 代码(clone)

## 快速开始

```bash
cd <workspace>
export COVFETCH_WS=$PWD

# 完整拉取(feature 270240): 元信息 + 15 个 job 日志 + 代码
uv run tools/covfetch/kdev_fetch.py "https://kdev.corp.kuaishou.com/web/workbench/featureDetail?bizId=84&id=270240&stage=test"

# 只拉日志不拉代码
uv run tools/covfetch/kdev_fetch.py "URL" --no-code

# 只拉某个 job(调试时省时间)
uv run tools/covfetch/kdev_fetch.py "URL" --no-code --only-job 150857502
```

输出: `data/kdev/<featureId>/`:
```
feature.json            feature 元信息 + 分支 + pipeline 概览 + logs 清单
jobs.json               15 个 job(jobLogId / name / status / stage)
logs/<jobLogId>_<name>.log         全量日志(下载接口原始完整)
logs/<jobLogId>_<name>.error.log   错误日志(各阶段单独的错误流)
```

## 接口清单(全部实测,只读)

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/artemis/task/pass/pipeline?sourceId={featureId}&relationType=3&testType=1&taskId=500685` | **唯一入口**:分支(branch/commitId/repoId) + pipelineList + pipelineLog.jobLogList |
| GET | `/api/kdev/pipeline/pipelineJobLog?id={jobLogId}` | job 元信息(name/stage/status/halo/onCall) |
| GET | `/api/kdev/pipeline/pipelineJobLog/log?id={jobLogId}&start=0` | 增量日志流 `{content, offset, hasMore}` |
| GET | `/api/kdev/pipeline/pipelineJobLog/errorLog?id={jobLogId}&start=0` | 错误日志流(仅错误) |
| GET | `/api/kdev/pipeline/job/log/download?id={jobLogId}` | **全量日志下载**(最完整,推荐) |

kdev 返回统一包 `{status:200, message, data}`。

### 树形结构(从 feature 到日志)

```
featureDetail?id=270240
  └─ artemis/task/pass/pipeline  → data.list[0]
       ├─ branch / commitId / repoId / repoUrl
       └─ pipelineList[i].pipelineLog.jobLogList
            └─ 15 个 job {id, name, statusDesc, stage}
                 └─ pipelineJobLog + log + errorLog + download
```

### jobLogList 的 id 坑

接口返回的 `id` 可能是字符串或数字,**必须 `int(float(id))` 归一**,否则拼 URL 会带 `.0` 尾巴导致 400(实测踩过)。

## 认证

- **kdev**: 必须走浏览器 SSO 会话(agent-browser + OboToken),页面内 `fetch(credentials:'include')`。
  curl 直接打 kdev 接口会 401/跳 SSO —— 会话绑定在浏览器那次请求,OAuth state 跨进程重放必失败。
- **GitLab**: PAT(`.secrets/gitlab_token` 或 `GITLAB_TOKEN`),credential helper + env 姿势,见 `gitlab_api.py`。

## 已踩过的坑(别重走)

1. **curl 打 kdev 接口** → 401 跳 SSO。必须浏览器会话,零例外。
2. **browser blob 下载** → 无头环境静默失败。日志用 fetch 拿文本再 base64 导出(`fetch_b64`)。
3. **JSON 分块拼日志** → UTF-16 代理对截断导致长度对不上。改 base64 整块传输。
4. **jobLogId 浮点化** → 接口返回字符串/数字混合,归一成 int。
5. **KAT 报告域名** → `kat.corp.kuaishou.com` 已合并进 `ktest.corp.kuaishou.com`,访问会跳转。
6. **KAT 失败用例明细** → 报告页 SPA 默认表格空,需 UI 过滤才加载;接口反推中。产物里已含 KAT runtimeId 和 checkUrl,可另行深挖。

## 已验证

| 场景 | 结果 |
|---|---|
| feature 270240 全量拉取 | 15/15 job 日志 + 错误日志,3.6MB,含警告态「精准执行接口自动化」(61944 字符) |
| `--no-code` | 跳过 clone,正常 |
| 代码 clone | 复用本地仓库,分支对齐 commit 1cac94ca |

## 边界

- **只读**。不触发流水线,不改 kdev,不推 GitLab。
- **只拉准出流水线**(pass pipeline 验证那条),不拉其他历史流水线。
- 浏览器会话有成本,一次 `kdev_fetch.py` 建一次会话,批量场景应复用 `KdevSession`。
- 日志接口保留近期数据,历史 job 可能 404。