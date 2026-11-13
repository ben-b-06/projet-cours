# La Communauté du Savoir

Plateforme de mise en relation entre professeurs particuliers (des étudiants) et élèves du collège
et du lycée. Application **Flask** + **SQLite**, paiements par **Stripe Connect**.

## Installation et lancement en local

```bash
py -m venv venv
venv\Scripts\activate            # Linux / macOS : source venv/bin/activate
pip install -r requirements.txt
set DATA_DIR=donnees              # Linux / macOS : export DATA_DIR=donnees
set SEED_DEMO_DATA=1              # comptes et cours fictifs (jamais en production)
py app.py
```

Puis ouvrir http://127.0.0.1:5000. Avec `SEED_DEMO_DATA=1` sur une base vide, trois comptes de
démonstration sont créés (`camille@coursconnect.fr`, `karim@coursconnect.fr`, `lea@coursconnect.fr`),
mot de passe `Demo-CoursConnect-2026`. Sans cette variable, les anciens comptes de démonstration dont
le mot de passe publié n'a jamais été changé sont désactivés au démarrage.

Le compte administrateur (`ADMIN_EMAIL`, par défaut `admin@cours.fr`) est créé au premier démarrage
avec `ADMIN_PASSWORD`, ou avec un mot de passe aléatoire affiché une seule fois dans la console.

## Comment fonctionnent les paiements

Le site **ne détient jamais d'argent pour le compte de ses utilisateurs**. L'ancien système
(porte-monnaie rechargeable, séquestre de 24 h, demandes de retrait) revenait à recevoir et conserver
des fonds de tiers, activité réservée en France aux établissements de paiement ou de monnaie
électronique agréés (Code monétaire et financier, art. L. 521-2 et L. 525-3). Il est supprimé.

Nouveau fonctionnement :

1. Le professeur active les paiements depuis « Paiements » : il ouvre un compte **Stripe Connect
   Express**. Stripe vérifie son identité et recueille son RIB ; le site ne voit pas ces données.
2. L'élève choisit ses créneaux et paie par carte sur **Stripe Checkout**. Le paiement est une
   *destination charge* avec `on_behalf_of` : il est encaissé par Stripe pour le compte du
   professeur, crédité sur son compte Stripe, puis viré sur son compte bancaire par Stripe.
   Seule la commission (`application_fee_amount`, 10 % par défaut) revient à la plateforme.
3. Les créneaux sont bloqués pendant le paiement (30 minutes). Si le paiement n'aboutit pas, ils sont
   libérés automatiquement (webhook `checkout.session.expired` ou tâche de maintenance).
4. Annulation gratuite jusqu'à 12 h avant la séance, ou à tout moment avant la séance pendant le
   délai légal de rétractation de 14 jours si le professeur est micro-entrepreneur. Le remboursement
   passe par l'API Stripe avec `reverse_transfer` et `refund_application_fee` : le montant est repris
   sur le compte du professeur et la commission est annulée.
5. Pour chaque commande payée, le site génère au nom du professeur une facture (micro-entrepreneur)
   ou un reçu (particulier), un avoir en cas de remboursement, et chaque année un récapitulatif des
   sommes perçues (art. 242 bis du CGI).

Les cours gratuits (prix 0) restent possibles sans Stripe.

### Configuration Stripe

1. Dans le tableau de bord Stripe : activer **Connect**, choisir le type de compte **Express**,
   renseigner le profil de la plateforme (marketplace, France).
2. Créer un point de terminaison webhook vers `https://votre-domaine/stripe/webhook` avec les
   événements : `checkout.session.completed`, `checkout.session.expired`,
   `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`,
   `charge.refunded`, `charge.dispute.created`. Son secret va dans `STRIPE_WEBHOOK_SECRET`.
3. Créer un second point de terminaison sur la même URL, en cochant « événements des comptes
   connectés », avec l'événement `account.updated`. Son secret va dans
   `STRIPE_CONNECT_WEBHOOK_SECRET`.
4. En local : `stripe listen --forward-to localhost:5000/stripe/webhook`.

### Anciens soldes

À la mise à jour, les soldes de l'ancien porte-monnaie, les paiements encore en séquestre et les
demandes de retrait en attente restent visibles dans **Administration > Anciens soldes**
(`/admin/anciens-soldes`). Il faut restituer chaque montant hors du site (remboursement de la
recharge depuis le tableau de bord Stripe, ou virement), puis marquer la ligne comme réglée.
Les élèves concernés voient un message les en informant.

## Variables d'environnement

| Variable | Rôle |
|---|---|
| `FLASK_SECRET_KEY` | Clé de signature des sessions. Si absente, une clé est créée une fois dans `DATA_DIR/.flask_secret_key`. |
| `DATA_DIR` | Dossier de la base et des fichiers envoyés (défaut `/var/data`). |
| `FORCE_HTTPS` | `1` en production : cookies `Secure` et `__Host-`, HSTS. |
| `TRUST_PROXY_HOPS` | Nombre de proxys devant l'appli (défaut 1 si `FORCE_HTTPS=1`, sinon 0). Nécessaire pour la vraie IP des visiteurs. |
| `PUBLIC_BASE_URL` | URL publique, ex. `https://www.exemple.fr` (liens de retour Stripe). |
| `ALLOWED_HOSTS` | Noms d'hôte acceptés, séparés par des virgules. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Compte administrateur créé au premier démarrage. |
| `STRIPE_SECRET_KEY` | Clé secrète Stripe (`sk_live_…` ou `sk_test_…`). |
| `STRIPE_WEBHOOK_SECRET`, `STRIPE_CONNECT_WEBHOOK_SECRET` | Secrets des deux webhooks (voir plus haut). |
| `PLATFORM_FEE_PERCENT` | Commission de la plateforme (défaut 10). |
| `WEBRTC_STUN_URLS` | Serveurs STUN de la visio (défaut : Google). |
| `SEED_DEMO_DATA` | `1` pour créer des données fictives sur une base vide. |
| `SITE_NAME` | Nom affiché (défaut « La Communauté du Savoir »). |
| `LEGAL_*` | Informations légales, voir ci-dessous. |

En production : `gunicorn -w 2 -b 0.0.0.0:$PORT app:app`, derrière HTTPS. Sauvegardez régulièrement
`DATA_DIR` (base SQLite et fichiers envoyés).

## À faire avant d'ouvrir le site au public

Le code met le site en conformité sur ce qui dépend de lui. Les points suivants dépendent de vous :

1. **Avoir une structure juridique** qui exploite la plateforme (association, micro-entreprise, SAS…)
   et renseigner les variables `LEGAL_*` : `LEGAL_EDITOR_NAME`, `LEGAL_EDITOR_FORM`,
   `LEGAL_EDITOR_ADDRESS`, `LEGAL_EDITOR_ID` (SIREN, RCS ou RNA), `LEGAL_PUBLICATION_DIRECTOR`,
   `LEGAL_CONTACT_EMAIL`, `LEGAL_CONTACT_PHONE`, `LEGAL_HOST_NAME`, `LEGAL_HOST_ADDRESS`,
   `LEGAL_HOST_PHONE`, `LEGAL_HOST_LOCATION`, et facultativement `LEGAL_EDITOR_CAPITAL`,
   `LEGAL_EDITOR_VAT`, `LEGAL_PRIVACY_EMAIL`. Tant qu'un champ manque, il apparaît comme
   « [à compléter] » et l'administrateur voit un avertissement.
2. **Adhérer à un médiateur de la consommation** (obligatoire, art. L. 612-1 du Code de la
   consommation) et renseigner `LEGAL_MEDIATOR_NAME`, `LEGAL_MEDIATOR_ADDRESS`, `LEGAL_MEDIATOR_URL`.
3. **Héberger les données dans l'Union européenne** si possible (par exemple la région Francfort de
   votre hébergeur) et l'indiquer dans `LEGAL_HOST_LOCATION`.
4. **Déclaration DAC7** : chaque année avant le 31 janvier, transmettre à la DGFiP les revenus perçus
   par les professeurs (art. 1649 ter A et suivants du CGI). Le récapitulatif annuel du site fournit
   les montants ; il faudra aussi collecter l'adresse, la date de naissance et le numéro fiscal des
   professeurs pour la déclaration.
5. **Facturer votre commission** aux professeurs (comptabilité de votre structure) et souscrire une
   assurance responsabilité civile professionnelle.
6. Tenir un **registre des traitements** (RGPD, art. 30) : la page « Données personnelles » en donne
   le contenu.
7. **Faire relire les CGU, CGV et la politique de confidentialité** par un juriste : elles sont
   rédigées pour ce fonctionnement mais ne remplacent pas un conseil adapté à votre situation.
8. Vérifier l'identité des professeurs et leur demander un extrait de casier judiciaire (bulletin
   n° 3) avant de valider leur profil : les élèves sont en majorité mineurs.

## Sécurité

Mesures en place : politique de sécurité du contenu stricte (aucun script en ligne, aucune ressource
tierce), jeton CSRF sur tous les formulaires et appels `fetch`, cookies `HttpOnly`/`SameSite`/`Secure`,
session limitée à 12 h et invalidée à chaque changement de mot de passe, mots de passe hachés
(scrypt) avec politique conforme à la recommandation CNIL, limitation des tentatives par IP et par
compte, photos et pièces jointes ré-encodées (métadonnées EXIF supprimées) et servies en bac à sable,
accès aux fichiers vérifié, webhooks Stripe signés et idempotents, montants toujours calculés côté
serveur, journal des actions sensibles, données d'appel et de tableau purgées automatiquement.

Limites connues :

- La limitation de débit est en mémoire, par processus : avec plusieurs workers, les seuils sont
  multipliés. Pour un trafic important, utiliser un stockage partagé (Redis).
- Pas de double authentification pour l'administrateur : à ajouter en priorité si le site grossit.
- Pas de réinitialisation de mot de passe par e-mail (aucun envoi d'e-mail) : l'administrateur
  génère un mot de passe provisoire, à changer à la connexion suivante.
- Aucune application n'est garantie sans faille : faites un test d'intrusion avant une ouverture à
  grande échelle et tenez les dépendances à jour (`pip install -U -r requirements.txt`).
