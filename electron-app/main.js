const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn, exec } = require('child_process');

const userDataPath = app.getPath('userData');
const CONFIG_PATH = path.join(userDataPath, 'config.json');

let pythonProcess = null;
let processGroup = null; // Store process group ID for better cleanup

function killPythonProcess() {
    if (!pythonProcess || !pythonProcess.pid) return;

    console.log(`[ELECTRON] Killing Python process tree (PID=${pythonProcess.pid})...`);

    return new Promise((resolve) => {
        if (process.platform === 'win32') {
            // Windows: Kill by process tree AND by executable name
            const commands = [
                `taskkill /PID ${pythonProcess.pid} /T /F`,
                `taskkill /IM "pythonApp.exe" /F`,
                `taskkill /IM "python.exe" /F /FI "WINDOWTITLE eq pythonApp*"`
            ];

            let completed = 0;
            commands.forEach((cmd, index) => {
                exec(cmd, (err, stdout, stderr) => {
                    if (err && !err.message.includes('not found')) {
                        console.error(`[ELECTRON] Command ${index + 1} failed:`, err.message);
                    } else {
                        console.log(`[ELECTRON] Command ${index + 1} executed: ${cmd}`);
                    }
                    
                    completed++;
                    if (completed === commands.length) {
                        console.log('[ELECTRON] All Windows cleanup commands completed');
                        resolve();
                    }
                });
            });

        } else {
            // Linux/macOS: Use process group killing + backup methods
            const killCommands = [];
            
            // Method 1: Kill process group if available
            if (processGroup) {
                killCommands.push(`kill -TERM -${processGroup}`);
            }
            
            // Method 2: Kill by PID tree
            killCommands.push(`pkill -TERM -P ${pythonProcess.pid}`);
            killCommands.push(`kill -TERM ${pythonProcess.pid}`);
            
            // Method 3: Kill by executable name (backup)
            killCommands.push(`pkill -f "pythonApp"`);

            let completed = 0;
            killCommands.forEach((cmd, index) => {
                exec(cmd, (err, stdout, stderr) => {
                    if (err && !err.message.includes('No such process')) {
                        console.error(`[ELECTRON] Kill command ${index + 1} failed:`, err.message);
                    } else {
                        console.log(`[ELECTRON] Kill command ${index + 1} executed: ${cmd}`);
                    }
                    
                    completed++;
                    if (completed === killCommands.length) {
                        console.log('[ELECTRON] All Unix cleanup commands completed');
                        resolve();
                    }
                });
            });
        }

        // Cleanup references
        pythonProcess = null;
        processGroup = null;
        
        // Fallback timeout
        setTimeout(resolve, 3000);
    });
}


function spawnPythonProcess(config) {
    const spawnOptions = {
        detached: process.platform !== 'win32', 
        stdio: ['ignore', 'pipe', 'pipe']
    };

    if (process.platform === 'win32') {
        spawnOptions.windowsHide = true;
        spawnOptions.detached = false;
    }

    pythonProcess = spawn('pythonApp.exe', [config.tenant, config.gymBranchId], spawnOptions);
    if (process.platform !== 'win32' && pythonProcess.pid) {
        processGroup = pythonProcess.pid;
        console.log(`[ELECTRON] Python process group ID: ${processGroup}`);
    }

    pythonProcess.stdout.on('data', (data) => console.log(`[PYTHON] ${data}`));
    pythonProcess.stderr.on('data', (data) => console.error(`[PYTHON ERROR] ${data}`));
    
    pythonProcess.on('close', (code) => {
        console.log(`[PYTHON] Process exited with code ${code}`);
        pythonProcess = null;
        processGroup = null;
    });

    pythonProcess.on('error', (err) => {
        console.error('[PYTHON] Process error:', err);
        pythonProcess = null;
        processGroup = null;
    });

    return pythonProcess;
}

function promptConfig(callback) {
    const promptWin = new BrowserWindow({
        width: 400,
        height: 250,
        resizable: false,
        modal: true,
        show: true,
        icon: path.join(__dirname, 'resources', 'yogymlogo.ico'),
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false
        }
    });

    promptWin.loadURL(
        'data:text/html,' +
            encodeURIComponent(`
        <html>
            <body style="font-family: sans-serif; padding: 20px;">
                <h3>Configuration initiale</h3>
                <input id="tenant" placeholder="Identifiant du tenant" style="width:100%; margin-bottom:10px;" />
                <input id="branch" placeholder="ID de la salle (GymBranchId)" style="width:100%; margin-bottom:10px;" />
                <button onclick="submit()">Valider</button>
                <script>
                    const { ipcRenderer } = require('electron');
                    function submit() {
                        const tenant = document.getElementById('tenant').value.trim();
                        const gymBranchId = document.getElementById('branch').value.trim();
                        ipcRenderer.send('config-submitted', { tenant, gymBranchId });
                    }
                </script>
            </body>
        </html>
    `)
    );

    ipcMain.once('config-submitted', (event, data) => {
        fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2));
        promptWin.close();
        callback(data);
    });
}

function createWindow(config) {
    const win = new BrowserWindow({
        width: 1920,
        height: 1200,
        icon: path.join(__dirname, 'resources', 'yogymlogo.ico'),
        webPreferences: {
            contextIsolation: false,
            nodeIntegration: false,
            sandbox: false
        }
    });

    win.webContents.on('dom-ready', async () => {
        await win.webContents.executeJavaScript(`
            localStorage.setItem('realm', '${config.tenant}');
            localStorage.setItem('GYM_BRANCH_ID', '${config.gymBranchId}');
            localStorage.setItem('currentGymBranchId', '${config.gymBranchId}');
            localStorage.setItem('TENANT', '${config.tenant}');
            console.log('[INJECTION OK] sessionStorage now ready');
        `);
    });

    win.webContents.on('console-message', (event, level, message) => {
        console.log(`[ELECTRON CONSOLE] ${message}`);
    });

    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<html><body></body></html>'));

    setTimeout(() => {
        win.loadURL('https://app.yogym.co');
        win.show();
    }, 100);

    return win;
}

function launch(config) {
    spawnPythonProcess(config);
    const mainWindow = createWindow(config);

    mainWindow.on('closed', async () => {
        if (pythonProcess) {
            console.log('[ELECTRON] Window closed, killing Python processes...');
            await killPythonProcess();
        }
    });
}


app.whenReady().then(() => {
    if (fs.existsSync(CONFIG_PATH)) {
        const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
        launch(config);
    } else {
        promptConfig((config) => {
            launch(config);
        });
    }
});

app.on('before-quit', async (event) => {
    if (pythonProcess) {
        event.preventDefault(); 
        
        console.log('[ELECTRON] App quitting, cleaning up Python processes...');
        await killPythonProcess();
        
        setTimeout(() => {
            app.exit(0);
        }, 1000);
    }
});

// Quit app when all windows closed (except macOS behavior)
app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});

process.on('exit', () => {
    console.log('[ELECTRON] Process exiting, final cleanup...');
    if (pythonProcess && pythonProcess.pid) {
        try {
            if (process.platform === 'win32') {
                exec(`taskkill /IM "pythonApp.exe" /F`);
            } else {
                process.kill(-pythonProcess.pid, 'SIGKILL');
            }
        } catch (err) {
            console.error('[ELECTRON] Final cleanup error:', err);
        }
    }
});

['SIGTERM', 'SIGINT'].forEach(signal => {
    process.on(signal, async () => {
        console.log(`[ELECTRON] Received ${signal}, cleaning up...`);
        if (pythonProcess) {
            await killPythonProcess();
        }
        process.exit(0);
    });
});