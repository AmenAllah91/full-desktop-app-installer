const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { Readable, Transform } = require('stream');
const { pipeline } = require('stream/promises');

const VERSION = /^\d{1,3}\.\d{1,3}\.\d{1,3}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX_SIZE = 400 * 1024 * 1024;
const FIRST_CHECK_DELAY_MS = 30 * 1000;
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
const RETRY_DELAY_MS = 30 * 60 * 1000;
const KEPT_INSTALLERS = 2;
const INSTALLER_PREFIX = 'Yo-ClubInstaller-';
const REPORT_FILE = 'derniere-installation.json';
const SCRIPT_FILE = 'installer-mise-a-jour.ps1';

const INSTALL_SCRIPT = `param(
    [string]$Installer,
    [string]$App,
    [int]$ParentPid,
    [string]$Report,
    [string]$Version
)
$parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
if ($parent) {
    if (-not $parent.WaitForExit(60000)) {
        Stop-Process -Id $ParentPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}
$result = 'refused'
try {
    $process = Start-Process -FilePath $Installer -ArgumentList '/S' -Verb RunAs -Wait -PassThru
    $result = "$($process.ExitCode)"
} catch {
    $result = 'refused'
}
@{ version = $Version; result = $result; at = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -Path $Report -Encoding UTF8
Start-Process -FilePath $App
`;

function compareVersions(a, b) {
    const left = a.split('.').map(Number);
    const right = b.split('.').map(Number);
    for (let i = 0; i < 3; i++) {
        if (left[i] !== right[i]) return left[i] - right[i];
    }
    return 0;
}

function installerName(version) {
    return `${INSTALLER_PREFIX}${version}.exe`;
}

function validRelease(release, baseUrl, currentVersion) {
    if (!release || typeof release !== 'object') throw new Error('RELEASE_INVALID');
    const version = String(release.version ?? '');
    if (!VERSION.test(version) || compareVersions(version, currentVersion) <= 0) throw new Error('VERSION_INVALID');
    const sha256 = String(release.sha256 ?? '').toLowerCase();
    if (!SHA256.test(sha256)) throw new Error('SHA256_INVALID');
    const size = Number(release.size);
    if (!Number.isInteger(size) || size <= 0 || size > MAX_SIZE) throw new Error('SIZE_INVALID');
    let downloadUrl;
    try {
        downloadUrl = new URL(String(release.downloadUrl ?? ''));
    } catch {
        throw new Error('DOWNLOAD_URL_INVALID');
    }
    if (downloadUrl.protocol !== 'https:' || downloadUrl.host !== new URL(baseUrl).host) {
        throw new Error('DOWNLOAD_URL_INVALID');
    }
    return { version, sha256, size, notes: String(release.notes ?? ''), downloadUrl: downloadUrl.toString() };
}

async function sha256OfFile(file) {
    const hash = crypto.createHash('sha256');
    await pipeline(fs.createReadStream(file), hash);
    return hash.digest('hex');
}

function createUpdater({
    baseUrl,
    tenant,
    currentVersion,
    updatesDir,
    fetch,
    spawn,
    onState = () => {},
    log = () => {},
    setTimer = setTimeout,
    clearTimer = clearTimeout,
}) {
    let state = { status: 'idle', currentVersion };
    let ready = null;
    let busy = false;
    let timer = null;

    function setState(next) {
        state = { currentVersion, ...next };
        onState(state);
    }

    function schedule(delay) {
        if (timer) clearTimer(timer);
        timer = setTimer(() => {
            timer = null;
            check().finally(() => schedule(state.status === 'error' ? RETRY_DELAY_MS : CHECK_INTERVAL_MS));
        }, delay);
    }

    async function isVerified(file, release) {
        if (ready && ready.file === file && ready.sha256 === release.sha256) return fs.existsSync(file);
        if (!fs.existsSync(file) || fs.statSync(file).size !== release.size) return false;
        return (await sha256OfFile(file)) === release.sha256;
    }

    function cleanup(keep) {
        let entries;
        try {
            entries = fs.readdirSync(updatesDir);
        } catch {
            return;
        }
        for (const entry of entries.filter((name) => name.endsWith('.part'))) {
            fs.rmSync(path.join(updatesDir, entry), { force: true });
        }
        const installers = entries
            .filter((name) => name.startsWith(INSTALLER_PREFIX) && name.endsWith('.exe'))
            .map((name) => name.slice(INSTALLER_PREFIX.length, -4))
            .filter((version) => VERSION.test(version))
            .sort((a, b) => compareVersions(b, a));
        const kept = new Set(keep ? [path.basename(keep)] : []);
        for (const version of installers) {
            if (kept.size >= KEPT_INSTALLERS) break;
            kept.add(installerName(version));
        }
        for (const version of installers) {
            const name = installerName(version);
            if (!kept.has(name)) fs.rmSync(path.join(updatesDir, name), { force: true });
        }
    }

    async function download(release, target) {
        fs.mkdirSync(updatesDir, { recursive: true });
        const part = `${target}.part`;
        const response = await fetch(release.downloadUrl);
        if (!response.ok || !response.body) throw new Error(`DOWNLOAD_HTTP_${response.status}`);
        if (response.url && new URL(response.url).protocol !== 'https:') throw new Error('DOWNLOAD_NOT_HTTPS');
        const hash = crypto.createHash('sha256');
        let received = 0;
        let lastProgress = -1;
        setState({ status: 'downloading', version: release.version, notes: release.notes, progress: 0 });
        const meter = new Transform({
            transform(chunk, _encoding, callback) {
                received += chunk.length;
                if (received > release.size) return callback(new Error('SIZE_MISMATCH'));
                hash.update(chunk);
                const progress = Math.floor((received * 100) / release.size);
                if (progress !== lastProgress) {
                    lastProgress = progress;
                    setState({ status: 'downloading', version: release.version, notes: release.notes, progress });
                }
                callback(null, chunk);
            },
        });
        try {
            await pipeline(Readable.fromWeb(response.body), meter, fs.createWriteStream(part));
            if (received !== release.size) throw new Error('SIZE_MISMATCH');
            if (hash.digest('hex') !== release.sha256) throw new Error('CHECKSUM_MISMATCH');
        } catch (err) {
            fs.rmSync(part, { force: true });
            throw err;
        }
        fs.renameSync(part, target);
    }

    async function check() {
        if (busy || state.status === 'installing') return state;
        busy = true;
        try {
            const url = `${baseUrl}/tenant-management/api/public/desktop-releases/latest`
                + `?tenant=${encodeURIComponent(tenant)}&current=${encodeURIComponent(currentVersion)}`;
            const response = await fetch(url, { cache: 'no-store' });
            if (response.status === 204) {
                ready = null;
                cleanup(null);
                setState({ status: 'up-to-date', checkedAt: new Date().toISOString() });
                return state;
            }
            if (!response.ok) throw new Error(`CHECK_HTTP_${response.status}`);
            const release = validRelease(await response.json(), baseUrl, currentVersion);
            const target = path.join(updatesDir, installerName(release.version));
            if (!(await isVerified(target, release))) await download(release, target);
            ready = { file: target, sha256: release.sha256, version: release.version, notes: release.notes };
            cleanup(target);
            setState({ status: 'ready', version: release.version, notes: release.notes, checkedAt: new Date().toISOString() });
        } catch (err) {
            log(`vérification de mise à jour en échec : ${err.message}`);
            if (ready && fs.existsSync(ready.file)) {
                setState({ status: 'ready', version: ready.version, notes: ready.notes });
            } else {
                ready = null;
                setState({ status: 'error', error: err.message });
            }
        } finally {
            busy = false;
        }
        return state;
    }

    async function install({ appExe, pid }) {
        if (state.status !== 'ready' || !ready) return { started: false, error: 'NOT_READY' };
        if (!fs.existsSync(ready.file) || (await sha256OfFile(ready.file)) !== ready.sha256) {
            ready = null;
            setState({ status: 'error', error: 'CHECKSUM_MISMATCH' });
            schedule(0);
            return { started: false, error: 'CHECKSUM_MISMATCH' };
        }
        const script = path.join(updatesDir, SCRIPT_FILE);
        fs.writeFileSync(script, INSTALL_SCRIPT, 'utf-8');
        const child = spawn('powershell.exe', [
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', script,
            '-Installer', ready.file,
            '-App', appExe,
            '-ParentPid', String(pid),
            '-Report', path.join(updatesDir, REPORT_FILE),
            '-Version', ready.version,
        ], { detached: true, stdio: 'ignore', windowsHide: true });
        child.unref();
        if (timer) clearTimer(timer);
        timer = null;
        setState({ status: 'installing', version: ready.version, notes: ready.notes });
        return { started: true };
    }

    function readLastInstall() {
        const report = path.join(updatesDir, REPORT_FILE);
        if (!fs.existsSync(report)) return null;
        try {
            const raw = fs.readFileSync(report, 'utf-8').replace(/^﻿/, '');
            const parsed = JSON.parse(raw);
            return { ...parsed, installed: parsed.version === currentVersion };
        } catch {
            return null;
        } finally {
            fs.rmSync(report, { force: true });
        }
    }

    return {
        start() {
            const last = readLastInstall();
            if (last) log(`dernière installation : version ${last.version}, résultat ${last.result}, ${last.installed ? 'en place' : 'non appliquée'}`);
            schedule(FIRST_CHECK_DELAY_MS);
        },
        stop() {
            if (timer) clearTimer(timer);
            timer = null;
        },
        state: () => state,
        check,
        install,
    };
}

module.exports = { createUpdater, compareVersions, validRelease, installerName, INSTALL_SCRIPT };
