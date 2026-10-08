#!/usr/bin/env python3
"""KTest 覆盖率取数层 —— 浏览器会话封装。

KTest 走 SSO/OBO,git 走 PAT,两套认证互不通用。
本模块只负责 KTest 侧:用 agent-browser 建立一次 SSO 会话,
之后所有接口通过页面内 fetch(credentials:'include') 调用。

为什么不用 curl:KTest 的 _gitlab_session 式 cookie 无法跨进程重放,
OAuth state 绑定在浏览器那次请求里,curl 重放必然 422。实测结论。
"""
import json
import os
import re
import subprocess
import sys

WS = os.environ.get('COVFETCH_WS') or os.getcwd()
KBROWSE = os.path.join(WS, 'skills/agent-browser/scripts/kbrowse.sh')
BASE = 'https://ktest.corp.kuaishou.com'


class KTestSession:
    """一次 SSO 会话,复用给多次接口调用。务必用 with 语句,确保浏览器关闭。"""

    def __init__(self, anchor_url=None, verbose=True):
        self.anchor = anchor_url or f'{BASE}/web/cov/collection'
        self.sid = None
        self.verbose = verbose

    def _log(self, msg):
        if self.verbose:
            print(f'[ktest] {msg}', file=sys.stderr)

    def __enter__(self):
        self.sid = subprocess.run(
            ['bash', KBROWSE, 'new-session'],
            capture_output=True, text=True, check=True).stdout.strip()
        self._log(f'session={self.sid}')
        self._log(f'SSO 登录中: {self.anchor}')
        r = subprocess.run(
            ['bash', KBROWSE, '--browser-session', self.sid, 'open', self.anchor],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f'SSO/open 失败: {r.stderr[-400:]}')
        self._log('登录完成')
        return self

    def __exit__(self, *exc):
        if self.sid:
            subprocess.run(['bash', KBROWSE, '--browser-session', self.sid, 'close'],
                           capture_output=True, timeout=120)
            self._log('浏览器已关闭')
        return False

    def _eval(self, js, timeout=180):
        r = subprocess.run(
            ['bash', KBROWSE, '--browser-session', self.sid, 'eval', js],
            capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip()
        if not out:
            raise RuntimeError(f'eval 无输出: {r.stderr[-300:]}')
        # agent-browser 把返回值包成 JSON 字符串字面量
        last = out.splitlines()[-1]
        if last.startswith('"'):
            last = json.loads(last)
        return last

    def get(self, path):
        js = (f"(async()=>{{const r=await fetch({json.dumps(path)},"
              "{credentials:'include'});return await r.text();})()")
        return self._parse(self._eval(js), path)

    def post(self, path, body):
        js = (f"(async()=>{{const r=await fetch({json.dumps(path)},"
              "{method:'POST',headers:{'Content-Type':'application/json'},"
              f"credentials:'include',body:JSON.stringify({json.dumps(body)})}});"
              "return await r.text();})()")
        return self._parse(self._eval(js), path)

    @staticmethod
    def _parse(text, path):
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f'{path} 返回非 JSON: {text[:200]}')
        if d.get('status') != 200:
            raise RuntimeError(f"{path} 失败: status={d.get('status')} {d.get('message')}")
        return d['data']
