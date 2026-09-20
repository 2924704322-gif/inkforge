import { spawn, type ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import path from 'node:path'

import type { EngineState, EngineStateEvent } from '../shared/bridge'

/**
 * Python 引擎（novel_agent2.1 蒸馏版）监督器。
 *
 * 负责引擎子进程的启动、健康检查、自动重启与受代理的本地 HTTP 访问。
 * 引擎是 sidecar：Renderer 不感知进程与端口，一切经 Main 转发。
 *
 * 安全（S1-4）：
 * - 每次启动生成一次性随机 token，经环境变量注入子进程；引擎侧中间件校验
 *   `Authorization: Bearer <token>`。token 只存在于 Main 与子进程内存，
 *   渲染层无法读取 → 本机其他进程/浏览器页面无法访问引擎 API。
 * - 端口由内核分配（`--port 0`），子进程经 stdout 结构化标记回报，
 *   消除「探测空闲端口 → 释放 → 再绑定」的 TOCTOU 竞态。
 * - 健康检查打 `/api/ping`（无副作用），不再打 `/api/books`
 *   （后者会为每本书构造 MdStore 并遍历章节）。
 */
export class EngineSupervisor extends EventEmitter {
  state: EngineState = 'stopped'
  port = 0

  private proc: ChildProcess | null = null
  private stopping = false
  private restartAttempts = 0
  private readonly logs: string[] = []
  private token = ''

  /** 用户经系统对话框授权过的本地路径（仅用于蒸馏输入，S1-4）。 */
  private readonly grantedPaths = new Set<string>()

  constructor(private readonly engineDir: string) {
    super()
  }

  get pythonPath(): string {
    return process.env['INKFORGE_PYTHON'] || 'python'
  }

  get baseUrl(): string {
    return `http://127.0.0.1:${this.port}`
  }

  /** 引擎子进程 pid（0 = 未运行）。主进程退出时用它兜底回收整棵进程树。 */
  get pid(): number {
    return this.proc?.pid ?? 0
  }

  /** 记录一个由用户授权（系统对话框）的绝对路径。 */
  grantPath(absolutePath: string): void {
    try {
      this.grantedPaths.add(fs.realpathSync(absolutePath))
    } catch {
      this.grantedPaths.add(absolutePath)
    }
  }

  /** 该路径是否由用户显式授权过。 */
  isGranted(absolutePath: string): boolean {
    const resolved = (() => {
      try {
        return fs.realpathSync(absolutePath)
      } catch {
        return absolutePath
      }
    })()
    return this.grantedPaths.has(resolved)
  }

  log(line: string): void {
    const stamped = `[engine ${new Date().toLocaleTimeString()}] ${line}`
    this.logs.push(stamped)
    if (this.logs.length > 800) this.logs.shift()
    console.log(stamped)
    this.appendToFile(stamped)
  }

  /**
   * 日志落盘（`engine/data/runtime/logs/desktop-<yyyyMMdd>.log`）。
   *
   * 由来：日志原先只存在内存环形缓冲里，一旦用户重启应用现场就没了——排"互动创作
   * 跑一半报连接错误"这类问题时，手上一点证据都没有。写盘失败只告警，绝不影响主流程。
   */
  private appendToFile(line: string): void {
    try {
      const dir = path.join(this.engineDir, 'data', 'runtime', 'logs')
      fs.mkdirSync(dir, { recursive: true })
      const day = new Date().toISOString().slice(0, 10).replace(/-/g, '')
      fs.appendFileSync(path.join(dir, `desktop-${day}.log`), `${line}\n`, 'utf8')
    } catch {
      /* 落盘失败不影响引擎运行 */
    }
  }

  recentLogs(): string[] {
    return [...this.logs]
  }

  async start(): Promise<void> {
    if (this.proc) return
    this.stopping = false
    this.state = 'starting'
    this.port = 0
    this.emitState('引擎启动中…')

    // 每次启动换发一次性 token
    this.token = randomBytes(32).toString('hex')

    const python = this.pythonPath
    const novelId = this.resolveDefaultNovel()
    const args = ['-m', 'src.web.server', '--novel-id', novelId, '--port', '0']
    this.log(`spawn: ${python} ${args.join(' ')}  (cwd=${this.engineDir})`)

    const uploadRoots = this.uploadRoots()
    const proc = spawn(python, args, {
      cwd: this.engineDir,
      windowsHide: true,
      env: {
        ...process.env,
        INKFORGE_ENGINE_TOKEN: this.token,
        ...(uploadRoots.length > 0 ? { INKFORGE_UPLOAD_ROOTS: uploadRoots.join(path.delimiter) } : {}),
        // 编码：**必须显式指定 UTF-8**。
        // 由来（2026-09-19 实测复现）：Python 在 Windows 下被 spawn 成管道子进程时，
        // sys.stdout.encoding 取的是 locale 编码（实测 `gbk`），于是中文日志以 **GBK 字节**写出；
        // 而 Node 的 `chunk.toString()` 默认按 **UTF-8** 解码这些字节 → 每个字节变成 U+FFFD，
        // 落进 desktop-<日期>.log 后**不可逆**（实测一条中文日志产生 8 个替换符）。
        // 后果：排障时日志中文全丢，只能靠 ASCII 骨架猜（上一轮排查就吃了这个亏）。
        // 这里让引擎侧直接吐 UTF-8，与 Node 的解码口径对齐。
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
    })
    this.proc = proc

    const portReady = this.capturePort(proc)
    this.pipeLogs(proc)

    proc.on('error', (err) => {
      // spawn 本身失败（如 py 解释器路径不存在 ENOENT）：必须落到 crashed，
      // 否则界面会永远停在「引擎启动中…」而没有任何解释。
      this.log(`进程错误: ${err.message}`)
      this.state = 'crashed'
      this.emitState(`引擎进程无法启动：${err.message}`)
    })
    proc.on('exit', (code) => {
      this.proc = null
      this.token = ''
      if (this.stopping) {
        this.state = 'stopped'
        this.emitState('引擎已停止')
        return
      }
      this.log(`引擎进程异常退出（code=${code ?? 'null'}）`)
      this.state = 'crashed'
      this.emitState('引擎异常退出')
      if (this.restartAttempts < 3) {
        this.restartAttempts += 1
        const delay = 1500 * this.restartAttempts
        this.log(`${delay}ms 后自动重启（第 ${this.restartAttempts}/3 次）`)
        setTimeout(() => {
          void this.start().catch(() => undefined)
        }, delay)
      }
    })

    try {
      await portReady
      if (this.port === 0) throw new Error('引擎未回报监听端口')
      await this.waitReady(180_000)
      this.state = 'ready'
      this.restartAttempts = 0
      this.emitState(`引擎就绪 :${this.port}`)
    } catch (err) {
      this.state = 'crashed'
      const message = err instanceof Error ? err.message : String(err)
      this.emitState(`引擎启动失败：${message}`)
      throw err
    }
  }

  /** 从子进程 stdout 中解析 `INKFORGE_ENGINE_PORT=<n>`。 */
  private capturePort(proc: ChildProcess): Promise<void> {
    return new Promise((resolve, reject) => {
      if (!proc.stdout) {
        resolve()
        return
      }
      const onData = (chunk: Buffer): void => {
        const text = chunk.toString()
        const m = /INKFORGE_ENGINE_PORT=(\d+)/.exec(text)
        if (m) {
          this.port = Number(m[1])
          proc.stdout?.off('data', onData)
          resolve()
        }
      }
      proc.stdout.on('data', onData)
      proc.once('exit', () => reject(new Error('引擎进程在回报端口前退出')))
      proc.once('error', (err) => reject(err))
    })
  }

  /** 把子进程 stdout/stderr 逐行转发到日志缓冲。 */
  private pipeLogs(proc: ChildProcess): void {
    const attach = (stream: NodeJS.ReadableStream | null, prefix: string): void => {
      stream?.on('data', (chunk: Buffer) => {
        for (const line of chunk.toString().split('\n')) {
          const trimmed = line.trim()
          if (!trimmed) continue
          // 端口标记是协议输出，不作为日志噪音
          if (trimmed.startsWith('INKFORGE_ENGINE_PORT=')) continue
          this.log(`${prefix}${trimmed}`)
        }
      })
    }
    attach(proc.stdout, '')
    attach(proc.stderr, '[stderr] ')
  }

  /** 蒸馏上传授权根目录：用户主目录（对话框选取的路径必在其内）。 */
  private uploadRoots(): string[] {
    const home = process.env['USERPROFILE'] || process.env['HOME']
    return home ? [home] : []
  }

  private authHeaders(extra?: Record<string, string>): Record<string, string> {
    return { Authorization: `Bearer ${this.token}`, ...(extra ?? {}) }
  }

  /** 引擎 API 调用（仅 Main 进程内部使用；Renderer 的调用经 IPC 校验后转发至此）。 */
  async request(
    method: string,
    apiPath: string,
    body?: unknown,
  ): Promise<{ status: number; data: unknown }> {
    if (this.state !== 'ready' || this.proc === null) {
      return { status: 503, data: { detail: '引擎未就绪，请稍候或重启引擎' } }
    }
    return this.rawRequest(method, apiPath, body)
  }

  /** 不检查就绪状态的底层请求（健康检查在 starting 阶段也要能用）。 */
  private async rawRequest(
    method: string,
    apiPath: string,
    body?: unknown,
  ): Promise<{ status: number; data: unknown }> {
    try {
      const res = await fetch(`${this.baseUrl}${apiPath}`, {
        method,
        headers: this.authHeaders(
          body === undefined ? undefined : { 'Content-Type': 'application/json' },
        ),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(this.timeoutMs(apiPath)),
      })
      const text = await res.text()
      let data: unknown = null
      if (text) {
        try {
          data = JSON.parse(text)
        } catch {
          data = { raw: text }
        }
      }
      return { status: res.status, data }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      // 超时要和"连接被重置/引擎已退出"分开报：两者的下一步动作完全不同
      // （前者是模型慢、等一等；后者是引擎进程没了、要重启）。原先统一成
      // `引擎通信失败：<原始英文>`，用户看不懂也判断不了。
      const timedOut = /timeout|timed out|aborted/i.test(message)
      const detail = timedOut
        ? `引擎请求超时（${Math.round(this.timeoutMs(apiPath) / 1000)}s 未返回，路径 ${apiPath}）。`
          + '若正在进行长章节生成，稍后重试即可；持续如此请查看引擎日志。'
        : `引擎通信失败（${message}）。引擎进程可能已退出或正在重启：`
          + '可在「工具」里点「重启引擎」，或重新启动 Inkforge。'
      this.log(`请求失败 ${method} ${apiPath}: ${message}`)
      return { status: 502, data: { detail, raw: message } }
    }
  }

  /**
   * 按路径选择超时：长文生成类请求可能跑几分钟，健康检查/读接口必须短，
   * 否则 UI 会把「引擎卡住」和「模型慢」混为一谈。
   */
  private timeoutMs(apiPath: string): number {
    if (apiPath === '/api/ping') return 5_000
    const slow = /^\/api\/(interactive|converse|chats|demo|start|resume|proposals|distill)/.test(
      apiPath,
    )
    return slow ? 1_800_000 : 60_000
  }

  /** 流式下载（技能包 ZIP / 文稿导出），落盘到用户选择的路径。 */
  async downloadToFile(
    apiPath: string,
    savePath: string,
  ): Promise<{ ok: boolean; message: string }> {
    if (this.state !== 'ready' || this.proc === null) {
      return { ok: false, message: '引擎未就绪' }
    }
    try {
      const res = await fetch(`${this.baseUrl}${apiPath}`, {
        headers: this.authHeaders(),
        signal: AbortSignal.timeout(600_000),
      })
      if (!res.ok) return { ok: false, message: `导出失败（HTTP ${res.status}）` }
      const buf = Buffer.from(await res.arrayBuffer())
      await fs.promises.writeFile(savePath, buf)
      this.log(`已导出 ${savePath}（${(buf.length / 1024 / 1024).toFixed(2)} MB）`)
      return { ok: true, message: savePath }
    } catch (err) {
      return { ok: false, message: err instanceof Error ? err.message : String(err) }
    }
  }

  async restart(): Promise<EngineStateEvent> {
    this.restartAttempts = 0
    await this.stop()
    await this.start()
    return { state: this.state, port: this.port, message: '引擎已重启' }
  }

  async stop(): Promise<void> {
    this.stopping = true
    const proc = this.proc
    if (!proc) return
    this.log('正在停止引擎…')
    // 先取 pid：子进程一退出，proc.pid 仍可读，但为稳妥起见固定下来给 taskkill 用
    const killedPid = proc.pid
    await new Promise<void>((resolve) => {
      const forceKillTree = (): void => {
        if (process.platform !== 'win32' || !killedPid) return
        try {
          // python 可能还有 git 等子进程；taskkill /T 一起收掉，避免残留进程
          // 继续占用 checkpoints.sqlite / chroma 目录
          spawn('taskkill', ['/PID', String(killedPid), '/T', '/F'], {
            windowsHide: true,
            stdio: 'ignore',
          })
        } catch {
          /* 已退出 */
        }
      }
      const timer = setTimeout(() => {
        try {
          proc.kill('SIGKILL')
        } catch {
          /* 已退出 */
        }
        forceKillTree()
        resolve()
      }, 3000)
      proc.once('exit', () => {
        clearTimeout(timer)
        resolve()
      })
      try {
        proc.kill()
      } catch {
        clearTimeout(timer)
        resolve()
      }
    })
    this.proc = null
    this.token = ''
    this.port = 0
    this.state = 'stopped'
    this.emitState('引擎已停止')
  }

  private resolveDefaultNovel(): string {
    const novelsDir = path.join(this.engineDir, 'data', 'novels')
    try {
      const dirs = fs
        .readdirSync(novelsDir, { withFileTypes: true })
        .filter((d) => d.isDirectory())
        .map((d) => d.name)
      if (dirs.includes('demo-web')) return 'demo-web'
      if (dirs.length > 0) return dirs[0]!
    } catch {
      /* 目录不存在时回落 demo-web */
    }
    return 'demo-web'
  }

  private waitReady(timeoutMs: number): Promise<void> {
    const deadline = Date.now() + timeoutMs
    return new Promise((resolve, reject) => {
      const tick = (): void => {
        if (Date.now() > deadline) {
          reject(new Error('健康检查超时（180s）'))
          return
        }
        if (this.proc === null) {
          reject(new Error('引擎进程在启动期间退出（请检查 INKFORGE_PYTHON 与依赖）'))
          return
        }
        void this.rawRequest('GET', '/api/ping')
          .then((res) => {
            if (res.status === 200) {
              resolve()
              return
            }
            if (res.status === 401) {
              reject(new Error('引擎拒绝了本次启动的访问令牌（token 握手失败）'))
              return
            }
            setTimeout(tick, 700)
          })
          .catch(() => {
            setTimeout(tick, 700)
          })
      }
      tick()
    })
  }

  private emitState(message: string): void {
    const event: EngineStateEvent = { state: this.state, port: this.port, message }
    this.emit('state', event)
  }
}
