# 🖥️ Desktop App — README détaillé

## 📂 Présentation du repo

Ce dépôt contient l’outil **builder** (`builder.bat`) et la logique permettant de générer une **application desktop installable**.

Il existe **deux branches principales** :

1. **`full-desktop-int`** — version **hors-ligne / tout en local**  
   - Le `builder.bat` *clône* les repositories front-end et back-end, les build, déploie une base **H2 locale** et assemble l’application desktop complète.  
   - Branches attendues dans les repos clônés :
     - backend : `integration-desktop-H2`
     - frontend : `integration-desktop`

2. **`web-desktop-int`** — version **connectée / client web**  
   - Le `builder.bat` **ne clône pas** les repos front/back.  
   - L’application desktop est un client qui **se connecte** à des services front/back déjà déployés sur un serveur via HTTPS.  
   - Le builder prépare uniquement l’UI desktop et la configuration du domaine distant.

---

## ⚙️ Fonctionnalités principales du builder

- Clone (si c la version hors ligne) les repos front/back suivant la branche.  
- Lance les builds front (npm / node) et back (mvn / java) si nécessaire.  
- Prépare la base H2 embarquée pour la version `full-desktop-int` (automatiquement via l'application spring boot).  
- Génère l’installateur de l’application desktop
- Produit un répertoire `build-output/` contenant l’installateur et les artefacts.

---

## 🖥️ Prérequis

### Pour **`full-desktop-int`** (hors-ligne)
- **Maven 3.9.x**
- **Java 17 (JDK 17)**
- **Node.js 18.x**
- **Python 3.9**
- **Git**
- *(Optionnel)* Outil de packaging Windows : **NSIS**

### Pour **`web-desktop-int`** (client connecté)
- **Node.js 18.x**
- **Python 3.9**
- *(Optionnel)* Outil de packaging Windows : **NSIS**
- *(Git non requis, car aucun clone front/back)*


---

## 🚀 Étapes — `full-desktop-int` (local complet)

1. **Préparer l’environnement**
   ```bash
   mvn -v
   java -version
   node -v
   python --version
   git --version
   ```

2. **Lancer le builder**
   ```bash
  build-installer.bat
   ```

3. **Ce que fait le script en gros**
   - Clone front (`integration-desktop`) et back (`integration-desktop-H2`)
   - Build backend :
     ```bash
     mvn clean package -DskipTests
     ```
   - Build frontend :
     ```bash
     npm install
     ng build
     ```
   - Build backend :
     ```bash
     mvn clean install
     ```
   - installe les modules nécessaires et génere un .exe de `pythonApp/`
   - installe les modules nécessaires pour `electron-app/`
   - Génére l’installateur final dans `build-output/`

4. **Résultat**
   - Fichier installateur généré (`.exe`)  
---

## 🚀 Étapes — `web-desktop-int` (connecté)

1. **Préparer l’environnement**
   - Node.js 18 et Python 3.9 installés

3. **Lancer le builder**
   ```bash
  build-installer.bat web
   ```

4. **Ce que fait le script**
   - génere un .exe de `pythonApp/`
   - installe les modules nécessaires pour `electron-app/`
   - Génére l’installateur final dans `build-output/`

---

## 🔒 H2 (version locale)

- Base embarquée dans un fichier `*.mv.db`  
- Connexion via : `jdbc:h2:file:./data/gymdb` (le dossier data va etre généré automation sous le repertoire AppData)
- insertion des roles et un utilisateur initial dans la bd H2 (credentials: admin admin)
---


---

## 🐞 Dépannage

| Problème | Cause probable | Solution |
|-----------|----------------|-----------|
| `mvn` non trouvé | PATH incorrect | Vérifie `JAVA_HOME`, `MAVEN_HOME` |
| Build front échoue | Node incompatible | Utilise Node 18, supprime `node_modules` puis `npm install` |
| Git clone échoue | Auth manquante | Configurer SSH key ou token |
| H2 "Object Already Closed" | Mauvais chemin / fermeture prématurée | Vérifie la config et les droits d’accès |
| SSL error (web) | Certificat invalide | Utiliser certificat valide (Let's Encrypt, etc.) |

---

## ✅ Check-list avant build

- [ ] Java 17 installé  
- [ ] Maven 3.9 installé  
- [ ] Node 18 installé  
- [ ] Python 3.9 installé  
- [ ] Git configuré  
- [ ] Outil de packaging installé  

---

## 🧾 Commandes utiles (debug manuel)

**Backend**
```bash
git clone -b integration-desktop-H2 https://github.com/monorg/backend-repo.git backend
cd backend
mvn clean package -DskipTests
```

**Frontend**
```bash
git clone -b integration-desktop https://github.com/monorg/frontend-repo.git frontend
cd frontend
npm ci
ng build
```

**Lancer l’app (dev)**
```bash
cd pythonApp
python main.py
```


