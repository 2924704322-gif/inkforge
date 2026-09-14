import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import path from 'node:path'

import { EngineSupervisor } from './engine-supervisor'

let mainWindow: BrowserWindow | null = null
let supervisor: EngineSupervisor | null = null
let quitting = false

const ALLOWED_METHODS = new Set(['GET', 'POST', 'PUT', 'DELETE'])
/** 跨 IPC 请求体上限（8 MB）：防止渲染层被注入后打爆内存/引擎。 */
const MAX_BODY_BYTES = 8 * 1024 * 1024
/** shell.openExternal 允许的协议白名单（S1-4）。 */
const ALLOWED_EXTERNAL_PROTOCOLS = new Set(['http:', 'https:'])

function resolveEngineDir(): string {
  if (app.isPackaged) return path.join(process.resourcesPath, 'engine')
  // dev: <repo>/apps/desktop → <repo>/engine
  return path.resolve(app.getAppPath(), '..', '..', 'engine')
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 1080,
    minHeight: 700,
    show: false,
    title: 'Inkforge',
    backgroundColor: '#ffffff',
    autoHideMenuBar: false,
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  mainWindow.maximize()
  mainWindow.on('ready-to-show', () => mainWindow?.show())
  mainWindow.on('closed', () => {
    mainWindow = null
  })
  mainWindow.webContents.setWindowOpenHandler((details) => {
    // 仅放行 http/https；javascript: / file: / 自定义协议一律拒绝（S1-4）
    try {
      const protocol = new URL(details.url).protocol
      if (ALLOWED_EXTERNAL_PROTOCOLS.has(protocol)) {
        void shell.openExternal(details.url)
      } else {
        console.warn(`[inkforge] 已拦截非白名单协议的外部打开：${details.url}`)
      }
    } catch {
      console.warn(`[inkforge] 非法外部 URL：${details.url}`)
    }
    return { action: 'deny' }
  })

  const devUrl = process.env['ELECTRON_RENDERER_URL']
  if (devUrl) {
    void mainWindow.loadURL(devUrl)
  } else {
    void mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'))
  }
}

/**
 * 校验来自渲染层的 API 路径（S1-4 加固）：
 * 前缀必须为 /api/，且 URL 解码后不得出现路径穿越序列。
 */
function validateApiPath(rawPath: string): { ok: true; value: string } | { ok: false; reason: string } {
  if (!rawPath.startsWith('/api/')) return { ok: false, reason: '非法 API 路径' }
  let decoded = rawPath
  try {
    decoded = decodeURIComponent(rawPath)
  } catch {
    return { ok: false, reason: '路径编码非法' }
  }
  if (decoded.includes('..') || decoded.includes('\\') || /[\u0000-\u001f]/.test(decoded)) {
    return { ok: false, reason: '非法 API 路径' }
  }
  return { ok: true, value: rawPath }
}

function bodySizeOf(body: unknown): number {
  if (body === undefined) return 0
  try {
    return Buffer.byteLength(JSON.stringify(body), 'utf8')
  } catch {
    return Number.MAX_SAFE_INTEGER
  }
}

/**
 * 蒸馏输入路径授权校验（S1-4）：`file_path` 必须来自用户经系统对话框选择的文件，
 * 否则拒绝转发——从源头掐断「任意本机文件被读入技能包后导出外带」的链路。
 */
function authorizeUploadPath(apiPath: string, body: unknown): string | null {
  if (!apiPath.startsWith('/api/distill/init')) return null
  const raw = (body as { file_path?: unknown } | null | undefined)?.file_path
  const filePath = typeof raw === 'string' ? raw : ''
  if (!filePath) return '缺少 file_path'
  if (!path.isAbsolute(filePath)) return 'file_path 必须是绝对路径'
  if (!supervisor || !supervisor.isGranted(filePath)) {
    return '该文件未获授权：请使用界面中的「选择文件」按钮选取'
  }
  return null
}

function isSafeId(id: string): boolean {
  return (
    id.length > 0 &&
    !id.includes('/') &&
    !id.includes('\\') &&
    !id.includes('..') &&
    !/[\u0000-\u001f]/.test(id)
  )
}

function registerIpc(sup: EngineSupervisor): void {
  ipcMain.handle(
    'engine:request',
    (_event, payload: { method?: string; path?: string; body?: unknown } | undefined) => {
      const method = (payload?.method ?? 'GET').toUpperCase()
      const rawPath = typeof payload?.path === 'string' ? payload.path : ''
      const checked = validateApiPath(rawPath)
      if (!checked.ok) {
        return Promise.resolve({ status: 400, data: { detail: checked.reason } })
      }
      if (!ALLOWED_METHODS.has(method)) {
        return Promise.resolve({ status: 405, data: { detail: '不支持的 HTTP 方法' } })
      }
      if (bodySizeOf(payload?.body) > MAX_BODY_BYTES) {
        return Promise.resolve({ status: 413, data: { detail: '请求体过大（上限 8 MB）' } })
      }
      const uploadError = authorizeUploadPath(rawPath, payload?.body)
      if (uploadError) {
        return Promise.resolve({ status: 403, data: { detail: uploadError } })
      }
      return sup.request(method, checked.value, payload?.body)
    },
  )

  ipcMain.handle('engine:state', () => ({
    state: sup.state,
    port: sup.port,
    message: sup.state,
  }))
  ipcMain.handle('engine:restart', () => sup.restart())
  ipcMain.handle('engine:logs', () => sup.recentLogs())

  ipcMain.handle(
    'export:manuscript',
    async (
      _event,
      payload: { novelId?: string; format?: string; includeDraft?: boolean } | undefined,
    ) => {
      const novelId = String(payload?.novelId ?? '')
      const format = payload?.format === 'epub' ? 'epub' : 'txt'
      const includeDraft = payload?.includeDraft === true
      if (!isSafeId(novelId)) {
        return { ok: false, message: '非法书目标识' }
      }
      if (!mainWindow) return { ok: false, message: '窗口不可用' }
      const res = await dialog.showSaveDialog(mainWindow, {
        title: '导出文稿',
        defaultPath: `${novelId}.${format}`,
        filters:
          format === 'epub'
            ? [{ name: 'EPUB 电子书', extensions: ['epub'] }]
            : [{ name: '文本文档', extensions: ['txt'] }],
      })
      if (res.canceled || !res.filePath) return { ok: false, message: '已取消' }
      return sup.downloadToFile(
        `/api/export?novel=${encodeURIComponent(novelId)}&format=${format}${
          includeDraft ? '&include_unapproved=true' : ''
        }`,
        res.filePath,
      )
    },
  )

  ipcMain.handle('app:info', () => {
    const engineDir = resolveEngineDir()
    return {
      versions: { ...process.versions } as Record<string, string>,
      engineDir,
      engineDataDir: path.join(engineDir, 'data'),
      python: sup.pythonPath,
      enginePort: sup.port,
    }
  })

  ipcMain.handle('dialog:pickBookFile', async () => {
    if (!mainWindow) return null
    const res = await dialog.showOpenDialog(mainWindow, {
      title: '选择用于蒸馏的书籍文件',
      properties: ['openFile'],
      filters: [
        { name: '书籍文件', extensions: ['txt', 'epub', 'md'] },
        { name: '全部文件', extensions: ['*'] },
      ],
    })
    if (res.canceled || res.filePaths.length === 0) return null
    const picked = res.filePaths[0] ?? null
    // 记录用户授权：只有这里登记过的路径才允许传给 /api/distill/init
    if (picked) sup.grantPath(picked)
    return picked
  })

  ipcMain.handle(
    'export:skillZip',
    async (_event, payload: { skillId?: string } | undefined) => {
      const skillId = String(payload?.skillId ?? '')
      if (!isSafeId(skillId)) {
        return { ok: false, message: '非法技能包 ID' }
      }
      if (!mainWindow) return { ok: false, message: '窗口不可用' }
      const res = await dialog.showSaveDialog(mainWindow, {
        title: '导出技能包',
        defaultPath: `${skillId}.zip`,
        filters: [{ name: 'ZIP 压缩包', extensions: ['zip'] }],
      })
      if (res.canceled || !res.filePath) return { ok: false, message: '已取消' }
      return sup.downloadToFile(`/api/skills/export/${encodeURIComponent(skillId)}`, res.filePath)
    },
  )
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
  })

  app.whenReady().then(() => {
    const sup = new EngineSupervisor(resolveEngineDir())
    supervisor = sup
    sup.on('state', (event) => {
      mainWindow?.webContents.send('engine:state', event)
    })
    registerIpc(sup)
    createWindow()
    void sup.start().catch(() => undefined)

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  app.on('before-quit', (event) => {
    if (quitting || supervisor === null) return
    quitting = true
    event.preventDefault()
    void supervisor
      .stop()
      .catch(() => undefined)
      .then(() => app.quit())
  })

  app.on('window-all-closed', () => {
    app.quit()
  })
}
