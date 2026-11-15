const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {
    activationUrl,
    activate,
    saveIdentity,
    loadIdentity,
    bridgeEnv,
    bridgeLaunchConfig,
    INVALID_CODE_MESSAGE,
} = require('../activation');

const BASE = 'https://integration.yo-club.app';
const SECRET = 'secret-tres-confidentiel';

function identity(overrides = {}) {
    return {
        club: 'empire',
        clientId: 'yoclub-poste',
        clientSecret: SECRET,
        numero: 1,
        gymBranchId: 1003,
        tokenUrl: 'https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token',
        ...overrides,
    };
}

function fakeSafeStorage({ available = true } = {}) {
    return {
        isEncryptionAvailable: () => available,
        encryptString: (text) => Buffer.from('ENC:' + Buffer.from(text, 'utf-8').reverse().toString('base64')),
        decryptString: (buffer) => {
            const text = buffer.toString('utf-8');
            if (!text.startsWith('ENC:')) throw new Error('déchiffrement impossible');
            return Buffer.from(text.slice(4), 'base64').reverse().toString('utf-8');
        },
    };
}

function tempFile() {
    return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'yoclub-poste-')), 'poste-identite.bin');
}

function fakeFetch(status, body) {
    const calls = [];
    const fetch = async (url, options) => {
        calls.push({ url, options });
        if (status === 'network') throw new Error('ENOTFOUND');
        return new Response(body === undefined ? null : JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
        });
    };
    return { fetch, calls };
}

test("l'URL d'activation passe par le préfixe tenant-management", () => {
    assert.equal(activationUrl(`${BASE}/`), `${BASE}/tenant-management/api/public/postes/activation`);
});

test("l'activation envoie club et code sans jeton et rend l'identité", async () => {
    const { fetch, calls } = fakeFetch(200, identity());
    const result = await activate({ baseUrl: BASE, club: ' empire ', code: ' abcd-efgh-jklm-npqr ', fetch });
    assert.equal(result.ok, true);
    assert.deepEqual(result.identity, identity());
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `${BASE}/tenant-management/api/public/postes/activation`);
    assert.equal(calls[0].options.method, 'POST');
    assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
    assert.equal(calls[0].options.headers.Authorization, undefined);
    assert.deepEqual(JSON.parse(calls[0].options.body), { club: 'empire', code: 'ABCD-EFGH-JKLM-NPQR' });
});

test('un code refusé donne le message prévu', async () => {
    const { fetch } = fakeFetch(400, { code: 'CODE_ACTIVATION_INVALIDE' });
    const result = await activate({ baseUrl: BASE, club: 'empire', code: 'ABCD-EFGH-JKLM-NPQR', fetch });
    assert.deepEqual(result, { ok: false, message: INVALID_CODE_MESSAGE });
    assert.equal(INVALID_CODE_MESSAGE, "Code d'activation invalide ou expiré");
});

test('club ou code vide : aucun appel réseau', async () => {
    const { fetch, calls } = fakeFetch(200, identity());
    assert.equal((await activate({ baseUrl: BASE, club: ' ', code: 'X', fetch })).ok, false);
    assert.equal((await activate({ baseUrl: BASE, club: 'empire', code: '', fetch })).ok, false);
    assert.equal(calls.length, 0);
});

test('réseau absent, erreur serveur ou réponse incomplète : échec sans secret dans le message', async () => {
    for (const [status, body] of [['network'], [500, { error: 'x' }], [200, identity({ clientSecret: '' })], [200, identity({ gymBranchId: 'abc' })], [200, identity({ club: 'autre' })]]) {
        const { fetch } = fakeFetch(status, body);
        const result = await activate({ baseUrl: BASE, club: 'empire', code: 'ABCD-EFGH-JKLM-NPQR', fetch });
        assert.equal(result.ok, false);
        assert.equal(typeof result.message, 'string');
        assert.ok(!result.message.includes(SECRET));
        assert.equal(result.identity, undefined);
    }
});

test("l'identité est écrite chiffrée et relue telle quelle", () => {
    const file = tempFile();
    const safeStorage = fakeSafeStorage();
    saveIdentity({ filePath: file, identity: identity(), safeStorage, fs });
    const raw = fs.readFileSync(file);
    assert.ok(!raw.toString('utf-8').includes(SECRET));
    assert.ok(!raw.toString('utf-8').includes('yoclub-poste'));
    assert.deepEqual(loadIdentity({ filePath: file, safeStorage, fs }), identity());
    assert.deepEqual(fs.readdirSync(path.dirname(file)), ['poste-identite.bin']);
});

test("sans chiffrement disponible, rien n'est écrit", () => {
    const file = tempFile();
    assert.throws(() => saveIdentity({ filePath: file, identity: identity(), safeStorage: fakeSafeStorage({ available: false }), fs }));
    assert.equal(fs.existsSync(file), false);
});

test('fichier absent, illisible, incomplet ou chiffrement indisponible : pas d\'identité', () => {
    const safeStorage = fakeSafeStorage();
    assert.equal(loadIdentity({ filePath: tempFile(), safeStorage, fs }), null);

    const garbage = tempFile();
    fs.writeFileSync(garbage, 'pas chiffré');
    assert.equal(loadIdentity({ filePath: garbage, safeStorage, fs }), null);

    const partial = tempFile();
    fs.writeFileSync(partial, safeStorage.encryptString(JSON.stringify(identity({ clientSecret: undefined }))));
    assert.equal(loadIdentity({ filePath: partial, safeStorage, fs }), null);

    const ok = tempFile();
    saveIdentity({ filePath: ok, identity: identity(), safeStorage, fs });
    assert.equal(loadIdentity({ filePath: ok, safeStorage: fakeSafeStorage({ available: false }), fs }), null);
});

test('les variables du pont portent les six valeurs du poste activé', () => {
    const env = bridgeEnv(identity(), { PATH: 'C:\\Windows' });
    assert.equal(env.PATH, 'C:\\Windows');
    assert.equal(env.YOCLUB_POSTE_CLUB, 'empire');
    assert.equal(env.YOCLUB_POSTE_CLIENT_ID, 'yoclub-poste');
    assert.equal(env.YOCLUB_POSTE_CLIENT_SECRET, SECRET);
    assert.equal(env.YOCLUB_POSTE_TOKEN_URL, identity().tokenUrl);
    assert.equal(env.YOCLUB_POSTE_NUMERO, '1');
    assert.equal(env.YOCLUB_POSTE_FILIALE, '1003');
});

test('la version du poste est remise au pont avec son identité', () => {
    const env = bridgeEnv(identity(), { PATH: 'x', YOCLUB_POSTE_VERSION: 'héritée' }, { version: '1.4.0' });
    assert.equal(env.YOCLUB_POSTE_VERSION, '1.4.0');
    assert.equal(bridgeEnv(identity(), { PATH: 'x', YOCLUB_POSTE_VERSION: 'héritée' }).YOCLUB_POSTE_VERSION, undefined);
    assert.deepEqual(bridgeEnv(null, { PATH: 'x' }, { version: '1.4.0' }), { PATH: 'x', YOCLUB_POSTE_VERSION: '1.4.0' });
});

test('sans activation, aucune variable YOCLUB_POSTE_ ne passe au pont, même héritée', () => {
    const env = bridgeEnv(null, { PATH: 'x', YOCLUB_POSTE_CLIENT_SECRET: 'fuite', YOCLUB_POSTE_FILIALE: '9' });
    assert.deepEqual(env, { PATH: 'x' });
});

test("poste activé : club et filiale viennent de l'identité, pas de config.json", () => {
    assert.deepEqual(bridgeLaunchConfig({ tenant: 'autre', gymBranchId: '42' }, identity()), { tenant: 'empire', gymBranchId: '1003' });
    assert.deepEqual(bridgeLaunchConfig(null, identity()), { tenant: 'empire', gymBranchId: '1003' });
    assert.deepEqual(bridgeLaunchConfig({ tenant: 'autre', gymBranchId: '42' }, null), { tenant: 'autre', gymBranchId: '42' });
});
