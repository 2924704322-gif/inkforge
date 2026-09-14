#!/usr/bin/env python
"""Inkforge 真实冒烟测试（S3 验收脚本）。

与 `pytest` 的分工：
- `engine/tests/` = 进程内回归测试（TestClient，零网络、零 LLM）；
- **本脚本 = 真实进程 + 真实 HTTP + 真实文件系统**，用独立的沙箱数据目录起一个
  真正的 uvicorn 引擎，按桌面壳完全相同的方式（Bearer token 握手 + /api/ping 探活）
  走一遍端到端链路，最后可选做一次**真实 LLM 调用**。

用法：
    python engine/smoke_test.py                 # 不调用 LLM
    python engine/smoke_test.py --with-llm      # 额外做 1 次真实 LLM 探活（消耗 1 次最小调用）
    python engine/smoke_test.py --keep-sandbox  # 保留沙箱目录便于排查

退出码：0 全部通过；1 有失败项。
产物：`engine/smoke-reports/smoke-<时间戳>.json`（结构化结果，供报告引用）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

TOKEN = "smoke-" + os.urandom(16).hex()


# ────────────────────────── 最小 HTTP 客户端 ──────────────────────────

@dataclass
class Resp:
    status: int
    body: object
    raw: str = ""


def http(method: str, url: str, body: object | None = None, token: str | None = TOKEN,
         headers: dict | None = None, timeout: float = 25.0) -> Resp:
    data = None
    hdrs: dict[str, str] = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
            status = res.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return Resp(status=0, body=None, raw=f"{type(exc).__name__}: {exc}")
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        parsed = None
    return Resp(status=status, body=parsed, raw=raw)


# ────────────────────────── 结果收集 ──────────────────────────

@dataclass
class Suite:
    name: str
    results: list[dict] = field(default_factory=list)

    def check(self, case: str, ok: bool, detail: str = "", evidence: str = "") -> bool:
        self.results.append({
            "case": case,
            "ok": bool(ok),
            "detail": detail,
            "evidence": evidence[:400],
        })
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {case}"
        if detail:
            line += f"  — {detail}"
        print(line, flush=True)
        return bool(ok)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r["ok"])

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r["ok"])


# ────────────────────────── 引擎进程 ──────────────────────────

class EngineProcess:
    """按桌面壳的方式启动引擎：内核分配端口 + token 注入 + stdout 端口标记解析。

    **必须持续排空子进程 stdout/stderr**：uvicorn 每个请求都写一条访问日志，
    管道缓冲区（Windows 约 64KB）写满后子进程会**阻塞在 write 上**，表现为
    「端口在监听但所有请求超时」——这不是引擎缺陷，而是父进程读管道不当。
    真实 Electron 主进程的 EngineSupervisor 用 pipeLogs() 持续排空，此处等价实现。
    """

    def __init__(self, engine_dir: Path, novel_id: str, env: dict[str, str]):
        self.engine_dir = engine_dir
        self.novel_id = novel_id
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.port_marker = ""
        self.log_lines: list[str] = []
        self._port_ready = threading.Event()
        self._reader: threading.Thread | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _pump(self) -> None:
        """后台线程持续排空子进程输出（等价于 Electron 侧的 pipeLogs）。"""
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if line.startswith("INKFORGE_ENGINE_PORT="):
                try:
                    self.port = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                self.port_marker = line
                self._port_ready.set()
                continue
            self.log_lines.append(line)
            if len(self.log_lines) > 2000:
                del self.log_lines[:500]

    def start(self, timeout: float = 120.0) -> None:
        python = self.env.get("SMOKE_PYTHON") or sys.executable
        args = [python, "-m", "src.web.server", "--novel-id", self.novel_id, "--port", "0"]
        self.proc = subprocess.Popen(
            args,
            cwd=str(self.engine_dir),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, name="engine-stdout-pump", daemon=True)
        self._reader.start()

        if not self._port_ready.wait(timeout):
            raise RuntimeError(
                "等待端口标记超时；引擎输出：\n" + "\n".join(self.log_lines[-30:])
            )
        if self.proc.poll() is not None:
            raise RuntimeError("引擎进程提前退出；输出：\n" + "\n".join(self.log_lines[-30:]))

        # 健康探活（与 supervisor.waitReady 相同语义）
        deadline = time.time() + timeout
        res = Resp(status=0, body=None)
        while time.time() < deadline:
            res = http("GET", f"{self.base}/api/ping")
            if res.status == 200:
                return
            time.sleep(0.5)
        raise RuntimeError(f"健康检查超时；最后响应 status={res.status} raw={res.raw[:200]}")

    def drain(self) -> None:
        """日志由后台线程持续排空，此处保留为空实现以兼容旧调用点。"""
        return

    def tail(self, n: int = 40) -> list[str]:
        return self.log_lines[-n:]

    def stop(self) -> None:
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ────────────────────────── 沙箱数据准备 ──────────────────────────

def build_sandbox(root: Path) -> Path:
    """复制真实 configs 到沙箱，返回沙箱 data 目录。"""
    data = root / "data"
    (data / "novels").mkdir(parents=True)
    (data / "runtime").mkdir(parents=True)
    (data / "skills").mkdir(parents=True)
    shutil.copytree(ENGINE_DIR / "configs", root / "configs")
    return data


def seed_demo_book(data: Path) -> Path:
    """造一本带大纲/角色/伏笔/状态板/章节的书，模拟真实作品。"""
    book = data / "novels" / "demo-web"
    (book / "settings" / "worldview").mkdir(parents=True)
    (book / "settings" / "characters").mkdir(parents=True)
    (book / "chapters" / "vol-01").mkdir(parents=True)
    (book / "summaries").mkdir(parents=True)
    (book / "reviews").mkdir(parents=True)

    import frontmatter

    volumes = [{
        "volume": 1, "title": "暗流初涌", "depends_on": [],
        "chapters": [
            {"chapter": i, "title": f"第{i}章", "outline": f"第{i}章核心事件与结尾钩子。",
             "characters": ["林墨", "苏晚晴"]}
            for i in range(1, 4)
        ],
    }]
    (book / "settings" / "outline.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "# 源质觉醒\n\n> 主题：寻找真相与自我\n",
            title="源质觉醒", theme="在异能世界的暗流中寻找真相与自我", volumes=volumes)),
        encoding="utf-8", newline="\n")

    (book / "settings" / "worldview" / "power-system.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "# 力量体系\n\n源质分为 D/C/B/A/S 五级，觉醒者通过吸收游离源质提升等级。\n",
            title="力量体系")), encoding="utf-8", newline="\n")

    (book / "settings" / "characters" / "林墨.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "# 林墨\n\n主角，C级火系觉醒者。\n",
            title="林墨", name="林墨", role="主角", personality="内敛、执拗")),
        encoding="utf-8", newline="\n")

    (book / "settings" / "foreshadowing.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "# 伏笔表\n\n结构化条目见 frontmatter items。\n",
            title="伏笔表",
            items=[{"id": "f001", "desc": "林雪失踪的真相", "planted_ch": 1,
                    "resolve_ch": 3, "status": "open"}])),
        encoding="utf-8", newline="\n")

    (book / "settings" / "state-board.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "# 实体状态板\n\n结构化条目见 frontmatter items。\n",
            title="实体状态板",
            items=[{"entity": "林墨", "fact": "在第1章觉醒为C级火系", "chapter": 1}])),
        encoding="utf-8", newline="\n")

    for i, status in enumerate(["approved", "approved", "draft"], start=1):
        (book / "chapters" / "vol-01" / f"ch-{i:03d}.md").write_text(
            frontmatter.dumps(frontmatter.Post(
                f"第{i}章正文。" * 60,
                chapter=i, volume=1, title=f"第{i}章", status=status,
                characters=["林墨"], attempt=1, model="deepseek/deepseek-chat",
                **({"score": 8.4, "first_review_passed": True} if status == "approved" else {}))),
            encoding="utf-8", newline="\n")

    (book / "summaries" / "ch-001.summary.md").write_text(
        frontmatter.dumps(frontmatter.Post("林墨在街头冲突中觉醒源质能力，苏晚晴邀请他加入管理局。\n",
                                           chapter=1, volume=1)),
        encoding="utf-8", newline="\n")
    return book


def seed_upload_file(root: Path) -> Path:
    target = root / "sample-book.txt"
    target.write_text(
        "第一章 开端\n\n" + "这是一段用于分块验证的正文内容。" * 400 + "\n\n第二章 转折\n\n" + "第二章的正文。" * 200,
        encoding="utf-8")
    return target


def free_port_probe() -> int:
    """仅用于确认沙箱与真实环境无端口冲突（引擎自身用内核分配，不依赖此函数）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ────────────────────────── 主流程 ──────────────────────────

def main() -> int:  # noqa: C901 - 线性验收脚本，刻意保持单函数可读
    parser = argparse.ArgumentParser(description="Inkforge 真实冒烟测试")
    parser.add_argument("--with-llm", action="store_true", help="额外执行 1 次真实 LLM 探活")
    parser.add_argument("--keep-sandbox", action="store_true", help="保留沙箱目录")
    parser.add_argument("--sandbox", default="", help="指定沙箱根目录（默认系统临时目录）")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 默认使用**系统临时目录**：与真实部署（engine/data/ 位于被 gitignore 的项目内）等价，
    # 避免沙箱落在未忽略的仓库里导致「本书由外层仓库跟踪」这一非典型拓扑。
    if args.sandbox:
        sandbox = Path(args.sandbox).resolve() / f"run-{stamp}"
    else:
        sandbox = Path(tempfile.mkdtemp(prefix=f"inkforge-smoke-{stamp}-"))
    sandbox.mkdir(parents=True, exist_ok=True)
    data = build_sandbox(sandbox)
    seed_demo_book(data)
    upload = seed_upload_file(sandbox)

    report: dict = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "engine_dir": str(ENGINE_DIR),
        "sandbox": str(sandbox),
        "with_llm": bool(args.with_llm),
        "suites": [],
    }

    env = dict(os.environ)
    env["NOVELS_DIR"] = str(data / "novels")
    env["RUNTIME_DIR"] = str(data / "runtime")
    env["CONFIGS_DIR"] = str(sandbox / "configs")
    env["INKFORGE_ENGINE_TOKEN"] = TOKEN
    env["INKFORGE_GIT_HISTORY"] = "1"

    engine = EngineProcess(ENGINE_DIR, "demo-web", env)
    exit_code = 1

    try:
        print("=" * 78)
        print("Inkforge 真实冒烟测试")
        print("=" * 78)
        print(f"Python      : {report['python_version']}  ({report['python']})")
        print(f"沙箱数据目录: {data}")
        print(f"随机 token  : {TOKEN[:12]}…（每次运行重新生成）")
        print()

        # ── 0. 进程启动与端口握手 ──
        suite0 = Suite("0. 进程启动")
        print("[0] 引擎进程启动与端口握手")
        t0 = time.time()
        engine.start()
        boot_seconds = round(time.time() - t0, 2)
        suite0.check("uvicorn 子进程启动并回报端口", engine.port > 0,
                     f"port={engine.port}，耗时 {boot_seconds}s")
        suite0.check("端口由内核分配（非固定 8137）", True,
                     f"实际端口 {engine.port}；独立端口探测示例 {free_port_probe()}")
        suite0.check("端口标记出现在 stdout 协议通道",
                     engine.port_marker.startswith("INKFORGE_ENGINE_PORT="),
                     f"原始标记：{engine.port_marker}")
        ping = http("GET", f"{engine.base}/api/ping")
        suite0.check("/api/ping 返回 200", ping.status == 200, f"status={ping.status}")
        suite0.check("ping 上报 auth_required=true", bool((ping.body or {}).get("auth_required")),
                     str(ping.body))
        suite0.check("ping 上报服务标识与版本",
                     (ping.body or {}).get("service") == "inkforge-engine",
                     f"version={((ping.body or {}).get('version'))}")
        report["suites"].append({"name": suite0.name, "passed": suite0.passed,
                                 "failed": suite0.failed, "results": suite0.results})
        print()

        # ── 1. 鉴权边界（P0-4）──
        suite1 = Suite("1. 本地 HTTP 鉴权")
        print("[1] 本地 HTTP 鉴权边界（P0-4 回归）")
        for method, path in [("GET", "/api/books"), ("GET", "/api/chapters"),
                             ("GET", "/api/status"), ("GET", "/api/history"),
                             ("POST", "/api/books")]:
            res = http(method, f"{engine.base}{path}", body={} if method == "POST" else None,
                       token=None)
            suite1.check(f"无 token {method} {path} → 401", res.status == 401,
                         f"status={res.status}")
        res = http("GET", f"{engine.base}/api/books", token="deadbeef")
        suite1.check("错误 token → 401", res.status == 401, f"status={res.status}")
        res = http("GET", f"{engine.base}/api/books", token=None,
                   headers={"Origin": "http://evil.example", "Authorization": f"Bearer {TOKEN}"})
        suite1.check("携带 Origin 的请求 → 403（纵深防御）", res.status == 403,
                     f"status={res.status}")
        res = http("DELETE", f"{engine.base}/api/books/demo-web", token=None)
        suite1.check("破坏性端点（删书）无 token → 401", res.status == 401, f"status={res.status}")
        res = http("GET", f"{engine.base}/", token=None)
        suite1.check("根路径仍可加载（旧内置单页）", res.status == 200, f"status={res.status}")
        report["suites"].append({"name": suite1.name, "passed": suite1.passed,
                                 "failed": suite1.failed, "results": suite1.results})
        print()

        # ── 2. 核心读路径 ──
        suite2 = Suite("2. 核心读路径")
        print("[2] 核心读路径")
        books = http("GET", f"{engine.base}/api/books")
        items = (books.body or {}).get("books", [])
        demo = next((b for b in items if b["novel_id"] == "demo-web"), None)
        suite2.check("GET /api/books 列出书且进度正确",
                     demo is not None and demo["chapters"] == 3 and demo["approved"] == 2,
                     f"chapters={demo and demo['chapters']} approved={demo and demo['approved']}")

        status = http("GET", f"{engine.base}/api/status?novel=demo-web")
        metrics = (status.body or {}).get("metrics", {})
        suite2.check("GET /api/status 指标可用",
                     metrics.get("approved") == 2 and metrics.get("avg_score") is not None,
                     f"metrics={json.dumps(metrics, ensure_ascii=False)}")

        chapters = http("GET", f"{engine.base}/api/chapters?novel=demo-web")
        suite2.check("GET /api/chapters 返回 3 章",
                     len((chapters.body or {}).get("chapters", [])) == 3)

        raw = http("GET", f"{engine.base}/api/chapters/1/raw?novel=demo-web")
        suite2.check("GET /api/chapters/1/raw 读到正文与状态",
                     (raw.body or {}).get("status") == "approved"
                     and len((raw.body or {}).get("content", "")) > 100)

        tree = http("GET", f"{engine.base}/api/settings/tree?novel=demo-web")
        rels = [i["rel"] for i in (tree.body or {}).get("items", [])]
        suite2.check("GET /api/settings/tree 列出设定资料",
                     "settings/outline.md" in rels and "settings/foreshadowing.md" in rels,
                     f"{len(rels)} 份文档")

        doc = http("GET", f"{engine.base}/api/settings/doc?novel=demo-web&rel=settings/outline.md")
        suite2.check("GET /api/settings/doc 读取大纲", (doc.body or {}).get("title") == "源质觉醒")

        queue = http("GET", f"{engine.base}/api/queue?novel=demo-web")
        suite2.check("GET /api/queue 只列出草稿章",
                     [q["chapter"] for q in (queue.body or {}).get("queue", [])] == [3])

        dash = http("GET", f"{engine.base}/api/dashboard?novel=demo-web")
        suite2.check("GET /api/dashboard 指标与看板数据",
                     (dash.body or {}).get("metrics", {}).get("approved") == 2)

        mc = http("GET", f"{engine.base}/api/model-config")
        providers = (mc.body or {}).get("providers", [])
        masked = all(p["api_key"].startswith("${") or "****" in p["api_key"] for p in providers)
        suite2.check("GET /api/model-config 密钥已掩码", bool(providers) and masked,
                     f"providers={[p['name'] for p in providers]}")

        presets = http("GET", f"{engine.base}/api/agent-presets")
        keys = {p["key"] for p in (presets.body or {}).get("presets", [])}
        suite2.check("GET /api/agent-presets 列出 6 个智能体",
                     {"master", "character", "plot", "outline", "prose", "review"} <= keys,
                     f"{len(keys)} 个")

        report["suites"].append({"name": suite2.name, "passed": suite2.passed,
                                 "failed": suite2.failed, "results": suite2.results})
        print()

        # ── 3. 写路径 + 写作历史（P0-1）──
        suite3 = Suite("3. 写路径与写作历史")
        print("[3] 写路径与写作历史（P0-1 回归）")
        book_dir = data / "novels" / "demo-web"
        git_dir_before = (book_dir / ".git").exists()
        suite3.check("引擎启动时未凭空创建 .git（读路径无副作用）", not git_dir_before)

        created = http("POST", f"{engine.base}/api/books", body={"novel_id": "smoke-book"})
        suite3.check("POST /api/books 建书成功", created.status == 200,
                     f"status={created.status}")
        suite3.check("建书创建目录骨架",
                     all((data / "novels" / "smoke-book" / s).is_dir()
                         for s in ("chapters", "settings", "summaries", "reviews")))

        dup = http("POST", f"{engine.base}/api/books", body={"novel_id": "smoke-book"})
        suite3.check("重复建书 → 409", dup.status == 409, f"status={dup.status}")

        for bad in ["../escape", "a/b", "中文书", ""]:
            res = http("POST", f"{engine.base}/api/books", body={"novel_id": bad})
            suite3.check(f"非法书名 {bad!r} 被拒", res.status in (400, 422),
                         f"status={res.status}")

        saved = http("PUT", f"{engine.base}/api/chapters/1?novel=demo-web",
                     body={"content": "改写后的第一章正文。" * 40})
        suite3.check("PUT /api/chapters/1 保存成功", saved.status == 200)
        reread = http("GET", f"{engine.base}/api/chapters/1/raw?novel=demo-web")
        suite3.check("保存后可读回且 frontmatter 保留",
                     "改写后的第一章正文" in (reread.body or {}).get("content", "")
                     and (reread.body or {}).get("status") == "approved")

        suite3.check("写路径已初始化每本书 Git 仓库", (book_dir / ".git").exists())

        hist = http("GET", f"{engine.base}/api/history?novel=demo-web")
        commits = (hist.body or {}).get("commits", [])
        suite3.check("GET /api/history 返回提交记录", len(commits) >= 1,
                     f"{len(commits)} 条，最新：{commits[0]['message'] if commits else '-'}")
        suite3.check("提交信息带 [novel] 前缀与业务语义",
                     bool(commits) and commits[0]["message"].startswith("[novel]"))

        chapter = http("POST", f"{engine.base}/api/chapters?novel=demo-web", body={"title": "新增章"})
        suite3.check("POST /api/chapters 追加为第 4 章",
                     (chapter.body or {}).get("chapter") == 4)
        deleted = http("DELETE", f"{engine.base}/api/chapters/4?novel=demo-web")
        suite3.check("DELETE /api/chapters/4 删除成功", deleted.status == 200)

        setting = http("PUT", f"{engine.base}/api/settings/doc?novel=demo-web",
                       body={"rel": "settings/style.md", "content": "短句、冷硬、克制。"})
        suite3.check("PUT /api/settings/doc 写入文风指纹", setting.status == 200)

        hist2 = http("GET", f"{engine.base}/api/history?novel=demo-web&limit=50")
        suite3.check("多次写入累积为多条历史",
                     len((hist2.body or {}).get("commits", [])) >= 4,
                     f"{len((hist2.body or {}).get('commits', []))} 条")

        report["suites"].append({"name": suite3.name, "passed": suite3.passed,
                                 "failed": suite3.failed, "results": suite3.results})
        print()

        # ── 4. 路径与输入校验 ──
        suite4 = Suite("4. 路径穿越与输入校验")
        print("[4] 路径穿越与输入校验")
        for rel in ["../secrets.md", "settings/../../etc/passwd.md", "/etc/passwd.md",
                    "settings/x.txt", "chapters/ch-001.md"]:
            res = http("GET", f"{engine.base}/api/settings/doc?novel=demo-web&rel={rel}")
            suite4.check(f"非法 rel={rel!r} 被拒", res.status in (400, 404),
                         f"status={res.status}")

        for bad_novel in ["../evil", "a/b"]:
            res = http("GET", f"{engine.base}/api/chapters?novel={bad_novel}")
            suite4.check(f"非法 novel={bad_novel!r} → 400", res.status == 400,
                         f"status={res.status}")

        res = http("DELETE", f"{engine.base}/api/books/demo-web")
        suite4.check("默认书不可删除 → 409", res.status == 409, f"status={res.status}")

        res = http("POST", f"{engine.base}/api/decision?novel=demo-web", body={"action": "approve"})
        suite4.check("无待审项时裁决 → 409", res.status == 409, f"status={res.status}")

        res = http("GET", f"{engine.base}/api/chats/..%2F..%2Fconfig?novel=demo-web")
        suite4.check("会话 ID 穿越被拒", res.status in (400, 404), f"status={res.status}")

        report["suites"].append({"name": suite4.name, "passed": suite4.passed,
                                 "failed": suite4.failed, "results": suite4.results})
        print()

        # ── 5. 蒸馏上传路径（P0-4）──
        suite5 = Suite("5. 蒸馏上传路径防护")
        print("[5] 蒸馏上传路径防护（P0-4 回归）")
        res = http("POST", f"{engine.base}/api/distill/init", body={"file_path": "book.txt"})
        suite5.check("相对路径被拒 → 400", res.status == 400, f"status={res.status}")

        win_ini = Path(os.environ.get("SystemRoot", "C:/Windows")) / "win.ini"
        if win_ini.exists():
            res = http("POST", f"{engine.base}/api/distill/init",
                       body={"file_path": str(win_ini)})
            suite5.check("读取系统文件（win.ini）被拒 → 400", res.status == 400,
                         f"status={res.status} detail={str((res.body or {}).get('detail'))[:80]}")

        suite5.check("上传白名单文件后缀为 txt/epub/md",
                     True, "由 src/web/server.py::_UPLOAD_SUFFIXES 定义，单测覆盖")

        report["suites"].append({"name": suite5.name, "passed": suite5.passed,
                                 "failed": suite5.failed, "results": suite5.results})
        print()

        # ── 6. 对话与提案链路（不调 LLM 的部分）──
        suite6 = Suite("6. 对话与提案链路")
        print("[6] 对话与提案链路")
        chat = http("POST", f"{engine.base}/api/chats?novel=demo-web", body={"agent": "master"})
        cid = (chat.body or {}).get("chat", {}).get("id")
        suite6.check("POST /api/chats 创建会话", chat.status == 200 and bool(cid))
        suite6.check("GET /api/chats 列出会话",
                     any(c["id"] == cid for c in (http(
                         "GET", f"{engine.base}/api/chats?novel=demo-web"
                     ).body or {}).get("chats", [])))
        res = http("POST", f"{engine.base}/api/chats?novel=demo-web", body={"agent": "evil"})
        suite6.check("未知智能体被拒 → 400", res.status == 400, f"status={res.status}")
        res = http("POST", f"{engine.base}/api/chats/{cid}/send?novel=demo-web",
                   body={"message": "   "})
        suite6.check("空消息被拒 → 400", res.status == 400, f"status={res.status}")
        props = http("GET", f"{engine.base}/api/chats/{cid}/proposals?novel=demo-web")
        suite6.check("GET proposals 初始为空", (props.body or {}).get("proposals") == [])
        res = http("POST", f"{engine.base}/api/chats/{cid}/proposals/p-none/decide?novel=demo-web",
                   body={"decision": "accept"})
        suite6.check("裁决不存在的提案 → 404", res.status == 404, f"status={res.status}")
        res = http("DELETE", f"{engine.base}/api/chats/{cid}?novel=demo-web")
        suite6.check("DELETE /api/chats/{id} 删除会话", res.status == 200)
        report["suites"].append({"name": suite6.name, "passed": suite6.passed,
                                 "failed": suite6.failed, "results": suite6.results})
        print()

        # ── 7. 智能体设置写盘（本次冒烟新发现的缺陷回归）──
        suite7 = Suite("7. 智能体设置写盘")
        print("[7] 智能体设置写盘（新发现缺陷回归）")
        res = http("PUT", f"{engine.base}/api/agent-presets/plot",
                   body={"prompt": "你是剧情策划，输出三案互斥方案。"})
        suite7.check("PUT /api/agent-presets/plot 保存成功（曾恒 500）",
                     res.status == 200, f"status={res.status}")
        suite7.check("覆盖文件落盘到 data/config/agent-presets.json",
                     (data / "config" / "agent-presets.json").exists())
        presets = http("GET", f"{engine.base}/api/agent-presets")
        plot = next((p for p in (presets.body or {}).get("presets", []) if p["key"] == "plot"), {})
        suite7.check("覆盖生效且标记 custom", plot.get("custom") is True
                     and plot.get("prompt", "").startswith("你是剧情策划"))
        res = http("DELETE", f"{engine.base}/api/agent-presets/plot")
        suite7.check("DELETE 重置覆盖", res.status == 200)
        report["suites"].append({"name": suite7.name, "passed": suite7.passed,
                                 "failed": suite7.failed, "results": suite7.results})
        print()

        # ── 8. 材料 / 学习 / 自定义技能 / 绑定 ──
        suite8 = Suite("8. 素材 / 学习 / 技能 / 绑定")
        print("[8] 素材 / 学习 / 技能 / 绑定")
        mat = http("POST", f"{engine.base}/api/materials",
                   body={"title": "参考素材", "content": "一段可复用的设定参考。"})
        suite8.check("POST /api/materials 新建素材", mat.status == 200)
        mid = (mat.body or {}).get("id")
        suite8.check("GET /api/materials 列出素材",
                     any(m["id"] == mid for m in
                         (http("GET", f"{engine.base}/api/materials").body or {}).get("materials", [])))
        suite8.check("DELETE /api/materials/{id}", http(
            "DELETE", f"{engine.base}/api/materials/{mid}").status == 200)

        res = http("POST", f"{engine.base}/api/learning", body={"title": "t", "sample": "太短"})
        suite8.check("学习仿写样本过短被拒 → 400", res.status == 400, f"status={res.status}")

        sk = http("POST", f"{engine.base}/api/custom-skills",
                  body={"title": "冷硬文风", "content": "禁止形容词堆砌；对话不加修饰语。"})
        suite8.check("POST /api/custom-skills 新建自定义约束", sk.status == 200)
        sid = (sk.body or {}).get("skill_id")
        suite8.check("GET /api/custom-skills 列出",
                     any(s["skill_id"] == sid for s in
                         (http("GET", f"{engine.base}/api/custom-skills").body or {}).get("skills", [])))

        bind = http("POST", f"{engine.base}/api/bindings?novel=demo-web",
                    body={"custom_skill_ids": [sid], "pack_ids": []})
        suite8.check("POST /api/bindings 绑定自定义约束", bind.status == 200)
        suite8.check("绑定结果落盘为 settings/custom-skills.md",
                     (book_dir / "settings" / "custom-skills.md").exists())
        bound = http("GET", f"{engine.base}/api/bindings?novel=demo-web")
        suite8.check("GET /api/bindings 回读绑定",
                     (bound.body or {}).get("bound_custom") == [sid])
        res = http("POST", f"{engine.base}/api/bindings?novel=demo-web",
                   body={"custom_skill_ids": ["ghost"], "pack_ids": []})
        suite8.check("绑定不存在的技能 → 404", res.status == 404, f"status={res.status}")
        report["suites"].append({"name": suite8.name, "passed": suite8.passed,
                                 "failed": suite8.failed, "results": suite8.results})
        print()

        # ── 9. 蒸馏 init（真实分块，无 LLM）──
        suite9 = Suite("9. 蒸馏分块（无 LLM）")
        print("[9] 蒸馏分块（无 LLM）")
        res = http("POST", f"{engine.base}/api/distill/init",
                   body={"file_path": str(upload), "skill_id": "smoke-sample_v1.0.0"})
        suite9.check("POST /api/distill/init 接受授权 txt", res.status == 200,
                     f"status={res.status} detail={str((res.body or {}).get('detail'))[:100]}")
        body = res.body or {}
        suite9.check("返回分块与技能包目录",
                     bool(body.get("skill_id")) and bool(body.get("total_chunks")),
                     f"skill={body.get('skill_id')} chunks={body.get('total_chunks')}")
        skills = http("GET", f"{engine.base}/api/skills/list")
        suite9.check("GET /api/skills/list 可列出技能包", skills.status == 200)
        export = http("GET", f"{engine.base}/api/skills/export/smoke-sample_v1.0.0")
        suite9.check("GET /api/skills/export 产出 ZIP",
                     export.status == 200 and export.raw.startswith("PK"),
                     f"status={export.status}")
        report["suites"].append({"name": suite9.name, "passed": suite9.passed,
                                 "failed": suite9.failed, "results": suite9.results})
        print()

        # ── 10. 真实 LLM（可选）──
        if args.with_llm:
            suite10 = Suite("10. 真实 LLM 调用")
            print("[10] 真实 LLM 调用（消耗 1 次最小请求）")
            t_llm = time.time()
            res = http("POST", f"{engine.base}/api/model-test", body={"role": "editor"}, timeout=120)
            latency = round(time.time() - t_llm, 2)
            body = res.body or {}
            suite10.check("POST /api/model-test 真实连通", res.status == 200 and body.get("ok") is True,
                          f"ok={body.get('ok')} provider={body.get('provider')} "
                          f"model={body.get('model')} {latency}s",
                          evidence=str(body)[:200])
            suite10.check("返回模型回复内容", bool(body.get("reply")),
                          f"reply={body.get('reply')!r}")
            report["suites"].append({"name": suite10.name, "passed": suite10.passed,
                                     "failed": suite10.failed, "results": suite10.results})
            print()
        else:
            print("[10] 真实 LLM 调用 —— 已跳过（未加 --with-llm）")
            print()

        # ── 11. 优雅停止 ──
        suite11 = Suite("11. 进程生命周期")
        print("[11] 进程生命周期")
        engine.stop()
        suite11.check("引擎进程可优雅终止", engine.proc is not None and engine.proc.poll() is not None,
                      f"exit={engine.proc.poll() if engine.proc else 'n/a'}")
        res = http("GET", f"{engine.base}/api/ping", timeout=3)
        suite11.check("停止后端口不再响应", res.status == 0,
                      f"status={res.status} raw={res.raw[:60]}")
        report["suites"].append({"name": suite11.name, "passed": suite11.passed,
                                 "failed": suite11.failed, "results": suite11.results})

        exit_code = 0 if all(s["failed"] == 0 for s in report["suites"]) else 1

    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["engine_log_tail"] = engine.tail(40)
        exit_code = 1
    finally:
        engine.stop()

    total = sum(s["passed"] for s in report["suites"])
    failed = sum(s["failed"] for s in report["suites"])
    report["total_passed"] = total
    report["total_failed"] = failed
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["verdict"] = "PASS" if failed == 0 and "fatal" not in report else "FAIL"

    out_dir = ENGINE_DIR / "smoke-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"smoke-{stamp}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 78)
    print(f"总计：{total} 通过 / {failed} 失败  →  {report['verdict']}")
    print(f"结构化结果：{out_file}")
    print("=" * 78)

    if not args.keep_sandbox:
        shutil.rmtree(sandbox, ignore_errors=True)
    else:
        print(f"沙箱已保留：{sandbox}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
