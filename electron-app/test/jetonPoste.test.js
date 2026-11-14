const test = require('node:test');
const assert = require('node:assert/strict');
const {
    createJetonPoste,
    estHotePlateforme,
    entetesPoste,
    installerEntetesPoste,
    ENTETES,
} = require('../jetonPoste');

const BASE = 'https://integration.yo-club.app';
const SECRET = 'secret-du-poste';
const TOKEN_URL = 'https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token';

function identity(overrides = {}) {
    return {
        club: 'empire',
        clientId: 'yoclub-poste',
        clientSecret: SECRET,
        numero: 7,
        gymBranchId: 1003,
        tokenUrl: TOKEN_URL,
        ...overrides,
    };
}

function reponse(status, body) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => {
            if (body instanceof Error) throw body;
            return body;
        },
    };
}

function fakeFetch(steps) {
    const calls = [];
    const queue = [...steps];
    const fn = async (url, options) => {
        calls.push({ url, options });
        const step = queue.shift();
        if (step instanceof Error) throw step;
        if (!step) throw new Error('aucune réponse prévue');
        return step;
    };
    fn.calls = calls;
    return fn;
}

function clock(start = 1000) {
    const c = { t: start, now: () => c.t };
    return c;
}

function journal() {
    const lines = [];
    const log = (...args) => lines.push(args.join(' '));
    log.text = () => lines.join('\n');
    return log;
}

test('le jeton est demandé au serveur de jetons en client_credentials, formulaire', async () => {
    const fetch = fakeFetch([reponse(200, { access_token: 'jeton-1', expires_in: 300 })]);
    const jeton = createJetonPoste({ identity: identity(), fetch, now: clock().now, log: journal() });
    assert.equal(await jeton.valeur(), 'jeton-1');
    assert.equal(fetch.calls.length, 1);
    const { url, options } = fetch.calls[0];
    assert.equal(url, TOKEN_URL);
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['Content-Type'], 'application/x-www-form-urlencoded');
    const form = new URLSearchParams(options.body);
    assert.equal(form.get('grant_type'), 'client_credentials');
    assert.equal(form.get('client_id'), 'yoclub-poste');
    assert.equal(form.get('client_secret'), SECRET);
});

test('le jeton est réutilisé jusqu’à 80 % de sa durée, puis renouvelé', async () => {
    const c = clock();
    const fetch = fakeFetch([
        reponse(200, { access_token: 'jeton-1', expires_in: 300 }),
        reponse(200, { access_token: 'jeton-2', expires_in: 300 }),
    ]);
    const jeton = createJetonPoste({ identity: identity(), fetch, now: c.now, log: journal() });
    assert.equal(await jeton.valeur(), 'jeton-1');
    c.t += 239000;
    assert.equal(await jeton.valeur(), 'jeton-1');
    assert.equal(fetch.calls.length, 1);
    c.t += 2000;
    assert.equal(await jeton.valeur(), 'jeton-2');
    assert.equal(fetch.calls.length, 2);
});

test('des requêtes simultanées ne demandent qu’un seul jeton', async () => {
    const fetch = fakeFetch([reponse(200, { access_token: 'jeton-1', expires_in: 300 })]);
    const jeton = createJetonPoste({ identity: identity(), fetch, now: clock().now, log: journal() });
    const valeurs = await Promise.all([jeton.valeur(), jeton.valeur(), jeton.valeur()]);
    assert.deepEqual(valeurs, ['jeton-1', 'jeton-1', 'jeton-1']);
    assert.equal(fetch.calls.length, 1);
});

test('en échec, pas de jeton ; nouvel essai après le délai', async () => {
    const c = clock();
    const fetch = fakeFetch([
        new Error('réseau coupé'),
        reponse(401, { error: 'unauthorized_client' }),
        reponse(200, { access_token: 'jeton-1' }),
        reponse(200, { access_token: 'jeton-2', expires_in: 300 }),
    ]);
    const jeton = createJetonPoste({ identity: identity(), fetch, now: c.now, log: journal(), delaiApresEchecMs: 30000 });
    assert.equal(await jeton.valeur(), null);
    c.t += 10000;
    assert.equal(await jeton.valeur(), null);
    assert.equal(fetch.calls.length, 1, 'pas de nouvel essai avant le délai');
    c.t += 20000;
    assert.equal(await jeton.valeur(), null, 'HTTP 401');
    c.t += 30000;
    assert.equal(await jeton.valeur(), null, 'expires_in absent');
    c.t += 30000;
    assert.equal(await jeton.valeur(), 'jeton-2');
    assert.equal(fetch.calls.length, 4);
});

test('un jeton invalidé est redemandé tout de suite', async () => {
    const fetch = fakeFetch([
        reponse(200, { access_token: 'jeton-1', expires_in: 300 }),
        reponse(200, { access_token: 'jeton-2', expires_in: 300 }),
    ]);
    const jeton = createJetonPoste({ identity: identity(), fetch, now: clock().now, log: journal() });
    assert.equal(await jeton.valeur(), 'jeton-1');
    jeton.invalider();
    assert.equal(await jeton.valeur(), 'jeton-2');
});

test('ni secret ni jeton dans le journal, même en échec', async () => {
    const log = journal();
    const fetch = fakeFetch([
        reponse(200, { access_token: 'jeton-1', expires_in: 300 }),
        new Error(`échec avec ${SECRET}`),
        reponse(400, { error: 'invalid_client', error_description: SECRET }),
    ]);
    const c = clock();
    const jeton = createJetonPoste({ identity: identity(), fetch, now: c.now, log, delaiApresEchecMs: 0 });
    await jeton.valeur();
    jeton.invalider();
    await jeton.valeur();
    c.t += 1;
    await jeton.valeur();
    assert.ok(log.text().length > 0, 'les échecs sont journalisés');
    assert.ok(!log.text().includes(SECRET), 'secret journalisé');
    assert.ok(!log.text().includes('jeton-1'), 'jeton journalisé');
});

test('seul l’hôte de la plateforme reçoit les en-têtes', () => {
    assert.equal(estHotePlateforme('https://integration.yo-club.app/gym-management/api/adherents', BASE), true);
    assert.equal(estHotePlateforme('https://integration.yo-club.app/', BASE), true);
    assert.equal(estHotePlateforme('wss://integration.yo-club.app/notification/ws', BASE), true);
    assert.equal(estHotePlateforme('https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token', BASE), false);
    assert.equal(estHotePlateforme('https://evil.example/integration.yo-club.app', BASE), false);
    assert.equal(estHotePlateforme('https://integration.yo-club.app.evil.example/', BASE), false);
    assert.equal(estHotePlateforme('https://integration.yo-club.app:8443/', BASE), false);
    assert.equal(estHotePlateforme('http://integration.yo-club.app/api', BASE), false);
    assert.equal(estHotePlateforme('http://127.0.0.1:9998/health', BASE), false);
    assert.equal(estHotePlateforme('http://localhost:8765/', BASE), false);
    assert.equal(estHotePlateforme('data:text/html,x', BASE), false);
    assert.equal(estHotePlateforme('pas une url', BASE), false);
    assert.equal(estHotePlateforme('http://localhost:4200/api', 'http://localhost:4200'), false);
    assert.equal(estHotePlateforme('https://integration.yo-club.app/api', 'pas une url'), false);
});

test('les quatre en-têtes du contrat', () => {
    assert.deepEqual(entetesPoste({ jeton: 'jeton-1', identity: identity(), version: '1.4.0' }), {
        'X-Yoclub-Poste': 'jeton-1',
        'X-Yoclub-Filiale': '1003',
        'X-Yoclub-Poste-Numero': '7',
        'X-Yoclub-Poste-Version': '1.4.0',
    });
    assert.deepEqual(ENTETES, ['X-Yoclub-Poste', 'X-Yoclub-Filiale', 'X-Yoclub-Poste-Numero', 'X-Yoclub-Poste-Version']);
});

function fakeSession() {
    const s = {
        webRequest: {
            onBeforeSendHeaders(filter, listener) {
                s.filter = filter;
                s.listener = listener;
            },
        },
        send(url, requestHeaders = {}) {
            return new Promise((resolve) => s.listener({ url, requestHeaders }, resolve));
        },
    };
    return s;
}

function jetonFixe(valeur) {
    return { valeur: async () => valeur, appels: 0 };
}

test('requête vers la plateforme : en-têtes ajoutés, ceux de la page remplacés', async () => {
    const s = fakeSession();
    installerEntetesPoste({ session: s, baseUrl: BASE, identity: identity(), jeton: jetonFixe('jeton-1'), version: '1.4.0' });
    const { requestHeaders, cancel } = await s.send(`${BASE}/gym-management/api/adherents`, {
        Authorization: 'Bearer utilisateur',
        'x-yoclub-poste': 'imposé',
        'X-YOCLUB-FILIALE': '9999',
    });
    assert.notEqual(cancel, true);
    assert.equal(requestHeaders.Authorization, 'Bearer utilisateur');
    assert.equal(requestHeaders['X-Yoclub-Poste'], 'jeton-1');
    assert.equal(requestHeaders['X-Yoclub-Filiale'], '1003');
    assert.equal(requestHeaders['X-Yoclub-Poste-Numero'], '7');
    assert.equal(requestHeaders['X-Yoclub-Poste-Version'], '1.4.0');
    const yoclub = Object.keys(requestHeaders).filter((k) => k.toLowerCase().startsWith('x-yoclub-'));
    assert.equal(yoclub.length, 4, 'aucun doublon de casse');
});

test('requête vers un autre hôte : rien n’est ajouté ni retiré, aucun jeton demandé', async () => {
    const s = fakeSession();
    let demandes = 0;
    const jeton = { valeur: async () => { demandes += 1; return 'jeton-1'; } };
    installerEntetesPoste({ session: s, baseUrl: BASE, identity: identity(), jeton, version: '1.4.0' });
    for (const url of [
        'https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token',
        'http://127.0.0.1:9998/health',
        'https://cdn.example/x.js',
    ]) {
        const headers = { Accept: '*/*', 'X-Yoclub-Poste': 'page' };
        const { requestHeaders } = await s.send(url, headers);
        assert.deepEqual(requestHeaders, { Accept: '*/*', 'X-Yoclub-Poste': 'page' }, url);
    }
    assert.equal(demandes, 0);
});

test('jeton indisponible : aucun en-tête de poste, la requête part quand même', async () => {
    const s = fakeSession();
    installerEntetesPoste({ session: s, baseUrl: BASE, identity: identity(), jeton: jetonFixe(null), version: '1.4.0' });
    const { requestHeaders, cancel } = await s.send(`${BASE}/gym-management/api/adherents`, {
        Accept: 'application/json',
        'X-Yoclub-Poste-Numero': '42',
    });
    assert.notEqual(cancel, true);
    assert.deepEqual(requestHeaders, { Accept: 'application/json' });
});

test('poste non activé : aucun en-tête, aucun jeton', async () => {
    const s = fakeSession();
    installerEntetesPoste({ session: s, baseUrl: BASE, identity: null, jeton: null, version: '1.4.0' });
    const { requestHeaders, cancel } = await s.send(`${BASE}/gym-management/api/adherents`, {
        Accept: 'application/json',
        'X-Yoclub-Filiale': '1003',
    });
    assert.notEqual(cancel, true);
    assert.deepEqual(requestHeaders, { Accept: 'application/json' });
});

test('une erreur inattendue du jeton ne bloque pas la requête', async () => {
    const s = fakeSession();
    const jeton = { valeur: async () => { throw new Error('inattendu'); } };
    installerEntetesPoste({ session: s, baseUrl: BASE, identity: identity(), jeton, version: '1.4.0', log: journal() });
    const { requestHeaders } = await s.send(`${BASE}/api`, { Accept: '*/*' });
    assert.deepEqual(requestHeaders, { Accept: '*/*' });
});
