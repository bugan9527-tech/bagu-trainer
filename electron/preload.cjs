const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('trainer', {
  request: (method, params = {}) => ipcRenderer.invoke('trainer:request', method, params),
  subscribe: (callback) => {
    const listener = (_event, message) => callback(message);
    ipcRenderer.on('trainer:event', listener);
    return () => ipcRenderer.removeListener('trainer:event', listener);
  },
});
