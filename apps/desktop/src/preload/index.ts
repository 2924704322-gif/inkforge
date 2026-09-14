import { contextBridge, ipcRenderer } from 'electron/renderer'

import type { BridgeApi, EngineStateEvent } from '../shared/bridge'

const bridge: BridgeApi = {
  request: (method, path, body) => ipcRenderer.invoke('engine:request', { method, path, body }),
  engineState: () => ipcRenderer.invoke('engine:state'),
  restartEngine: () => ipcRenderer.invoke('engine:restart'),
  engineLogs: () => ipcRenderer.invoke('engine:logs'),
  appInfo: () => ipcRenderer.invoke('app:info'),
  pickBookFile: () => ipcRenderer.invoke('dialog:pickBookFile'),
  exportSkillZip: (skillId) => ipcRenderer.invoke('export:skillZip', { skillId }),
  exportManuscript: (novelId, format, includeDraft) =>
    ipcRenderer.invoke('export:manuscript', { novelId, format, includeDraft }),
  onEngineState: (cb: (event: EngineStateEvent) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: EngineStateEvent): void => {
      cb(payload)
    }
    ipcRenderer.on('engine:state', listener)
    return () => {
      ipcRenderer.removeListener('engine:state', listener)
    }
  },
}

contextBridge.exposeInMainWorld('inkforge', bridge)
