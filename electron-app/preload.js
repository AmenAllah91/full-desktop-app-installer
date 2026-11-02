const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('yoclubPoste', {
    getState: () => ipcRenderer.invoke('poste-update:get'),
    checkNow: () => ipcRenderer.invoke('poste-update:check'),
    install: () => ipcRenderer.invoke('poste-update:install'),
    onState: (callback) => {
        const listener = (_event, state) => callback(state);
        ipcRenderer.on('poste-update-state', listener);
        return () => ipcRenderer.removeListener('poste-update-state', listener);
    },
});
