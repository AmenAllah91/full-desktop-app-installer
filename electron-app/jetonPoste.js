const PART_DE_VIE = 0.8;
const DELAI_JETON_MS = 10000;
const DELAI_APRES_ECHEC_MS = 30000;
const ENTETES = ['X-Yoclub-Poste', 'X-Yoclub-Filiale', 'X-Yoclub-Poste-Numero', 'X-Yoclub-Poste-Version'];
const ENTETES_MINUSCULES = ENTETES.map((nom) => nom.toLowerCase());
const HOTES_LOCAUX = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);
const PROTOCOLES_ASSOCIES = { 'https:': ['https:', 'wss:'], 'http:': ['http:', 'ws:'] };

function createJetonPoste({
    identity,
    fetch,
    now = Date.now,
    log = () => {},
    delaiApresEchecMs = DELAI_APRES_ECHEC_MS,
    delaiJetonMs = DELAI_JETON_MS,
}) {
    let valeurCourante = null;
    let renouvelerA = 0;
    let prochainEssaiA = 0;
    let enCours = null;

    async function obtenir() {
        let response;
        try {
            response = await fetch(identity.tokenUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
                body: new URLSearchParams({
                    grant_type: 'client_credentials',
                    client_id: identity.clientId,
                    client_secret: identity.clientSecret,
                }).toString(),
                signal: AbortSignal.timeout(delaiJetonMs),
            });
        } catch (err) {
            throw new Error(`jeton du poste injoignable (${(err && err.name) || 'Error'})`);
        }
        if (!response.ok) throw new Error(`jeton du poste refusé (HTTP ${response.status})`);
        let corps;
        try {
            corps = await response.json();
        } catch {
            throw new Error('réponse de jeton illisible');
        }
        const valeur = corps && corps.access_token;
        const duree = Number(corps && corps.expires_in);
        if (typeof valeur !== 'string' || !valeur || !(duree > 0)) throw new Error('réponse de jeton incomplète');
        return { valeur, duree };
    }

    function renouveler() {
        enCours = obtenir()
            .then(({ valeur, duree }) => {
                valeurCourante = valeur;
                renouvelerA = now() + duree * 1000 * PART_DE_VIE;
                return valeur;
            })
            .catch((err) => {
                valeurCourante = null;
                prochainEssaiA = now() + delaiApresEchecMs;
                log(`${err.message}, requêtes envoyées sans preuve du poste`);
                return null;
            })
            .finally(() => {
                enCours = null;
            });
        return enCours;
    }

    async function valeur() {
        if (valeurCourante && now() < renouvelerA) return valeurCourante;
        if (enCours) return enCours;
        valeurCourante = null;
        if (now() < prochainEssaiA) return null;
        return renouveler();
    }

    function invalider() {
        valeurCourante = null;
        prochainEssaiA = 0;
    }

    return { valeur, invalider };
}

function estHotePlateforme(url, baseUrl) {
    let cible;
    let base;
    try {
        cible = new URL(url);
        base = new URL(baseUrl);
    } catch {
        return false;
    }
    const protocoles = PROTOCOLES_ASSOCIES[base.protocol];
    if (!protocoles || !protocoles.includes(cible.protocol)) return false;
    if (HOTES_LOCAUX.has(cible.hostname) || HOTES_LOCAUX.has(base.hostname)) return false;
    const port = (u) => u.port || (['https:', 'wss:'].includes(u.protocol) ? '443' : '80');
    return cible.hostname === base.hostname && port(cible) === port(base);
}

function entetesPoste({ jeton, identity, version }) {
    return {
        'X-Yoclub-Poste': jeton,
        'X-Yoclub-Filiale': String(identity.gymBranchId),
        'X-Yoclub-Poste-Numero': String(identity.numero),
        'X-Yoclub-Poste-Version': String(version),
    };
}

function sansEntetesPoste(requestHeaders) {
    const propres = {};
    for (const [nom, valeur] of Object.entries(requestHeaders || {})) {
        if (!ENTETES_MINUSCULES.includes(nom.toLowerCase())) propres[nom] = valeur;
    }
    return propres;
}

function installerEntetesPoste({ session, baseUrl, identity, jeton, version, log = () => {} }) {
    session.webRequest.onBeforeSendHeaders({ urls: ['<all_urls>'] }, (details, callback) => {
        if (!estHotePlateforme(details.url, baseUrl)) {
            callback({ requestHeaders: details.requestHeaders });
            return;
        }
        const propres = sansEntetesPoste(details.requestHeaders);
        if (!identity || !jeton) {
            callback({ requestHeaders: propres });
            return;
        }
        Promise.resolve()
            .then(() => jeton.valeur())
            .catch(() => {
                log('jeton du poste indisponible, requête envoyée sans preuve du poste');
                return null;
            })
            .then((valeur) => {
                callback({
                    requestHeaders: valeur ? { ...propres, ...entetesPoste({ jeton: valeur, identity, version }) } : propres,
                });
            });
    });
}

module.exports = {
    ENTETES,
    createJetonPoste,
    estHotePlateforme,
    entetesPoste,
    installerEntetesPoste,
};
