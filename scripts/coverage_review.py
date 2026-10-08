#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests>=2.31.0,<3",
#   "ks-aimate>=1.0.30",
# ]
#
# [tool.uv.sources]
# "ks-aimate" = { index = "kuaishou" }
#
# [[tool.uv.index]]
# name = "kuaishou"
# url = "https://pypi.corp.kuaishou.com/kuaishou/prod/+simple/"
# publish = false
# ///
"""KTest 覆盖率核验脚本。

stdout 最后一行始终输出标准 JSON。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from ks_aimate.sso_login_client import SmartSSOSession
except Exception:  # pragma: no cover
    SmartSSOSession = None

KTEST = "https://ktest.corp.kuaishou.com"
KDEV = "https://kdev.corp.kuaishou.com"
GITLAB = "https://git.corp.kuaishou.com"


class SkillError(Exception):
    def __init__(self, error: str, msg: str, code: int = 1, next_action: str = "report_error"):
        super().__init__(msg)
        self.error = error
        self.msg = msg
        self.code = code
        self.next_action = next_action


def now_ms() -> int:
    return int(time.time() * 1000)


def emit(ok: bool, code: int, msg: str, error: Optional[str] = None,
         data: Optional[Dict[str, Any]] = None, start_ms: Optional[int] = None,
         next_action: str = "done") -> None:
    obj = {
        "ok": ok,
        "code": code,
        "error": error,
        "msg": msg,
        "data": data or {},
        "next_action": next_action,
        "duration_ms": now_ms() - (start_ms or now_ms()),
    }
    print(json.dumps(obj, ensure_ascii=False))


def ensure_out(path: str) -> Path:
    out = Path(path)
    if not str(out).startswith("tmp/") and str(out) != "tmp":
        raise SkillError("OUTPUT_ERROR", "输出目录必须位于 tmp/ 下", 1, "choose_tmp_output")
    out.mkdir(parents=True, exist_ok=True)
    return out


class InternalClient:
    def __init__(self):
        if SmartSSOSession is None:
            raise SkillError("SSO_AUTH_FAILED", "无法导入 SmartSSOSession，请确认运行环境已安装 ks-aimate", 2, "retry_after_environment_ready")
        self.session = SmartSSOSession()

    def request_json(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        try:
            resp = self.session.request(method, url, **kwargs)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise SkillError("API_ERROR", f"接口请求失败: {url}: {exc}", 1, "report_api_failure") from exc
        status = data.get("status")
        code = data.get("code")
        if status not in (None, 0, 200) or code not in (None, 0, 200):
            raise SkillError("API_ERROR", f"接口业务失败: {url}: {data}", 1, "report_api_failure")
        return data

    def get_data(self, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        data = self.request_json("GET", base + path, params=params)
        return data.get("data")


class KTestClient:
    def __init__(self, http: InternalClient):
        self.http = http

    def detail(self, cid: int) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getCoverageReportDetail", {"id": cid})

    def status(self, cid: int) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getReportStatus", {"cid": cid})

    def message_list(self, cid: int, node_id: Optional[int]) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getCoverageMessageList", {
            "cid": cid,
            "id": "" if node_id is None else node_id,
            "searchWord": "",
            "elementType": "",
            "change": 1,
            "cover": "",
            "isInterface": "",
        })

    def method_detail(self, cid: int, node_id: int) -> Dict[str, Any]:
        return self.http.get_data(KTEST, "/api/coverage/getCoverageMethodDetail", {"cid": cid, "id": node_id})

    def summary(self, cid: int, kind: str) -> Dict[str, Any]:
        return self.http.get_data(KTEST, f"/api/coverage/change/get{kind}Info", {"id": cid})

    def threshold(self, project_id: int) -> Any:
        return self.http.get_data(KTEST, "/api/coverage/config/getConfig", {
            "projectId": project_id,
            "key": "incremental_coverage_expectation",
            "active": 1,
        })


def line_stats(line_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    st = {"executable": 0, "ok": 0, "miss": 0, "part": 0, "diff": 0, "diffMiss": 0, "diffPart": 0, "diffOk": 0}
    for row in line_list or []:
        covered = row.get("covered")
        diff = row.get("diff") == 1
        if covered in (1, 2, 3):
            st["executable"] += 1
        if covered == 1:
            st["miss"] += 1
        elif covered == 2:
            st["ok"] += 1
        elif covered == 3:
            st["part"] += 1
        if diff:
            st["diff"] += 1
            if covered == 1:
                st["diffMiss"] += 1
            elif covered == 2:
                st["diffOk"] += 1
            elif covered == 3:
                st["diffPart"] += 1
    return st


def walk_classes(client: KTestClient, cid: int) -> List[Dict[str, Any]]:
    classes: List[Dict[str, Any]] = []

    def dfs(node_id: Optional[int], chain: List[str]):
        data = client.message_list(cid, node_id)
        for item in data.get("list", []) or []:
            name = item.get("element") or item.get("name") or str(item.get("id"))
            typ = item.get("type")
            nid = item.get("id")
            next_chain = chain + [name]
            if typ == 5:
                classes.append({"id": nid, "className": name, "chain": next_chain, "raw": item})
            elif item.get("goTarget") or typ in (2, 3, 4):
                dfs(nid, next_chain)

    dfs(None, [])
    return classes


def coverage_cmd(args: argparse.Namespace, start_ms: int) -> None:
    cid = args.cid
    if not cid:
        raise SkillError("MISSING_ARGUMENT", "缺少 --cid", 2, "ask_for_cid")
    out = ensure_out(args.out or f"tmp/ktest-coverage-{cid}")
    http = InternalClient()
    client = KTestClient(http)
    report = client.detail(cid)
    classes = walk_classes(client, cid)
    files = []
    aggregate = {"executable": 0, "ok": 0, "miss": 0, "part": 0, "diff": 0, "diffMiss": 0, "diffPart": 0, "diffOk": 0}
    for cls in classes:
        detail = client.method_detail(cid, int(cls["id"]))
        st = line_stats(detail.get("lineList") or [])
        for k in aggregate:
            aggregate[k] += st[k]
        files.append({
            "classId": cls["id"],
            "className": cls["className"],
            "chain": cls["chain"],
            "fileName": detail.get("fileName"),
            "fileFullPath": detail.get("fileFullPath"),
            "fullPath": detail.get("fullPath"),
            "stats": st,
            "lineList": detail.get("lineList") or [],
            "fileContent": detail.get("fileContent"),
        })
    aggregate["fileCount"] = len(files)
    aggregate["lineCoverage"] = round(aggregate["ok"] / aggregate["executable"], 4) if aggregate["executable"] else 0
    aggregate["diffLineCoverage"] = round((aggregate["diff"] - aggregate["diffMiss"]) / aggregate["diff"], 4) if aggregate["diff"] else 0
    payload = {
        "cid": cid,
        "source": {"ktest": f"{KTEST}/web/cov/reportDetail?cid={cid}"},
        "report": report,
        "files": files,
        "aggregate": aggregate,
    }
    out_file = out / f"coverage_{cid}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    emit(True, 0, "覆盖率数据已拉取", data={"output": str(out_file), "aggregate": aggregate}, start_ms=start_ms)


def verify_cmd(args: argparse.Namespace, start_ms: int) -> None:
    cid = args.cid
    if not cid:
        raise SkillError("MISSING_ARGUMENT", "缺少 --cid", 2, "ask_for_cid")
    out = ensure_out(args.out or f"tmp/ktest-verify-{cid}")
    http = InternalClient()
    client = KTestClient(http)
    detail = client.detail(cid)
    root = client.message_list(cid, None)
    first = (root.get("list") or [{}])[0]
    first_id = first.get("id")
    child = client.message_list(cid, first_id) if first_id else {"list": []}
    child_first = (child.get("list") or [{}])[0]
    method_candidate = child_first.get("id") or first_id
    method_detail = client.method_detail(cid, int(method_candidate)) if method_candidate else {}
    lines = [
        f"# KTest 路由与接口核验 - cid={cid}",
        "",
        "## 结论",
        "",
        "`/web/cov/...` 是前端页面路由；页面背后的数据接口是 `/api/coverage/...`。",
        "",
        "## 已验证接口",
        "",
        f"- `/api/coverage/getCoverageReportDetail?id={cid}` -> `{detail.get('name')}`",
        f"- `/api/coverage/getCoverageMessageList?cid={cid}&id=` -> 根节点 `{first.get('element')}` id={first_id}",
        f"- `/api/coverage/getCoverageMessageList?cid={cid}&id={first_id}` -> 子节点数量 {len(child.get('list') or [])}",
        f"- `/api/coverage/getCoverageMethodDetail?cid={cid}&id={method_candidate}` -> 文件 `{method_detail.get('fileName')}`",
        "",
        "## 前端路由示例",
        "",
        f"- `{KTEST}/web/cov/reportDetail?cid={cid}`",
        f"- `{KTEST}/web/cov/reportDetail?cid={cid}&id={first_id}&type=2`",
        f"- `{KTEST}/web/cov/fileReport?cid={cid}&id={method_candidate}&type=2`",
    ]
    out_file = out / f"verification_{cid}.md"
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit(True, 0, "接口映射核验完成", data={"output": str(out_file), "report": detail.get("name")}, start_ms=start_ms)


class KDevClient:
    def __init__(self, http: InternalClient):
        self.http = http

    def pass_pipeline(self, feature_id: str) -> Dict[str, Any]:
        return self.http.get_data(KDEV, "/api/artemis/task/pass/pipeline", {
            "sourceId": feature_id,
            "relationType": 3,
            "testType": 1,
            "taskId": 500685,
            "disableCache": "false",
        })

    def job_meta(self, job_id: int) -> Dict[str, Any]:
        return self.http.get_data(KDEV, "/api/kdev/pipeline/pipelineJobLog", {"id": job_id})

    def job_log(self, job_id: int, error: bool = False) -> str:
        path = "/api/kdev/pipeline/pipelineJobLog/errorLog" if error else "/api/kdev/pipeline/pipelineJobLog/log"
        data = self.http.get_data(KDEV, path, {"id": job_id, "start": 0})
        return (data or {}).get("content") or ""


def parse_feature_id(url: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    fid = q.get("id", [None])[0]
    if not fid:
        raise SkillError("INVALID_URL", "KDev feature URL 中未找到 id 参数", 2, "ask_for_valid_url")
    return fid


def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name or "job")


def kdev_cmd(args: argparse.Namespace, start_ms: int) -> None:
    if not args.feature_url:
        raise SkillError("MISSING_ARGUMENT", "缺少 --feature-url", 2, "ask_for_feature_url")
    fid = parse_feature_id(args.feature_url)
    out = ensure_out(args.out or f"tmp/kdev-feature-{fid}")
    logs_dir = out / "logs"
    logs_dir.mkdir(exist_ok=True)
    http = InternalClient()
    client = KDevClient(http)
    data = client.pass_pipeline(fid)
    items = data.get("list") or []
    if not items:
        raise SkillError("API_ERROR", f"feature {fid} 没有准出流水线数据", 1, "report_no_pipeline")
    branch = items[0]
    jobs = []
    for pipe in branch.get("pipelineList") or []:
        plog = pipe.get("pipelineLog") or {}
        for job in plog.get("jobLogList") or []:
            jid = int(float(job.get("id")))
            name = job.get("name") or str(jid)
            meta = client.job_meta(jid)
            content = client.job_log(jid, error=False)
            err = client.job_log(jid, error=True)
            log_file = logs_dir / f"{jid}_{safe_name(name)}.log"
            log_file.write_text(content, encoding="utf-8")
            err_file = None
            if err:
                err_file = logs_dir / f"{jid}_{safe_name(name)}.error.log"
                err_file.write_text(err, encoding="utf-8")
            jobs.append({"jobLogId": jid, "name": name, "status": job.get("statusDesc") or job.get("status"), "meta": meta, "log": str(log_file), "errorLog": str(err_file) if err_file else None})
    payload = {"featureId": fid, "branch": branch, "jobs": jobs}
    out_file = out / f"kdev_feature_{fid}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    emit(True, 0, "KDev 日志已拉取", data={"output": str(out_file), "jobCount": len(jobs), "logsDir": str(logs_dir)}, start_ms=start_ms)


def git_clone_cmd(git_url: str, branch: Optional[str], dest: Path) -> Dict[str, Any]:
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        raise SkillError("GIT_AUTH_FAILED", "未设置 GITLAB_TOKEN，无法拉取 GitLab 代码", 2, "ask_for_git_token_or_skip")
    if dest.exists():
        return {"codeDir": str(dest), "reused": True}
    env = os.environ.copy()
    env["GL_T"] = token
    cmd = [
        "git",
        "-c", "credential.helper=!f(){ echo \"username=oauth2\"; echo \"password=$GL_T\"; };f",
        "clone",
    ]
    if branch:
        cmd += ["--branch", branch]
    cmd += [git_url, str(dest)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise SkillError("GIT_AUTH_FAILED", f"Git clone 失败: {r.stderr[-500:]}", 2, "ask_for_git_credential_or_skip_code")
    return {"codeDir": str(dest), "reused": False}


def require_remote_agent(start_ms: int) -> Optional[int]:
    if not os.environ.get("MYFLICKER_REMOTE_AGENT_METHOD"):
        emit(False, 2, "该脚本可能长时间拉取内网数据，请通过 call_remote_agent(method=CALLBACK) 执行", error="REMOTE_AGENT_REQUIRED", start_ms=start_ms, next_action="RETRY_WITH_CALL_REMOTE_AGENT")
        return 2
    return None


def main() -> int:
    start_ms = now_ms()
    remote_code = require_remote_agent(start_ms)
    if remote_code is not None:
        return remote_code
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("coverage")
    p.add_argument("--cid", type=int, required=True)
    p.add_argument("--out", default=None)
    p = sub.add_parser("verify")
    p.add_argument("--cid", type=int, required=True)
    p.add_argument("--out", default=None)
    p = sub.add_parser("kdev")
    p.add_argument("--feature-url", required=True)
    p.add_argument("--out", default=None)
    args = parser.parse_args()
    try:
        if args.cmd == "coverage":
            coverage_cmd(args, start_ms)
        elif args.cmd == "verify":
            verify_cmd(args, start_ms)
        elif args.cmd == "kdev":
            kdev_cmd(args, start_ms)
        return 0
    except SkillError as exc:
        print(f"[error] {exc.error}: {exc.msg}", file=sys.stderr)
        emit(False, exc.code, exc.msg, error=exc.error, start_ms=start_ms, next_action=exc.next_action)
        return exc.code
    except KeyboardInterrupt:
        emit(False, 130, "用户中断", error="INTERRUPTED", start_ms=start_ms, next_action="stop")
        return 130
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit(False, 1, str(exc), error="UNEXPECTED_ERROR", start_ms=start_ms, next_action="report_diagnostic")
        return 1


if __name__ == "__main__":
    sys.exit(main())
