# 覆盖率取数层 (covfetch)

把 KTest 覆盖率数据 + GitLab 代码拉成一份标准 JSON,作为分析层的唯一输入。

## 为什么是两个数据源

| 数据源 | 认证 | 提供什么 | 不能提供什么 |
|---|---|---|---|
| **KTest** | SSO / OBO | 覆盖率汇总、树形下钻、**源码 + 行级覆盖状态 + 变更标记** | 完整仓库、调用关系、历史 |
| **GitLab** | PAT | 完整代码、diff、commit 历史、测试文件清单 | 覆盖率(该仓库没有 GitLab CI,流水线走 kdev) |

两边靠 `projectId` 和 `commitId` 对齐。KTest 告诉你"哪些行没覆盖",GitLab 的代码告诉你"那些行是什么逻辑、该不该覆盖"。

## 快速开始

```bash
cd <workspace>
export COVFETCH_WS=$PWD

# 已知 cid
uv run tools/covfetch/fetch.py --cid 9735795

# cid 自动发现(仓库 + 分支 -> 最新增量报告)
uv run tools/covfetch/fetch.py --project 113973 --branch feature_deploy

# 顺带 clone 代码
uv run tools/covfetch/fetch.py --cid 9735795 --with-code

# 探索
uv run tools/covfetch/fetch.py --project 113973 --list-reports
uv run tools/covfetch/fetch.py --project 113973 --list-branches
```

输出: `data/coverage/coverage_<cid>.json`

## 认证

### KTest —— SSO/OBO,零配置

`agent-browser` 的 wrapper 自动注入 OboToken,`open` 一次建立会话,之后所有接口用页面内 `fetch(credentials:'include')` 调用。

**踩过的坑(别重走)**:

- KTest 走 CAS + `oauth2_generic` 回调换自己的会话。直接拿 SSO 的 cookie 用 curl 打 API 会 **401**。
- 手动用 curl 重放 OAuth 回调会 **422** —— `state` 绑定在浏览器那次请求里,跨进程重放必然失败。这是协议设计,不是参数没调对。
- **结论:KTest 侧必须走浏览器会话,不要试图用 curl 复现。**

### GitLab —— PAT

token 放 `.secrets/gitlab_token`(已加 `.gitignore`,权限 600),或设 `GITLAB_TOKEN` 环境变量。

scope 只需 `read_api` + `read_repository`。

**克隆的关键坑**:

```bash
# ❌ 无效 —— git 走 Basic Auth,不看这个头
git -c http.extraHeader="PRIVATE-TOKEN: $T" clone ...

# ✅ 正确 —— credential helper 喂 oauth2 + token,且 token 走 env
git -c credential.helper='!f(){ echo "username=oauth2"; echo "password=$GL_T"; };f' clone ...
```

token 必须走环境变量。写进命令行会落进 shell history 和 `/proc/<pid>/cmdline`。

## KTest 前端路由 vs 后端接口(已重新核验)

这里必须分清两层,否则容易误判:

- **前端页面路由**: 用户在浏览器地址栏看到的是 `/web/cov/...`,例如:
  - `https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795`
  - `https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795&id=64&type=2`
  - `https://ktest.corp.kuaishou.com/web/cov/fileReport?cid=9735795&id=162&type=2`
- **后端数据接口**: 上述页面加载后,浏览器实际请求 `/api/coverage/...` 获取 JSON 数据。`covfetch` 取数层调用的是这一层。

换句话说,旧文档里“全部以 `/api/coverage` 为前缀”只适用于**后端接口清单**,不适用于用户可打开的**前端页面 URL**。这个表述已修正。

### 前端路由到后端接口的实测映射(cid=9735795)

| 前端页面 | 页面实际请求的关键接口 | 用途 |
|---|---|---|
| `/web/cov/reportDetail?cid=9735795` | `/api/coverage/getCoverageReportDetail?id=9735795` | 报告元信息 |
| `/web/cov/reportDetail?cid=9735795` | `/api/coverage/getCoverageMessageList?cid=9735795&id=&change=1...` | 根节点树 |
| `/web/cov/reportDetail?cid=9735795&id=64&type=2` | `/api/coverage/getCoverageNavigation?id=64&cid=9735795` | 面包屑/导航 |
| `/web/cov/reportDetail?cid=9735795&id=64&type=2` | `/api/coverage/getCoverageMessageList?cid=9735795&id=64&change=1...` | 下钻到 package/file/class |
| `/web/cov/fileReport?cid=9735795&id=162&type=2` | `/api/coverage/getCoverageMethodDetail?cid=9735795&id=162` | 文件源码 + lineList 行级覆盖 |

### KTest 后端接口清单(全部实测可用)

| 方法 | 完整路径 | 用途 |
|---|---|---|
| POST | `/api/coverage/getCoverageOriginReportList` | 报告列表 —— **cid 发现入口** |
| POST | `/api/coverage/getBranchList` | 分支列表 |
| GET | `/api/coverage/getReportStatus?cid={cid}` | 报告生成状态 |
| GET | `/api/coverage/getCoverageNavigation?id={节点id}&cid={cid}` | 当前节点导航/面包屑 |
| GET | `/api/coverage/getCoverageReportDetail?id={cid}` | 报告元信息(仓库/分支/commit/流水线/负责人) |
| GET | `/api/coverage/getCoverageChangeInfo?projectId&branch&commitId&type` | 本次变更信息 |
| GET | `/api/coverage/change/getMethodInfo?id={cid}` | 变更方法汇总 |
| GET | `/api/coverage/change/getFileInfo?id={cid}` | 变更文件汇总 |
| GET | `/api/coverage/change/getInterfaceInfo?id={cid}` | 变更接口汇总 |
| GET | `/api/coverage/config/getConfig?projectId&key=incremental_coverage_expectation&active=1` | 卡点阈值 |
| GET | `/api/coverage/getCoverageMessageList?cid={cid}&id={父id}&change=1...` | 树形下钻 |
| GET | `/api/coverage/getCoverageMethodDetail?cid={cid}&id={类/文件节点id}` | **源码 + lineList** |

### 树形结构

```
type=2  root    JaCoCo Coverage Report
type=3  package com/kuaishou/.../compose/callback
type=4  file    ComposeFinalOutputResolver.java
type=5  class   ComposeFinalOutputResolver     ← 通常用这层的 id 取源码
type=6  method  toJson(Object)
```

`getCoverageMessageList` 传空 `id` 取根节点,逐层用返回的 `id` 下钻。`change=1` 只看变更部分。

`getCoverageMethodDetail?cid&id=` 对 **class 节点 id** 和部分 **method 节点 id** 都能返回同一文件的完整源码 + lineList。实测 cid=9735795 时:

- `id=156` = `MetadataResultProcessService` class 节点,返回 `MetadataResultProcessService.java` 全量源码。
- `id=162` = `recordWorkflowIdFailed(...)` method 节点,同样返回 `MetadataResultProcessService.java` 全量源码。
- 前端 `/web/cov/fileReport?cid=9735795&id=162&type=2` 就属于这种“method 节点直接定位到文件”的页面路由。

取数工具为了避免重复,遍历时只收集 `type=5` class 节点;这不影响文件级 lineList 的完整性。

### lineList 语义(实测反推)

```json
{"line": 642, "covered": 1, "branch": 0, "diff": 1, "cl": []}
```

| 字段 | 值 | 含义 |
|---|---|---|
| `covered` | 0 | 非可执行行(空行、注释、声明) |
| | 1 | **未覆盖** |
| | 2 | 已覆盖 |
| | 3 | **部分覆盖**(行执行了,分支没走全) |
| `diff` | 1 | 本次变更行 |
| `branch` | 1 | 该行含分支 |
| `cl` | [] | 分支明细 |

**`diff=1 && covered=1`** = 增量未覆盖行,卡点盯的就是这个。

**`diff=1 && covered=3`** = 变更行分支没走全。行覆盖率口径下算"已覆盖",容易被忽略,但往往是真正的测试缺口。

## cid 自动发现

```
projectId (+ branch, + type) -> getCoverageOriginReportList -> 取最新一条 id
```

**限制**:该接口只保留近期数据,旧报告会滚出列表。实测 `feature_fix_compose_empty`(9 月 16 日)已查不到,必须手动指定 cid。

**参数注意**:`projectName` 过滤**不生效**(会返回其他项目),必须用 `projectId`。分支参数名是 `branch`,`branchName`/`branchList` 都会被忽略(静默返回全量)。

## 输出 JSON 结构

```jsonc
{
  "cid": 9735795,
  "source": { "ktest": "https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795" },
  "report": {
    "name": "...", "projectId": 113973, "projectName": "locallife-aigc-genesis",
    "branch": "...", "commitId": "...", "pipelineId": 1009036, "owner": "...",
    "type": 2,                     // 1=全量 2=增量
    "projectUrl": "...", "pipelineUrl": "...",
    "coveredLines": 26, "missedLines": 5, "totalLines": 31
  },
  "threshold": "60",
  "changeSummary": {
    "method":    { "total": 7, "covered": 5, "missed": 2 },
    "file":      { "total": 4, "covered": 2, "missed": 2 },
    "interface": { "total": 0, "covered": 0, "missed": 0 }
  },
  "files": [{
    "classId": 32865, "className": "ComposeFinalOutputResolver",
    "filePath": "com/kuaishou/.../ComposeFinalOutputResolver.java",
    "totalLines": 679,
    "stats": {
      "executable": 374, "ok": 37, "miss": 310, "part": 27,
      "diff": 4, "diffMiss": 2, "diffPart": 0, "diffOk": 2
    },
    "lineList": [ /* 全量行级数据 */ ],
    "fileContent": "package com.kuaishou..."   // 完整源码
  }],
  "aggregate": {
    "fileCount": 4, "executable": 721, "ok": ..., "miss": ..., "part": ...,
    "diff": 31, "diffMiss": 5, "diffPart": 10, "diffOk": 16,
    "lineCoverage": 0.xxxx, "diffLineCoverage": 0.xxxx
  },
  "codeDir": "<workspace>/repos/locallife-aigc-genesis"   // --with-code 时才有
}
```

**`fileContent` + `lineList` 是分析层的核心** —— 有了这两个,可以把任意一行还原成"代码 + 覆盖状态 + 是否变更",这是做归因判断的基础。

## 文件说明

| 文件 | 职责 |
|---|---|
| `ktest_session.py` | 浏览器会话封装(SSO + fetch),`with` 语句保证关闭 |
| `ktest_api.py` | KTest 接口封装 + cid 发现 + 树遍历 |
| `gitlab_api.py` | GitLab API + clone(正确的凭据姿势) |
| `fetch.py` | CLI 主入口,串起全流程 |

## 已验证

| 场景 | 结果 |
|---|---|
| `--cid 9735795` | 4 文件 / 31 变更行 / 未覆盖 5,与手工核对一致 |
| `--project 113973 --branch feature_ad_auto_chain` | 自动发现 cid=9773542,8 文件 / 211 变更行 |
| `--with-code` | clone 980 文件成功 |
| `--list-reports` / `--list-branches` | 正常 |

## 边界

- **只读**。不写 KTest,不推 GitLab。
- **不做分析**。取数层只负责把数据拉全、拉准。归因判断("这行是埋点容错所以不用测")依赖读源码上下文,留给分析层现做。
- **浏览器会话有成本**。一次 `fetch.py` 建一次会话,批量场景应该复用 `KTestSession` 而不是循环调 CLI。
- **旧报告查不到**。列表接口只保留近期数据,历史报告必须手动给 cid。
