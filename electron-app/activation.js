const path = require('path');

const IDENTITY_FILE = 'poste-identite.bin';
const ACTIVATION_PATH = '/tenant-management/api/public/postes/activation';
const ACTIVATION_TIMEOUT_MS = 15000;
const INVALID_CODE_MESSAGE = "Code d'activation invalide ou expiré";
const ENV_PREFIX = 'YOCLUB_POSTE_';

function activationUrl(baseUrl) {
    return String(baseUrl).replace(/\/+$/, '') + ACTIVATION_PATH;
}

function nonEmptyString(value) {
    return typeof value === 'string' && value.trim().length > 0;
}

function positiveInteger(value) {
    return /^\d+$/.test(String(value ?? '')) && Number(value) > 0;
}

function isValidIdentity(identity) {
    return Boolean(identity)
        && typeof identity === 'object'
        && nonEmptyString(identity.club)
        && nonEmptyString(identity.clientId)
        && nonEmptyString(identity.clientSecret)
        && nonEmptyString(identity.tokenUrl)
        && positiveInteger(identity.numero)
        && positiveInteger(identity.gymBranchId);
}

function pickIdentity(source) {
    return {
        club: source.club,
        clientId: source.clientId,
        clientSecret: source.clientSecret,
        numero: Number(source.numero),
        gymBranchId: Number(source.gymBranchId),
        tokenUrl: source.tokenUrl,
    };
}

async function readJson(response) {
    try {
        return await response.json();
    } catch {
        return null;
    }
}

async function activate({ baseUrl, club, code, fetch, timeoutMs = ACTIVATION_TIMEOUT_MS }) {
    const cleanClub = String(club ?? '').trim();
    const cleanCode = String(code ?? '').trim().toUpperCase();
    if (!cleanClub) return { ok: false, message: 'Le club est obligatoire.' };
    if (!cleanCode) return { ok: false, message: "Le code d'activation est obligatoire." };

    let response;
    try {
        response = await fetch(activationUrl(baseUrl), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ club: cleanClub, code: cleanCode }),
            signal: AbortSignal.timeout(timeoutMs),
        });
    } catch {
        return { ok: false, message: 'Serveur Yo-Club injoignable : vérifiez la connexion internet puis réessayez.' };
    }

    const body = await readJson(response);
    if (response.status === 400 && body && body.code === 'CODE_ACTIVATION_INVALIDE') {
        return { ok: false, message: INVALID_CODE_MESSAGE };
    }
    if (!response.ok) {
        return { ok: false, message: `Activation impossible (HTTP ${response.status}). Réessayez ou contactez Yo-Club.` };
    }
    if (!isValidIdentity(body) || body.club !== cleanClub) {
        return { ok: false, message: 'Réponse du serveur incomplète : activation impossible. Contactez Yo-Club.' };
    }
    return { ok: true, identity: pickIdentity(body) };
}

function saveIdentity({ filePath, identity, safeStorage, fs }) {
    if (!isValidIdentity(identity)) throw new Error('identité du poste incomplète');
    if (!safeStorage.isEncryptionAvailable()) throw new Error('chiffrement Windows indisponible');
    const encrypted = safeStorage.encryptString(JSON.stringify(pickIdentity(identity)));
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    const tmp = `${filePath}.tmp`;
    fs.writeFileSync(tmp, encrypted);
    fs.renameSync(tmp, filePath);
}

function loadIdentity({ filePath, safeStorage, fs }) {
    try {
        if (!fs.existsSync(filePath)) return null;
        if (!safeStorage.isEncryptionAvailable()) return null;
        const identity = JSON.parse(safeStorage.decryptString(fs.readFileSync(filePath)));
        return isValidIdentity(identity) ? pickIdentity(identity) : null;
    } catch {
        return null;
    }
}

function bridgeEnv(identity, baseEnv = process.env, { version } = {}) {
    const env = {};
    for (const [key, value] of Object.entries(baseEnv)) {
        if (!key.toUpperCase().startsWith(ENV_PREFIX)) env[key] = value;
    }
    const versionEnv = version ? { YOCLUB_POSTE_VERSION: String(version) } : {};
    if (!identity) return { ...env, ...versionEnv };
    return {
        ...env,
        YOCLUB_POSTE_CLUB: identity.club,
        YOCLUB_POSTE_CLIENT_ID: identity.clientId,
        YOCLUB_POSTE_CLIENT_SECRET: identity.clientSecret,
        YOCLUB_POSTE_TOKEN_URL: identity.tokenUrl,
        YOCLUB_POSTE_NUMERO: String(identity.numero),
        YOCLUB_POSTE_FILIALE: String(identity.gymBranchId),
        ...versionEnv,
    };
}

function bridgeLaunchConfig(config, identity) {
    if (identity) return { tenant: identity.club, gymBranchId: String(identity.gymBranchId) };
    return config;
}

module.exports = {
    IDENTITY_FILE,
    INVALID_CODE_MESSAGE,
    activationUrl,
    activate,
    isValidIdentity,
    saveIdentity,
    loadIdentity,
    bridgeEnv,
    bridgeLaunchConfig,
};
