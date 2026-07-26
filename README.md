# CoursConnect (version Python)

Plateforme de cours en ligne construite avec **Flask** et **SQLite**. Les comptes, cours et
inscriptions sont persistés dans un vrai fichier de base de données (`coursconnect.db`,
créé automatiquement au premier lancement).

## Installation

```bash
python3 -m venv venv
source venv/bin/activate        # sous Windows : venv\Scripts\activate
pip install -r requirements.txt
```

## Lancement

```bash
python app.py
```

Puis ouvrir **http://127.0.0.1:5000** dans un navigateur.

## Comptes de démonstration

| Rôle       | E-mail                     | Mot de passe |
|------------|-----------------------------|---------------|
| Professeur | camille@coursconnect.fr     | prof123       |
| Professeur | karim@coursconnect.fr       | prof123       |
| Étudiant   | lea@coursconnect.fr         | etu123        |

La page **Connexion** propose aussi deux boutons « Démo » pour se connecter en un clic
sans saisir d'identifiants.

## Structure du site

```
CoursConnect
│
├── Accueil
│   ├── Présentation        → /presentation
│   └── Liste des cours     → /cours
│
├── Authentification
│   ├── Connexion            → /connexion
│   └── Inscription          → /inscription
│
├── Professeur
│   ├── Tableau de bord       → /prof/tableau-de-bord
│   └── Créer un cours        → /prof/creer
│
└── Étudiant
    └── Mes cours              → /etudiant/mes-cours
```

## Fonctionnement

- Les mots de passe sont hachés avec `werkzeug.security` (jamais stockés en clair).
- La session est gérée avec les sessions signées de Flask (cookie `session`).
- Un professeur peut créer et supprimer ses propres cours.
- Un étudiant peut s'inscrire et se désinscrire des cours depuis la liste des cours ou
  son tableau de bord.
- Les pages « Professeur » et « Étudiant » sont protégées : un accès sans le bon rôle
  redirige vers la page de connexion avec un message explicatif.

## Aller plus loin

- Remplacer `app.secret_key` par une vraie valeur secrète (variable d'environnement) avant
  toute mise en production.
- Passer de SQLite à PostgreSQL pour un usage à plus grande échelle.
- Ajouter une validation d'e-mail et une politique de mot de passe plus stricte.
