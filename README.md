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

## Paiements (Stripe + séquestre 24h)

CoursConnect intègre un système de paiement complet :

- **Prix par séance** : chaque professeur fixe un prix (en euros) à la création d'un cours.
- **Portefeuille élève** : un élève recharge son portefeuille interne par carte bancaire via
  **Stripe Checkout** (`/portefeuille`).
- **Séquestre (escrow)** : à la réservation d'un créneau, le prix est débité du portefeuille
  de l'élève et mis en attente — le professeur **n'est pas payé immédiatement**.
- **Fenêtre de 24h après le cours** : une fois le créneau passé, l'élève peut, depuis
  « Mes cours » :
  - **confirmer** que le cours s'est bien passé → le professeur est crédité immédiatement, ou
  - **demander un remboursement** → la somme est recréditée sur son portefeuille.
- **Versement automatique** : si l'élève ne fait rien dans les 24h suivant la fin du cours,
  un thread d'arrière-plan (`start_auto_release_background_thread`, vérification chaque
  minute) verse automatiquement le professeur.
- Si l'élève annule un créneau **avant** le cours, il est remboursé automatiquement.
- **Versements réels** : depuis « Mon portefeuille », un professeur ou un élève peut
  configurer son compte bancaire avec **Stripe Connect Express**, puis retirer son solde.
  Le retrait crée un transfert Stripe vers le compte Connect ; Stripe effectue ensuite le
  versement bancaire selon ses délais et contrôles habituels. Les retraits manuels restent
  disponibles pour les comptes non Connect.

### Configuration Stripe

Définir ces variables d'environnement avant de lancer l'application (ne jamais les mettre
en dur dans le code) :

```bash
export STRIPE_SECRET_KEY="sk_test_..."       # Dashboard Stripe > Développeurs > Clés API
export STRIPE_PUBLISHABLE_KEY="pk_test_..."  # idem (non utilisée côté serveur pour l'instant,
                                              # gardée si vous ajoutez du Stripe Elements plus tard)
export STRIPE_WEBHOOK_SECRET="whsec_..."     # Dashboard Stripe > Webhooks > votre endpoint
```

Sans `STRIPE_SECRET_KEY`, la page « Mon portefeuille » reste accessible mais la recharge par
carte est désactivée (message explicatif affiché).

Pour activer les versements Connect, le même compte Stripe doit être configuré. Chaque
utilisateur renseigne ses informations d'identité et son compte bancaire dans le parcours
Stripe Connect depuis « Mon portefeuille » ; les coordonnées bancaires ne sont jamais
stockées par CoursConnect.

Pour recevoir les webhooks en local, utiliser le CLI Stripe :

```bash
stripe listen --forward-to localhost:5000/stripe/webhook
```

Le webhook (`checkout.session.completed`) sert de filet de sécurité pour créditer le
portefeuille même si l'élève ferme son navigateur avant la redirection vers
`/portefeuille/succes` ; l'idempotence est garantie par l'identifiant de session Stripe
(un même paiement ne peut jamais créditer deux fois).

### Limites connues / pistes d'évolution

- Le solde affiché dans CoursConnect reste un **portefeuille interne**. Le retrait Stripe
  transfère le montant vers le compte Connect de l'utilisateur, puis Stripe le verse sur son
  compte bancaire selon le calendrier de paiement configuré.
- Le remboursement est **automatique et immédiat** dès que l'élève le demande (pas de
  processus d'arbitrage/litige avec le professeur ou l'administrateur).
- Le thread de libération automatique fonctionne pour un déploiement mono-processus
  (`python app.py`, ou `gunicorn` avec un seul worker). Avec plusieurs workers, chacun
  lancerait son propre thread : ça reste fonctionnel (la requête SQL de libération est
  idempotente), mais ce n'est pas optimal. Pour un vrai déploiement en production, préférer
  un job planifié externe (cron, Celery beat...) appelant `run_auto_release_once()`.

## Aller plus loin

- Remplacer `app.secret_key` par une vraie valeur secrète (variable d'environnement) avant
  toute mise en production.
- Passer de SQLite à PostgreSQL pour un usage à plus grande échelle.
- Ajouter une validation d'e-mail et une politique de mot de passe plus stricte.
