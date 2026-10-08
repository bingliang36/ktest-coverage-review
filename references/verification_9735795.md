# KTest 覆盖率接口可信度核验报告 - cid=9735795

核验时间: 2026-10-08

## 结论

用户指出的问题成立:旧文档“全部以 `/api/coverage` 为前缀”的表述不够严谨。

准确说法应为:

- 浏览器地址栏里的页面路由是 `/web/cov/...`。
- 页面实际加载数据时调用的后端 JSON 接口是 `/api/coverage/...`。
- `covfetch` 工具调用的是后端接口层,所以“取数链路可行”;但文档已补充“前端路由 vs 后端接口”的映射,避免误解。

## 用户给出的前端 URL

- 根页面: `https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795`
- 下钻页面: `https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795&id=1&type=undefined`
- 下钻页面: `https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795&id=64&type=2`
- 文件页面: `https://ktest.corp.kuaishou.com/web/cov/fileReport?cid=9735795&id=162&type=2`

## 实测证据 1: 打开根页面后,浏览器实际请求的 API

打开:

`https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795`

浏览器 performance entries 中出现:

```text
https://ktest.corp.kuaishou.com/api/coverage/getReportStatus?cid=9735795
https://ktest.corp.kuaishou.com/api/coverage/getCoverageNavigation?id=&cid=9735795
https://ktest.corp.kuaishou.com/api/coverage/getCoverageMessageList?cid=9735795&id=&searchWord=&elementType=&change=1&cover=&isInterface=
https://ktest.corp.kuaishou.com/api/coverage/getCoverageReportDetail?id=9735795
https://ktest.corp.kuaishou.com/api/coverage/getCoverageChangeInfo?projectId=113973&branch=feature_fix_compose_empty&commitId=1cac94ca44c2251538fc34c0695ba4b8859cc393&type=2
https://ktest.corp.kuaishou.com/api/coverage/change/getMethodInfo?id=9735795
https://ktest.corp.kuaishou.com/api/coverage/change/getFileInfo?id=9735795
https://ktest.corp.kuaishou.com/api/coverage/change/getInterfaceInfo?id=9735795
https://ktest.corp.kuaishou.com/api/coverage/config/getConfig?projectId=113973&key=incremental_coverage_expectation&active=1
```

说明: 根页面 `/web/cov/reportDetail?cid=9735795` 的数据确实来自 `/api/coverage/...`。

## 实测证据 2: 下钻页面 id=64 的实际 API

打开:

`https://ktest.corp.kuaishou.com/web/cov/reportDetail?cid=9735795&id=64&type=2`

浏览器 performance entries 中出现:

```text
https://ktest.corp.kuaishou.com/api/coverage/getCoverageNavigation?id=64&cid=9735795
https://ktest.corp.kuaishou.com/api/coverage/getCoverageMessageList?cid=9735795&id=64&searchWord=&elementType=&change=1&cover=&isInterface=
```

说明: `id=64` 是树形节点 id,前端页面用它调用 `getCoverageMessageList` 下钻。

## 实测证据 3: 文件页 id=162 的实际 API

打开:

`https://ktest.corp.kuaishou.com/web/cov/fileReport?cid=9735795&id=162&type=2`

浏览器 performance entries 中出现:

```text
https://ktest.corp.kuaishou.com/api/coverage/getCoverageNavigation?id=162&cid=9735795
https://ktest.corp.kuaishou.com/api/coverage/getReportStatus?cid=9735795
https://ktest.corp.kuaishou.com/api/coverage/getCoverageMethodDetail?cid=9735795&id=162
https://ktest.corp.kuaishou.com/api/coverage/conf/getConf?configList=covearge_case_config
https://ktest.corp.kuaishou.com/api/coverage/getHistoryList
```

页面文本中出现:

```text
MetadataResultProcessService
public class MetadataResultProcessService {
private static final String SUBTAG = "MetadataResultProcessService";
覆盖 / 未覆盖
```

说明: 文件页 `/web/cov/fileReport?...id=162` 对应后端 `getCoverageMethodDetail?cid=9735795&id=162`,返回源码与行级覆盖数据。

## 实测证据 4: 直接调用 API 的返回结构

在同一个浏览器 SSO 会话内直接 fetch:

```text
/api/coverage/getCoverageReportDetail?id=9735795
```

返回 HTTP 200 / status 200, data keys:

```text
id, name, projectName, branch, product, productVersion, pipelineId, owner
```

样例:

```json
{"id":9735795,"name":"feature_fix_compose_empty的官方增量报告","projectName":"locallife-aigc-genesis","branch":"feature_fix_compose_empty"}
```

直接 fetch:

```text
/api/coverage/getCoverageMessageList?cid=9735795&id=&searchWord=&elementType=&change=1&cover=&isInterface=
```

返回 HTTP 200 / status 200, data keys:

```text
total, list
```

样例包含根节点:

```json
{"element":"JaCoCo Coverage Report","pid":0,"id":1,"type":2}
```

直接 fetch:

```text
/api/coverage/getCoverageMessageList?cid=9735795&id=1&searchWord=&elementType=&change=1&cover=&isInterface=
```

返回 HTTP 200 / status 200,样例包含 package 节点:

```json
{"element":"com/kuaishou/locallife/aigc/genesis/video/infra/metadata","pid":1,"id":64,"type":3}
```

直接 fetch:

```text
/api/coverage/getCoverageMethodDetail?cid=9735795&id=162
```

返回 HTTP 200 / status 200, data keys:

```text
fileName, fileContent, lineList, beginLine, projectId, branch, fullPath, fileFullPath
```

样例:

```json
{"fileName":"MetadataResultProcessService.java","fileContent":"package com.kuaishou.locallife.aigc.genesis.video.infra.metadata;\n..."}
```

## 本地取数结果交叉校验

本地文件:

`data/coverage/coverage_9735795.json`

报告信息:

```text
report: feature_fix_compose_empty的官方增量报告
branch: feature_fix_compose_empty
commitId: 1cac94ca44c2251538fc34c0695ba4b8859cc393
```

聚合统计:

```json
{"executable":721,"ok":227,"miss":424,"part":70,"diff":31,"diffMiss":5,"diffPart":10,"diffOk":16,"fileCount":4,"lineCoverage":0.3148,"diffLineCoverage":0.8387}
```

4 个文件:

| classId | className | file |
|---:|---|---|
| 32662 | ComposeTaskTerminalizationService | com/kuaishou/locallife/aigc/genesis/compose/callback/ComposeTaskTerminalizationService.java |
| 32865 | ComposeFinalOutputResolver | com/kuaishou/locallife/aigc/genesis/compose/callback/ComposeFinalOutputResolver.java |
| 31421 | DagTaskNodeResultMasterReadService | com/kuaishou/locallife/aigc/genesis/infra/dag/DagTaskNodeResultMasterReadService.java |
| 156 | MetadataResultProcessService | com/kuaishou/locallife/aigc/genesis/video/infra/metadata/MetadataResultProcessService.java |

注意: 用户举例中的 `fileReport?id=162` 能打开 `MetadataResultProcessService.java`,但 `covfetch` 通过树遍历拿到的 classId 是 `156`。这说明前端 fileReport 的 `id` 可能允许传 file/class 附近不同层级节点,最终都可定位到同一文件;取数工具实际使用 `getCoverageMethodDetail` 的可用节点 id,不依赖页面 URL 的层级语义。

## 最终判断

- 文档中“后端接口全部以 `/api/coverage` 为前缀”是**可验证成立**的。
- 用户看到的 `/web/cov/reportDetail`、`/web/cov/fileReport` 是**前端页面路由**,不是后端接口路径。
- 旧文档的问题是**表述不够严谨**,没有显式说明页面路由和后端接口的区别;已修正 README。
- `covfetch` 当前数据可信,因为它调用的 API 与页面实际网络请求一致,且本地 JSON 能还原报告元信息、树结构、源码和 lineList。
