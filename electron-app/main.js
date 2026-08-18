const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn, exec } = require('child_process');

const userDataPath = app.getPath('userData');
const CONFIG_PATH = path.join(userDataPath, 'config.json');

// ---------------------------------------------------------------------------
// Supervision du pont Python
//
// Historique : le process était lancé une fois et jamais surveillé. Ses
// événements 'close' et 'error' se contentaient d'écrire dans la console.
// Quand il mourait — saturation mémoire, DNS non résolu au démarrage du PC,
// exception fatale — plus rien ne fonctionnait et personne n'était prévenu :
// les clients redémarraient l'application à la main jusqu'à 20 fois par jour.
// ---------------------------------------------------------------------------
const BRIDGE_EXE_NAME = 'pythonApp.exe';
const HEALTH_URL = 'http://127.0.0.1:9998/health';
const HEALTH_INTERVAL_MS = 10000;
const HEALTH_TIMEOUT_MS = 5000;
// Un pont qui vient de démarrer met quelques secondes à ouvrir son port : on ne
// le déclare en panne qu'après plusieurs échecs consécutifs.
const HEALTH_FAILURES_BEFORE_RESTART = 3;
const HEALTH_GRACE_MS = 30000;

const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;

// Garde-fou anti-boucle : si le pont meurt en rafale, c'est un problème
// d'installation (DLL manquante, .NET absent…) qu'un redémarrage ne règlera pas.
const CRASH_WINDOW_MS = 10 * 60 * 1000;
const CRASH_LIMIT = 10;

// Repli si le .env est absent ou muet — voir readBaseUrl().
const DEFAULT_BASE_URL = 'https://app.yogym.co';

let mainWindow = null;
let bridge = null;
let bridgeConfig = null;
let shuttingDown = false;
let restartTimer = null;
let healthTimer = null;
let backoffMs = BACKOFF_MIN_MS;
let healthFailures = 0;
let lastSpawnAt = 0;
let recentCrashes = [];
let fatalNotified = false;
let bridgeState = { state: 'stopped', detail: '', since: Date.now() };
let baseUrl = DEFAULT_BASE_URL;

function log(...args) {
    console.log('[SUPERVISOR]', ...args);
}

function setBridgeState(state, detail = '') {
    bridgeState = { state, detail, since: Date.now() };
    log(`état=${state}${detail ? ' — ' + detail : ''}`);

    // Le front est une page distante : on ne peut pas lui parler en IPC. On
    // dépose donc l'état sur window et on émet un événement, que l'application
    // Angular peut écouter pour afficher un bandeau.
    if (mainWindow && !mainWindow.isDestroyed()) {
        const payload = JSON.stringify(bridgeState);
        mainWindow.webContents
            .executeJavaScript(
                `window.__yogymBridge = ${payload};` +
                `window.dispatchEvent(new CustomEvent('yogym-bridge-status', ` +
                `{ detail: window.__yogymBridge }));`
            )
            .catch(() => {});
    }
}

// Racine de la plateforme, lue dans le MÊME .env que le pont Python. L'URL était
// codée en dur ici, alors que Python avait ses propres valeurs par défaut
// dispersées : basculer le poste vers integration.yogym.co demandait d'éditer
// quatre endroits dans trois langages. Une seule ligne suffit désormais.
function resolveEnvFile() {
    const candidates = [
        path.join(path.dirname(process.execPath), '.env'),
        path.join(process.cwd(), '.env'),
        path.join(__dirname, '.env'),
    ];
    return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

function readBaseUrl() {
    const envPath = resolveEnvFile();
    if (!envPath) {
        log(`.env introuvable, URL par défaut : ${DEFAULT_BASE_URL}`);
        return DEFAULT_BASE_URL;
    }

    try {
        const line = fs
            .readFileSync(envPath, 'utf-8')
            .split(/\r?\n/)
            .map((l) => l.trim())
            .filter((l) => l && !l.startsWith('#'))
            .find((l) => l.startsWith('YOGYM_BASE_URL='));

        const value = line ? line.slice('YOGYM_BASE_URL='.length).trim().replace(/\/+$/, '') : '';
        if (!value) {
            log(`YOGYM_BASE_URL absent de ${envPath}, URL par défaut : ${DEFAULT_BASE_URL}`);
            return DEFAULT_BASE_URL;
        }

        log(`URL de la plateforme : ${value} (source : ${envPath})`);
        return value;
    } catch (err) {
        console.error('[SUPERVISOR] Lecture du .env impossible :', err.message);
        return DEFAULT_BASE_URL;
    }
}

function resolveBridgeExe() {
    // process.execPath = YoGym.exe une fois installé ; pythonApp.exe est déposé
    // à côté par l'installeur. L'ancien spawn('pythonApp.exe') dépendait du
    // répertoire courant du raccourci, donc échouait silencieusement s'il
    // pointait ailleurs.
    const candidates = [
        path.join(path.dirname(process.execPath), BRIDGE_EXE_NAME),
        path.join(process.cwd(), BRIDGE_EXE_NAME),
        path.join(__dirname, BRIDGE_EXE_NAME),
    ];
    for (const candidate of candidates) {
        if (fs.existsSync(candidate)) return candidate;
    }
    return null;
}

function killBridge() {
    return new Promise((resolve) => {
        if (!bridge || !bridge.pid) return resolve();

        const pid = bridge.pid;
        bridge = null;
        log(`arrêt du pont (PID=${pid})`);

        if (process.platform === 'win32') {
            // Uniquement par arbre de PID. L'ancien code faisait aussi
            // `taskkill /IM pythonApp.exe`, ce qui tuait les ponts des autres
            // instances installées sur la même machine.
            exec(`taskkill /PID ${pid} /T /F`, () => resolve());
        } else {
            try { process.kill(-pid, 'SIGTERM'); } catch (e) { /* déjà mort */ }
            resolve();
        }

        setTimeout(resolve, 5000);
    });
}

function noteCrash() {
    const now = Date.now();
    recentCrashes = recentCrashes.filter((t) => now - t < CRASH_WINDOW_MS);
    recentCrashes.push(now);
    return recentCrashes.length;
}

function scheduleRestart(reason) {
    if (shuttingDown || restartTimer) return;

    const crashes = noteCrash();
    if (crashes > CRASH_LIMIT) {
        setBridgeState('fatal', `${crashes} arrêts en 10 min — ${reason}`);
        if (!fatalNotified) {
            fatalNotified = true;
            dialog.showErrorBox(
                'YoGym — service biométrique instable',
                `Le service de communication avec les pointeuses s'est arrêté ` +
                `${crashes} fois en moins de 10 minutes.\n\nDernière cause : ${reason}\n\n` +
                `Les redémarrages continuent au ralenti. Contactez le support en ` +
                `joignant le fichier journal :\n%APPDATA%\\desktop-app\\logs\\app.log`
            );
        }
        backoffMs = BACKOFF_MAX_MS * 2;
    }

    setBridgeState('restarting', `${reason} — nouvelle tentative dans ${Math.round(backoffMs / 1000)}s`);

    restartTimer = setTimeout(() => {
        restartTimer = null;
        startBridge();
    }, backoffMs);

    backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS);
}

function startBridge() {
    if (shuttingDown || bridge) return;

    const exePath = resolveBridgeExe();
    if (!exePath) {
        setBridgeState('fatal', `${BRIDGE_EXE_NAME} introuvable`);
        if (!fatalNotified) {
            fatalNotified = true;
            dialog.showErrorBox(
                'YoGym — installation incomplète',
                `${BRIDGE_EXE_NAME} est introuvable à côté de l'application.\n\n` +
                `Réinstallez YoGym pour rétablir la communication avec les pointeuses.`
            );
        }
        return;
    }

    const cwd = path.dirname(exePath);
    log(`démarrage : ${exePath} ${bridgeConfig.tenant} ${bridgeConfig.gymBranchId} (cwd=${cwd})`);

    lastSpawnAt = Date.now();
    healthFailures = 0;

    bridge = spawn(exePath, [bridgeConfig.tenant, bridgeConfig.gymBranchId], {
        cwd,
        windowsHide: true,
        detached: process.platform !== 'win32',
        stdio: ['ignore', 'pipe', 'pipe'],
    });

    setBridgeState('starting', `PID=${bridge.pid}`);

    bridge.stdout.on('data', (d) => console.log(`[PYTHON] ${d}`.trimEnd()));
    bridge.stderr.on('data', (d) => console.error(`[PYTHON ERROR] ${d}`.trimEnd()));

    bridge.on('close', (code, signal) => {
        bridge = null;
        const reason = `arrêt du pont (code=${code}${signal ? ', signal=' + signal : ''})`;
        log(reason);
        if (!shuttingDown) scheduleRestart(reason);
    });

    bridge.on('error', (err) => {
        bridge = null;
        const reason = `impossible de lancer le pont : ${err.message}`;
        console.error('[SUPERVISOR]', reason);
        if (!shuttingDown) scheduleRestart(reason);
    });
}

function checkHealth() {
    if (shuttingDown || !bridge) return;

    // Laisse au pont le temps d'ouvrir son port après un démarrage.
    if (Date.now() - lastSpawnAt < HEALTH_GRACE_MS) return;

    const req = http.get(HEALTH_URL, { timeout: HEALTH_TIMEOUT_MS }, (res) => {
        let body = '';
        res.on('data', (c) => (body += c));
        res.on('end', () => {
            let payload = null;
            try { payload = JSON.parse(body); } catch (e) { /* réponse illisible */ }

            if (res.statusCode === 200 && payload && payload.status === 'starting') {
                // Le pont répond mais attend encore sa configuration (réseau pas
                // prêt au démarrage du poste). On patiente : le tuer ici
                // l'empêcherait définitivement d'aboutir.
                healthFailures = 0;
                if (bridgeState.state !== 'starting') {
                    setBridgeState('starting', 'récupération de la configuration');
                }
            } else if (res.statusCode === 200 && payload && payload.status === 'ok') {
                healthFailures = 0;
                backoffMs = BACKOFF_MIN_MS;
                recentCrashes = [];
                if (bridgeState.state !== 'healthy') {
                    setBridgeState(
                        'healthy',
                        `${payload.machinesConnected}/${payload.machinesTotal} machine(s) connectée(s)`
                    );
                }
            } else {
                onHealthFailure(payload && payload.status === 'degraded'
                    ? `pont figé (watchdog muet depuis ${payload.watchdogStaleSeconds}s)`
                    : `réponse /health inattendue (HTTP ${res.statusCode})`);
            }
        });
    });

    req.on('timeout', () => { req.destroy(); onHealthFailure('/health ne répond pas'); });
    req.on('error', (err) => onHealthFailure(`/health injoignable : ${err.message}`));
}

async function onHealthFailure(reason) {
    if (shuttingDown || !bridge) return;

    healthFailures += 1;
    log(`sonde en échec ${healthFailures}/${HEALTH_FAILURES_BEFORE_RESTART} — ${reason}`);

    if (healthFailures < HEALTH_FAILURES_BEFORE_RESTART) {
        setBridgeState('unhealthy', reason);
        return;
    }

    // Le process est vivant mais ne répond plus : c'est le cas que personne ne
    // détectait, puisqu'aucun crash ne se produisait. On le tue, l'événement
    // 'close' déclenchera le redémarrage.
    log('pont vivant mais non fonctionnel — redémarrage forcé');
    setBridgeState('restarting', reason);
    await killBridge();
    scheduleRestart(reason);
}

function startHealthLoop() {
    if (healthTimer) return;
    healthTimer = setInterval(checkHealth, HEALTH_INTERVAL_MS);
}

// Identité du poste : les deux valeurs sont obligatoires et le club doit être
// numérique. Un tenant vide ou un club non numérique produirait un consumer group
// erroné et des pointages attribués au mauvais club.
function isValidConfig(config) {
    if (!config || typeof config !== 'object') return false;
    const tenant = String(config.tenant ?? '').trim();
    const branch = String(config.gymBranchId ?? '').trim();
    return tenant.length > 0 && /^\d+$/.test(branch);
}

function promptConfig(callback) {
    let submitted = false;
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
                <div id="err" style="color:#c0392b; font-size:13px; min-height:18px; margin-bottom:8px;"></div>
                <button onclick="submit()">Valider</button>
                <script>
                    const { ipcRenderer } = require('electron');
                    // Sans validation, un clic sur "Valider" a vide envoyait deux
                    // chaines vides : le pont basculait alors sur le .env, commun a
                    // tous les installeurs, et le poste tournait sous l'identite
                    // d'un autre club.
                    function submit() {
                        const tenant = document.getElementById('tenant').value.trim();
                        const gymBranchId = document.getElementById('branch').value.trim();
                        const err = document.getElementById('err');
                        if (!tenant) {
                            err.textContent = 'Le tenant est obligatoire.';
                            return;
                        }
                        if (!/^\\d+$/.test(gymBranchId)) {
                            err.textContent = "L'ID de la salle est obligatoire et doit etre un nombre.";
                            return;
                        }
                        err.textContent = '';
                        ipcRenderer.send('config-submitted', { tenant, gymBranchId });
                    }
                </script>
            </body>
        </html>
    `)
    );

    // Deuxieme barriere, cote processus principal : on ne fait confiance ni au
    // renderer ni a un config.json edite a la main.
    ipcMain.once('config-submitted', (event, data) => {
        if (!isValidConfig(data)) {
            log('configuration invalide reçue, saisie ignorée');
            return;
        }
        fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2));
        submitted = true;
        promptWin.close();
        callback(data);
    });

    // Fermer la fenetre sans valider laissait l'application en vie sans identite,
    // sans pont et sans message. On arrete franchement.
    promptWin.on('closed', () => {
        if (!submitted) {
            log('configuration abandonnée — arrêt de YoGym');
            app.exit(1);
        }
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
            window.__yogymBridge = ${JSON.stringify(bridgeState)};
            console.log('[INJECTION OK] sessionStorage now ready');
        `);
    });

    win.webContents.on('console-message', (event, level, message) => {
        console.log(`[ELECTRON CONSOLE] ${message}`);
    });

    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<html><body></body></html>'));

    setTimeout(() => {
        win.loadURL(baseUrl);
        win.show();
    }, 100);

    return win;
}

function launch(config) {
    bridgeConfig = config;
    baseUrl = readBaseUrl();
    startBridge();
    startHealthLoop();
    mainWindow = createWindow(config);

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

// Deux instances se disputeraient les ports 9998 et 8765, et chacune tuerait le
// pont de l'autre.
if (!app.requestSingleInstanceLock()) {
    app.quit();
} else {
    app.on('second-instance', () => {
        if (mainWindow) {
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.focus();
        }
    });

    app.whenReady().then(() => {
        // Le config.json existant n'était jamais vérifié : un fichier tronqué,
        // illisible ou contenant des valeurs vides lançait le pont avec une
        // identité invalide. On le valide, et on redemande la saisie sinon.
        let config = null;
        if (fs.existsSync(CONFIG_PATH)) {
            try {
                config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
            } catch (err) {
                log(`config.json illisible (${err.message}), nouvelle saisie demandée`);
                config = null;
            }
            if (config && !isValidConfig(config)) {
                log('config.json incomplet (tenant ou gymBranchId manquant), nouvelle saisie demandée');
                config = null;
            }
        }

        if (config) {
            log(`identité du poste : tenant=${config.tenant} club=${config.gymBranchId}`);
            launch(config);
        } else {
            promptConfig((valid) => launch(valid));
        }
    });
}

async function shutdown() {
    if (shuttingDown) return;
    shuttingDown = true;

    if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }

    await killBridge();
}

app.on('before-quit', (event) => {
    if (!bridge && !restartTimer && !healthTimer) return;
    event.preventDefault();
    shutdown().then(() => app.exit(0));
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});

['SIGTERM', 'SIGINT'].forEach((signal) => {
    process.on(signal, async () => {
        log(`signal ${signal} reçu`);
        await shutdown();
        process.exit(0);
    });
});
