const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { createUpdater, compareVersions, validRelease, installerName } = require('../updater');

const BASE = 'https://integration.yo-club.app';
const PAYLOAD = Buffer.from('installeur factice '.repeat(5000));
const SHA = crypto.createHash('sha256').update(PAYLOAD).digest('hex');

function release(overrides = {}) {
    return {
        version: '1.2.0',
        notes: 'Corrections',
        size: PAYLOAD.length,
        sha256: SHA,
        downloadUrl: `${BASE}/document-management/public/desktop-release-files/abc`,
        ...overrides,
    };
}

function tempDir() {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'yoclub-maj-'));
}

function fakeFetch({ latest = release(), body = PAYLOAD, status = 200, failLatest = false } = {}) {
    const calls = [];
    const fetch = async (url) => {
        calls.push(url);
        if (url.includes('/desktop-releases/latest')) {
            if (failLatest) throw new Error('réseau absent');
            if (latest === null) return new Response(null, { status: 204 });
            return new Response(JSON.stringify(latest), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response(body, { status });
    };
    return { fetch, calls };
}

function updater(dir, fetch, extra = {}) {
    const states = [];
    const instance = createUpdater({
        baseUrl: BASE,
        tenant: 'empire',
        currentVersion: '1.1.0',
        updatesDir: dir,
        fetch,
        spawn: extra.spawn || (() => { throw new Error('spawn inattendu'); }),
        onState: (state) => states.push(state),
        setTimer: () => 1,
        clearTimer: () => {},
        log: extra.log || (() => {}),
    });
    return { instance, states };
}

test('compare les versions champ par champ', () => {
    assert.ok(compareVersions('1.10.0', '1.9.9') > 0);
    assert.equal(compareVersions('2.0.0', '2.0.0'), 0);
    assert.ok(compareVersions('1.0.9', '1.1.0') < 0);
});

test('un poste à jour reçoit 204 et ne garde que les deux installeurs les plus récents', async () => {
    const dir = tempDir();
    for (const v of ['1.0.0', '1.0.1', '1.1.0']) fs.writeFileSync(path.join(dir, installerName(v)), 'x');
    fs.writeFileSync(path.join(dir, `${installerName('1.2.0')}.part`), 'x');
    const { fetch, calls } = fakeFetch({ latest: null });
    const { instance } = updater(dir, fetch);
    const state = await instance.check();
    assert.equal(state.status, 'up-to-date');
    assert.equal(calls[0], `${BASE}/tenant-management/api/public/desktop-releases/latest?tenant=empire&current=1.1.0`);
    assert.deepEqual(fs.readdirSync(dir).sort(), [installerName('1.0.1'), installerName('1.1.0')].sort());
});

test('une nouvelle version est téléchargée, vérifiée puis annoncée prête', async () => {
    const dir = tempDir();
    const { fetch } = fakeFetch();
    const { instance, states } = updater(dir, fetch);
    const state = await instance.check();
    assert.equal(state.status, 'ready');
    assert.equal(state.version, '1.2.0');
    assert.ok(states.some((s) => s.status === 'downloading' && s.progress === 100));
    assert.deepEqual(fs.readFileSync(path.join(dir, installerName('1.2.0'))), PAYLOAD);
    assert.ok(!fs.readdirSync(dir).some((name) => name.endsWith('.part')));
});

test('un fichier déjà téléchargé et vérifié n\'est pas retéléchargé', async () => {
    const dir = tempDir();
    fs.writeFileSync(path.join(dir, installerName('1.2.0')), PAYLOAD);
    const { fetch, calls } = fakeFetch();
    const { instance } = updater(dir, fetch);
    assert.equal((await instance.check()).status, 'ready');
    assert.equal(calls.length, 1);
});

test('une empreinte fausse est refusée et ne laisse aucun fichier', async () => {
    const dir = tempDir();
    const { fetch } = fakeFetch({ body: Buffer.alloc(PAYLOAD.length, 1) });
    const { instance } = updater(dir, fetch);
    const state = await instance.check();
    assert.equal(state.status, 'error');
    assert.equal(state.error, 'CHECKSUM_MISMATCH');
    assert.deepEqual(fs.readdirSync(dir), []);
});

test('un fichier plus long qu\'annoncé est coupé net', async () => {
    const dir = tempDir();
    const { fetch } = fakeFetch({ body: Buffer.concat([PAYLOAD, Buffer.from('de trop')]) });
    const { instance } = updater(dir, fetch);
    assert.equal((await instance.check()).error, 'SIZE_MISMATCH');
    assert.deepEqual(fs.readdirSync(dir), []);
});

test('un lien hors de la plateforme n\'est jamais suivi', async () => {
    const dir = tempDir();
    const { fetch, calls } = fakeFetch({ latest: release({ downloadUrl: 'https://ailleurs.example/x.exe' }) });
    const { instance } = updater(dir, fetch);
    assert.equal((await instance.check()).error, 'DOWNLOAD_URL_INVALID');
    assert.equal(calls.length, 1);
});

test('les réponses incohérentes du serveur sont refusées', () => {
    assert.throws(() => validRelease(release({ version: '1.1.0' }), BASE, '1.1.0'), /VERSION_INVALID/);
    assert.throws(() => validRelease(release({ sha256: 'abc' }), BASE, '1.1.0'), /SHA256_INVALID/);
    assert.throws(() => validRelease(release({ size: 0 }), BASE, '1.1.0'), /SIZE_INVALID/);
    assert.throws(() => validRelease(release({ downloadUrl: 'http://integration.yo-club.app/x' }), BASE, '1.1.0'), /DOWNLOAD_URL_INVALID/);
});

test('une coupure réseau ne fait pas perdre une mise à jour déjà prête', async () => {
    const dir = tempDir();
    const ok = fakeFetch();
    let failing = false;
    const fetch = (url, options) => (failing ? Promise.reject(new Error('réseau absent')) : ok.fetch(url, options));
    const { instance } = updater(dir, fetch);
    await instance.check();
    failing = true;
    const state = await instance.check();
    assert.equal(state.status, 'ready');
    assert.equal(state.version, '1.2.0');
});

test('l\'installation n\'est lancée que si la mise à jour est prête', async () => {
    const dir = tempDir();
    const { instance } = updater(dir, fakeFetch({ latest: null }).fetch);
    await instance.check();
    assert.deepEqual(await instance.install({ appExe: 'C:\\Yo-Club\\Yo-Club.exe', pid: 42 }), { started: false, error: 'NOT_READY' });
});

test('l\'installation lance le script détaché avec l\'installeur vérifié', async () => {
    const dir = tempDir();
    const spawned = [];
    const spawn = (command, args, options) => {
        spawned.push({ command, args, options });
        return { unref() {} };
    };
    const { instance } = updater(dir, fakeFetch().fetch, { spawn });
    await instance.check();
    const result = await instance.install({ appExe: 'C:\\Yo-Club\\Yo-Club.exe', pid: 42 });
    assert.deepEqual(result, { started: true });
    assert.equal(instance.state().status, 'installing');
    assert.equal(spawned.length, 1);
    const { command, args, options } = spawned[0];
    assert.equal(command, 'powershell.exe');
    assert.equal(args[args.indexOf('-Installer') + 1], path.join(dir, installerName('1.2.0')));
    assert.equal(args[args.indexOf('-App') + 1], 'C:\\Yo-Club\\Yo-Club.exe');
    assert.equal(args[args.indexOf('-ParentPid') + 1], '42');
    assert.equal(args[args.indexOf('-Version') + 1], '1.2.0');
    assert.ok(fs.readFileSync(args[args.indexOf('-File') + 1], 'utf-8').includes("-Verb RunAs"));
    assert.equal(options.detached, true);
    assert.equal(await instance.check(), instance.state());
});

test('un installeur modifié après vérification n\'est pas lancé', async () => {
    const dir = tempDir();
    const spawn = () => { throw new Error('ne doit pas être lancé'); };
    const { instance } = updater(dir, fakeFetch().fetch, { spawn });
    await instance.check();
    fs.writeFileSync(path.join(dir, installerName('1.2.0')), Buffer.alloc(PAYLOAD.length, 2));
    const result = await instance.install({ appExe: 'C:\\Yo-Club\\Yo-Club.exe', pid: 42 });
    assert.deepEqual(result, { started: false, error: 'CHECKSUM_MISMATCH' });
    assert.equal(instance.state().status, 'error');
});

test('le compte rendu de la dernière installation est lu une fois puis effacé', () => {
    const dir = tempDir();
    const report = path.join(dir, 'derniere-installation.json');
    fs.writeFileSync(report, '\uFEFF{"version":"1.1.0","result":"0","at":"2026-11-02T08:00:00Z"}');
    const lines = [];
    const { instance } = updater(dir, fakeFetch({ latest: null }).fetch, { log: (line) => lines.push(line) });
    instance.start();
    assert.ok(lines.some((line) => line.includes('version 1.1.0') && line.includes('en place')));
    assert.ok(!fs.existsSync(report));
});
