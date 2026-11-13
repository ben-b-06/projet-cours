"""La Communauté du Savoir : plateforme de mise en relation entre professeurs
particuliers et élèves (Flask + SQLite).

Principes de cette version :

* Paiements : la plateforme ne détient jamais l'argent des utilisateurs.
  L'élève paie chaque réservation par carte via Stripe Checkout ; le paiement
  est encaissé par Stripe pour le compte du professeur (Stripe Connect,
  « destination charge » avec on_behalf_of). Seule la commission de la
  plateforme est versée sur le compte Stripe de la plateforme. Il n'y a plus
  de porte-monnaie interne, de séquestre ni de demandes de retrait : la
  détention de fonds pour le compte de tiers est une activité réservée aux
  établissements agréés (Code monétaire et financier, art. L. 521-2 et
  L. 525-3). Les remboursements passent par l'API Stripe (reverse transfer).
* Sécurité : CSP stricte sans script en ligne, jeton CSRF sur toutes les
  requêtes modifiantes (y compris les appels fetch), sessions invalidées au
  changement de mot de passe, téléversements ré-encodés, limitation de débit.
* RGPD : données minimales, durées de conservation appliquées par une tâche
  de maintenance, export et suppression du compte en libre-service.

Configuration : voir README.md (variables d'environnement).
"""

import calendar as calendar_mod
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time as time_mod
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from io import BytesIO
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

import stripe
from flask import (
    Flask,
    abort,
    flash,
    g,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from PIL import Image, ImageOps, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "oui")


def env_str(name, default=""):
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


# ---------------------------------------------------------------------------
# Chemins et réglages généraux
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = env_str("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "coursconnect.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
PROFILE_PHOTOS_DIR = os.path.join(UPLOADS_DIR, "profils")
MESSAGE_ATTACHMENTS_DIR = os.path.join(UPLOADS_DIR, "messagerie")

PARIS = ZoneInfo("Europe/Paris")

# FORCE_HTTPS=1 en production (site servi en HTTPS) : cookies Secure, HSTS.
FORCE_HTTPS = env_flag("FORCE_HTTPS")
# Nombre de proxys de confiance devant l'application (1 sur Render, Heroku,
# derrière un Nginx…). Indispensable pour connaître la vraie IP du client
# (limitation de débit) et le schéma https.
# Par défaut : 1 si FORCE_HTTPS=1 (le HTTPS est presque toujours terminé par un proxy).
TRUST_PROXY_HOPS = int(env_str("TRUST_PROXY_HOPS", "1" if FORCE_HTTPS else "0") or 0)
# URL publique du site (ex. https://www.exemple.fr), utilisée pour les liens
# de retour Stripe. Évite de dépendre de l'en-tête Host envoyé par le client.
PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL").rstrip("/")
# Noms d'hôte acceptés (ex. "www.exemple.fr,exemple.fr"). Vide = pas de contrôle.
ALLOWED_HOSTS = [h.strip() for h in env_str("ALLOWED_HOSTS").split(",") if h.strip()]
# Données de démonstration (comptes et cours fictifs) : jamais en production.
DEMO_MODE = env_flag("SEED_DEMO_DATA")

SITE_NAME = env_str("SITE_NAME", "La Communauté du Savoir")

app = Flask(__name__)
if TRUST_PROXY_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUST_PROXY_HOPS, x_proto=TRUST_PROXY_HOPS)
if ALLOWED_HOSTS:
    app.config["TRUSTED_HOSTS"] = ALLOWED_HOSTS


def _load_secret_key():
    """Clé de signature des sessions. Si FLASK_SECRET_KEY n'est pas fournie,
    une clé aléatoire est créée une seule fois dans DATA_DIR (droits 600) :
    elle reste identique entre redémarrages et entre workers gunicorn."""
    from_env = os.environ.get("FLASK_SECRET_KEY")
    if from_env:
        return from_env
    path = os.path.join(DATA_DIR, ".flask_secret_key")
    for _ in range(50):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, encoding="ascii") as fh:
                key = fh.read().strip()
            if key:
                return key
            time_mod.sleep(0.05)  # un autre worker est en train de l'écrire
            continue
        key = secrets.token_hex(32)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(key)
        return key
    raise RuntimeError("Impossible de lire ou de créer la clé secrète de session.")


app.secret_key = _load_secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=9 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=FORCE_HTTPS,
    # Le préfixe __Host- interdit qu'un sous-domaine pose ou écrase le cookie.
    SESSION_COOKIE_NAME="__Host-session" if FORCE_HTTPS else "session",
    WTF_CSRF_TIME_LIMIT=None,
)
csrf = CSRFProtect(app)

# ---------------------------------------------------------------------------
# Informations légales (mentions légales, CGU, CGV, confidentialité)
# ---------------------------------------------------------------------------
# À renseigner par variables d'environnement (ou en modifiant les valeurs par
# défaut ci-dessous). Tant qu'un champ obligatoire vaut TODO, il s'affiche
# tel quel sur le site et l'administrateur voit un avertissement.
TODO = "[à compléter]"
LEGAL = {
    "site_name": SITE_NAME,
    "editor_name": env_str("LEGAL_EDITOR_NAME", TODO),
    "editor_form": env_str("LEGAL_EDITOR_FORM", TODO),
    "editor_address": env_str("LEGAL_EDITOR_ADDRESS", TODO),
    "editor_id": env_str("LEGAL_EDITOR_ID", TODO),  # SIREN/SIRET, RCS ou n° RNA
    "editor_capital": env_str("LEGAL_EDITOR_CAPITAL"),
    "editor_vat": env_str("LEGAL_EDITOR_VAT"),
    "publication_director": env_str("LEGAL_PUBLICATION_DIRECTOR", TODO),
    "contact_email": env_str("LEGAL_CONTACT_EMAIL", "lacommunautedusavoir1@gmail.com"),
    "contact_phone": env_str("LEGAL_CONTACT_PHONE", TODO),
    "privacy_email": env_str("LEGAL_PRIVACY_EMAIL", env_str("LEGAL_CONTACT_EMAIL", "lacommunautedusavoir1@gmail.com")),
    "host_name": env_str("LEGAL_HOST_NAME", TODO),
    "host_address": env_str("LEGAL_HOST_ADDRESS", TODO),
    "host_phone": env_str("LEGAL_HOST_PHONE", TODO),
    "host_location": env_str("LEGAL_HOST_LOCATION", TODO),  # ex. « Union européenne (Francfort) »
    "mediator_name": env_str("LEGAL_MEDIATOR_NAME", TODO),
    "mediator_address": env_str("LEGAL_MEDIATOR_ADDRESS", TODO),
    "mediator_url": env_str("LEGAL_MEDIATOR_URL", TODO),
}
LEGAL_MISSING = [k for k, v in LEGAL.items() if v == TODO]
TERMS_VERSION = "2026-11-13"
TERMS_DATE = "13 novembre 2026"

# ---------------------------------------------------------------------------
# Référentiels
# ---------------------------------------------------------------------------
MATIERES = ["Mathématiques", "Physique-Chimie", "SI", "Français", "SVT", "SES", "Histoire-Géographie"]
NIVEAUX = ["6e", "5e", "4e", "3e", "2nde", "1re", "Terminale"]
NIVEAUX_ETUDE_PROF = [
    "Baccalauréat", "Prépa MP", "Prépa MPI", "Prépa PC", "Prépa PSI", "Prépa PT", "Prépa TSI",
    "Prépa ECG", "Prépa littéraire", "Licence (Bac+3)", "Master (Bac+5)", "Doctorat",
    "École d'ingénieur", "École de commerce", "Autre",
]
MODES = ["en_ligne", "presentiel", "les_deux"]
MODE_LABELS = {"en_ligne": "En ligne", "presentiel": "En présentiel", "les_deux": "En ligne ou en présentiel"}

# Statut déclaré par le professeur (information obligatoire des plateformes,
# art. L. 111-7 du Code de la consommation : l'élève doit savoir s'il traite
# avec un professionnel ou avec un particulier).
TEACHER_STATUSES = {
    "particulier": "Particulier (activité non professionnelle)",
    "micro": "Micro-entrepreneur (franchise en base de TVA)",
}
PRO_STATUSES = {"micro"}

# ---------------------------------------------------------------------------
# Paiements (Stripe Connect)
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = env_str("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env_str("STRIPE_WEBHOOK_SECRET")
STRIPE_CONNECT_WEBHOOK_SECRET = env_str("STRIPE_CONNECT_WEBHOOK_SECRET")
stripe.api_key = STRIPE_SECRET_KEY

PLATFORM_FEE_PERCENT = Decimal(env_str("PLATFORM_FEE_PERCENT", "10"))
PLATFORM_FEE_BPS = int(PLATFORM_FEE_PERCENT * 100)  # 10 % -> 1000 points de base
PRICE_MIN_CENTS = 500  # en dessous, les frais Stripe dépassent la commission
PRICE_MAX_CENTS = 50000
CANCELLATION_MIN_DELAY = timedelta(hours=12)
WITHDRAWAL_PERIOD = timedelta(days=14)
BOOKING_MIN_NOTICE = timedelta(hours=1)
CHECKOUT_TTL = timedelta(minutes=31)  # Stripe impose au moins 30 minutes
MAX_SLOTS_PER_BOOKING = 20
MAX_SLOTS_PER_COURSE = 60

# Limites de longueur des champs texte (anti-abus, base de données bornée).
LIMITS = {
    "name": 80, "email": 254, "title": 120, "description": 3000, "bio": 1500,
    "notes": 500, "message": 4000, "city": 80, "report": 3000, "admin_note": 500,
}
# Mots interdits dans les noms d'utilisateur (usurpation de l'équipe).
RESERVED_NAME_WORDS = ("admin", "modérat", "moderat", "équipe", "equipe", "support", SITE_NAME.lower())
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# Durées de conservation (RGPD) appliquées par la tâche de maintenance.
RETENTION_SIGNALS = timedelta(days=1)
RETENTION_WHITEBOARD = timedelta(days=30)
RETENTION_AUDIT_LOG = timedelta(days=365)
RETENTION_INACTIVE_ACCOUNT = timedelta(days=3 * 365)
MAINTENANCE_INTERVAL_SECONDS = 60

# Serveurs STUN utilisés par la visioconférence (découverte d'adresse réseau).
WEBRTC_STUN_URLS = [u.strip() for u in env_str("WEBRTC_STUN_URLS", "stun:stun.l.google.com:19302").split(",") if u.strip()]

# Images : ré-encodées à l'envoi (supprime les métadonnées EXIF, dont la
# géolocalisation, et neutralise les fichiers piégés).
Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS_ACCEPTED = 25_000_000
ATTACHMENT_IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "png"}
PROFILE_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}

DEMO_ACCOUNTS = {
    "camille@coursconnect.fr": "prof123",
    "karim@coursconnect.fr": "prof123",
    "lea@coursconnect.fr": "etu123",
}
DEMO_PASSWORD = "Demo-CoursConnect-2026"  # utilisé seulement si SEED_DEMO_DATA=1

ADMIN_EMAIL = env_str("ADMIN_EMAIL", "admin@cours.fr").lower()


# ---------------------------------------------------------------------------
# En-têtes de sécurité
# ---------------------------------------------------------------------------
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self' https://checkout.stripe.com https://connect.stripe.com",
    "frame-ancestors 'none'",
] + (["upgrade-insecure-requests"] if FORCE_HTTPS else []))


@app.after_request
def set_security_headers(response):
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    headers.setdefault(
        "Permissions-Policy",
        "camera=(self), microphone=(self), geolocation=(), payment=(), usb=(), interest-cohort=()",
    )
    if FORCE_HTTPS:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Pages personnelles : jamais en cache (poste partagé, bouton « précédent »).
    if session.get("user_id") and response.mimetype in ("text/html", "application/json"):
        headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Limitation de débit (en mémoire, par processus)
# ---------------------------------------------------------------------------
_rate_limit_lock = threading.Lock()
_rate_limit_buckets = {}


def client_ip():
    return request.remote_addr or "inconnue"


def is_rate_limited(key, max_attempts, window_seconds):
    """True si `key` a dépassé max_attempts sur la fenêtre glissante ;
    sinon enregistre la tentative et renvoie False."""
    now = time_mod.time()
    cutoff = now - window_seconds
    with _rate_limit_lock:
        bucket = _rate_limit_buckets.setdefault(key, [])
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= max_attempts:
            return True
        bucket.append(now)
        if len(_rate_limit_buckets) > 10000:
            for k in [k for k, v in _rate_limit_buckets.items() if not v or v[-1] < cutoff]:
                _rate_limit_buckets.pop(k, None)
        return False


# ---------------------------------------------------------------------------
# Outils divers
# ---------------------------------------------------------------------------
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean(value, max_len, multiline=False):
    """Nettoie un champ texte : caractères de contrôle retirés, espaces
    superflus supprimés, longueur bornée."""
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_CHARS.sub("", value)
    if not multiline:
        value = " ".join(value.split())
    return value.strip()[:max_len]


def form_value(name, limit_key, multiline=False):
    return clean(request.form.get(name, ""), LIMITS[limit_key], multiline)


def now_local():
    """Heure légale française (les créneaux sont saisis en heure de Paris)."""
    return datetime.now(PARIS).replace(tzinfo=None)


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_str():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def parse_db_utc(value):
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def parse_euros(raw):
    """'25', '25,5', '25.50' -> centimes (int). Lève ValueError sinon."""
    raw = (raw or "").strip().replace("€", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d{1,5}(\.\d{1,2})?", raw):
        raise ValueError("montant invalide")
    return int((Decimal(raw) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def platform_fee(amount_cents):
    return (amount_cents * PLATFORM_FEE_BPS + 5000) // 10000


def password_problem(password):
    """Politique conforme à la recommandation CNIL n° 2022-100 (12 caractères,
    3 types de caractères au moins, combinée à la limitation des tentatives)."""
    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        return f"Le mot de passe doit contenir entre {MIN_PASSWORD_LENGTH} et {MAX_PASSWORD_LENGTH} caractères."
    kinds = sum([
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"\d", password)),
        bool(re.search(r"[^a-zA-Z\d]", password)),
    ])
    if kinds < 3:
        return "Le mot de passe doit mélanger au moins trois types de caractères : minuscules, majuscules, chiffres, caractères spéciaux."
    return None


def siret_is_valid(siret):
    if not re.fullmatch(r"\d{14}", siret or ""):
        return False
    total = 0
    for i, ch in enumerate(reversed(siret)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0 or siret.startswith("356000000")  # exception La Poste


def slot_start(slot):
    return datetime.strptime(f"{slot['slot_date']} {str(slot['slot_time'])[:5]}", "%Y-%m-%d %H:%M")


def slot_end(slot):
    return slot_start(slot) + timedelta(minutes=int(slot["duration_minutes"]))


def safe_slot_start(slot):
    try:
        return slot_start(slot)
    except (TypeError, ValueError, KeyError):
        return datetime.min


def safe_slot_end(slot):
    try:
        return slot_end(slot)
    except (TypeError, ValueError, KeyError):
        return datetime.min


def redirect_back(default):
    """Retour à la page précédente, uniquement si elle est sur ce site."""
    ref = request.referrer
    if ref:
        parsed = urlparse(ref)
        if parsed.scheme in ("http", "https") and parsed.netloc == request.host and parsed.path.startswith("/"):
            return redirect(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
    return redirect(default)


def external_url(endpoint, **values):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


def to_plain(obj):
    """Objets Stripe -> dict Python (StripeObject n'est plus un dict)."""
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------
def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def table_columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]


def add_column(db, table, column, ddl):
    if column not in table_columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'prof', 'etudiant')),
    bio TEXT,
    photo TEXT,
    approved INTEGER NOT NULL DEFAULT 1,
    stripe_account_id TEXT
);

CREATE TABLE IF NOT EXISTS courses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    subject TEXT NOT NULL,
    level TEXT NOT NULL,
    description TEXT NOT NULL,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'en_ligne' CHECK(mode IN ('en_ligne', 'presentiel', 'les_deux')),
    city TEXT
);

CREATE TABLE IF NOT EXISTS slots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    slot_date TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    reserved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    student_notes TEXT,
    slot_mode TEXT
);

CREATE TABLE IF NOT EXISTS contacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(student_id, teacher_id)
);

CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at TEXT,
    attachment_filename TEXT,
    attachment_original_name TEXT,
    attachment_type TEXT
);

CREATE TABLE IF NOT EXISTS signals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS whiteboard_strokes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'stroke',
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ratings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(student_id, teacher_id)
);

CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Commandes : une réservation payée (ou gratuite) d'un ou plusieurs
-- créneaux d'un même cours. Les informations utiles aux justificatifs sont
-- copiées (nom, statut du professeur…) pour rester exactes même si un
-- compte est supprimé : les pièces comptables se conservent 10 ans.
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    course_title TEXT NOT NULL,
    student_name TEXT NOT NULL,
    student_email TEXT NOT NULL,
    teacher_name TEXT NOT NULL,
    teacher_status TEXT,
    teacher_siret TEXT,
    amount_cents INTEGER NOT NULL,
    platform_fee_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'paid', 'partially_refunded', 'refunded', 'expired', 'canceled', 'free')),
    stripe_checkout_session_id TEXT UNIQUE,
    stripe_payment_intent_id TEXT,
    stripe_account_id TEXT,
    invoice_number TEXT,
    waiver_accepted_at TEXT,
    terms_version TEXT,
    disputed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS order_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    slot_id INTEGER REFERENCES slots(id) ON DELETE SET NULL,
    slot_date TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    slot_mode TEXT,
    amount_cents INTEGER NOT NULL,
    platform_fee_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'paid', 'refunded', 'canceled', 'free')),
    stripe_refund_id TEXT,
    credit_note_number TEXT,
    refunded_at TEXT,
    refund_reason TEXT
);

-- Numérotation continue des factures/reçus et avoirs, par professeur.
CREATE TABLE IF NOT EXISTS doc_sequences(
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- Événements Stripe déjà traités (les webhooks peuvent être rejoués).
CREATE TABLE IF NOT EXISTS stripe_events(
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Signalements (contenus illicites, litiges sur une séance) : dispositif de
-- notification exigé par le règlement (UE) 2022/2065 (DSA), art. 16.
CREATE TABLE IF NOT EXISTS reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reporter_name TEXT,
    reporter_email TEXT,
    category TEXT NOT NULL,
    location TEXT,
    description TEXT NOT NULL,
    order_item_id INTEGER REFERENCES order_items(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
    admin_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

-- Journal des actions sensibles (administration, paiements, accès aux
-- conversations). Conservé un an.
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    ip TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ===== Ancien système de porte-monnaie (lecture seule) =====
-- Conservé pour l'historique et pour solder les montants restants.
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    teacher_amount_cents INTEGER,
    commission_cents INTEGER,
    status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held', 'released', 'refunded')),
    release_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS wallet_transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('topup', 'hold', 'release', 'refund', 'withdrawal')),
    description TEXT,
    stripe_session_id TEXT,
    payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    method TEXT NOT NULL,
    details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'paid', 'rejected')),
    admin_note TEXT,
    wallet_transaction_id INTEGER REFERENCES wallet_transactions(id) ON DELETE SET NULL,
    stripe_transfer_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_slots_course ON slots(course_id);
CREATE INDEX IF NOT EXISTS idx_slots_reserved ON slots(reserved_by);
CREATE INDEX IF NOT EXISTS idx_courses_teacher ON courses(teacher_id);
CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages(sender_id, recipient_id);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(recipient_id, read_at);
CREATE INDEX IF NOT EXISTS idx_signals_lookup ON signals(course_id, recipient_id, id);
CREATE INDEX IF NOT EXISTS idx_whiteboard_slot ON whiteboard_strokes(slot_id, id);
CREATE INDEX IF NOT EXISTS idx_orders_student ON orders(student_id);
CREATE INDEX IF NOT EXISTS idx_orders_teacher ON orders(teacher_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_slot ON order_items(slot_id);
"""


def migrate_legacy_schema(db):
    """Migrations héritées des versions précédentes (bases existantes)."""
    add_column(db, "users", "bio", "TEXT")
    add_column(db, "users", "photo", "TEXT")
    add_column(db, "users", "approved", "INTEGER NOT NULL DEFAULT 1")
    add_column(db, "users", "education_level", "TEXT")
    add_column(db, "users", "wallet_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column(db, "users", "stripe_account_id", "TEXT")
    add_column(db, "payments", "teacher_amount_cents", "INTEGER")
    add_column(db, "payments", "commission_cents", "INTEGER")
    add_column(db, "messages", "attachment_filename", "TEXT")
    add_column(db, "messages", "attachment_original_name", "TEXT")
    add_column(db, "messages", "attachment_type", "TEXT")
    add_column(db, "courses", "price_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column(db, "courses", "mode", "TEXT NOT NULL DEFAULT 'en_ligne'")
    add_column(db, "courses", "city", "TEXT")
    add_column(db, "slots", "reserved_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL")
    add_column(db, "slots", "student_notes", "TEXT")
    add_column(db, "slots", "slot_mode", "TEXT")

    # Ancienne colonne slots.reserved_by sans ON DELETE : on reconstruit la
    # table sans la renommer d'abord (un RENAME réécrirait les FK enfants).
    row = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'slots'").fetchone()
    if row and "reserved_by INTEGER REFERENCES users(id) ON DELETE SET NULL" not in row[0]:
        db.executescript(
            """
            CREATE TABLE slots_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                reserved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                student_notes TEXT,
                slot_mode TEXT
            );
            INSERT INTO slots_new(id, course_id, slot_date, slot_time, duration_minutes, reserved_by, student_notes, slot_mode)
            SELECT id, course_id, slot_date, slot_time, duration_minutes, reserved_by, student_notes, slot_mode FROM slots;
            DROP TABLE slots;
            ALTER TABLE slots_new RENAME TO slots;
            """
        )

    # Très anciennes bases : réservation au niveau du cours entier.
    course_cols = table_columns(db, "courses")
    if "reserved_by" in course_cols:
        for c in db.execute("SELECT id, reserved_by, student_notes FROM courses WHERE reserved_by IS NOT NULL").fetchall():
            db.execute(
                "UPDATE slots SET reserved_by = ?, student_notes = ? WHERE course_id = ? AND reserved_by IS NULL",
                (c[1], c[2], c[0]),
            )
        db.execute("ALTER TABLE courses DROP COLUMN reserved_by")
    if "student_notes" in table_columns(db, "courses"):
        db.execute("ALTER TABLE courses DROP COLUMN student_notes")

    row = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wallet_transactions'").fetchone()
    if row and "withdrawal" not in row[0]:
        db.executescript(
            """
            CREATE TABLE wallet_transactions_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount_cents INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('topup', 'hold', 'release', 'refund', 'withdrawal')),
                description TEXT,
                stripe_session_id TEXT,
                payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO wallet_transactions_new(id, user_id, amount_cents, kind, description, stripe_session_id, payment_id, created_at)
            SELECT id, user_id, amount_cents, kind, description, stripe_session_id, payment_id, created_at FROM wallet_transactions;
            DROP TABLE wallet_transactions;
            ALTER TABLE wallet_transactions_new RENAME TO wallet_transactions;
            """
        )

    row = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'withdrawals'").fetchone()
    if row and "wallet_transactions_old" in row[0]:
        db.executescript(
            """
            CREATE TABLE withdrawals_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount_cents INTEGER NOT NULL,
                method TEXT NOT NULL,
                details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'paid', 'rejected')),
                admin_note TEXT,
                wallet_transaction_id INTEGER REFERENCES wallet_transactions(id) ON DELETE SET NULL,
                stripe_transfer_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            INSERT INTO withdrawals_new(id, user_id, amount_cents, method, details, status, admin_note, wallet_transaction_id, stripe_transfer_id, created_at, resolved_at)
            SELECT id, user_id, amount_cents, method, details, status, admin_note, wallet_transaction_id, NULL, created_at, resolved_at FROM withdrawals;
            DROP TABLE withdrawals;
            ALTER TABLE withdrawals_new RENAME TO withdrawals;
            """
        )
    add_column(db, "withdrawals", "stripe_transfer_id", "TEXT")


def migrate_schema(db):
    """Colonnes ajoutées par cette version."""
    for column, ddl in [
        ("teacher_status", "TEXT"),
        ("siret", "TEXT"),
        ("stripe_charges_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("stripe_payouts_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("stripe_details_submitted", "INTEGER NOT NULL DEFAULT 0"),
        ("cgu_accepted_at", "TEXT"),
        ("cgu_version", "TEXT"),
        ("created_at", "TEXT"),
        ("last_login_at", "TEXT"),
        ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
        ("deleted_at", "TEXT"),
        ("legacy_settled_at", "TEXT"),
    ]:
        add_column(db, "users", column, ddl)
    db.execute("UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    db.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE last_login_at IS NULL")

    add_column(db, "slots", "reservation_status", "TEXT")
    add_column(db, "slots", "pending_until", "TEXT")
    add_column(db, "slots", "reserved_at", "TEXT")
    db.execute(
        "UPDATE slots SET reservation_status = 'confirmed' WHERE reserved_by IS NOT NULL AND reservation_status IS NULL"
    )


def init_db():
    db = connect_db()
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    migrate_legacy_schema(db)
    migrate_schema(db)
    db.executescript(INDEXES)

    if not db.execute("SELECT 1 FROM settings WHERE key = 'registration_mode'").fetchone():
        db.execute("INSERT INTO settings(key, value) VALUES ('registration_mode', 'tous')")

    os.makedirs(PROFILE_PHOTOS_DIR, exist_ok=True)
    os.makedirs(MESSAGE_ATTACHMENTS_DIR, exist_ok=True)

    if not db.execute("SELECT 1 FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone():
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if not admin_password:
            admin_password = secrets.token_urlsafe(18)
            print(
                "=" * 70 + "\nCompte administrateur créé : " + ADMIN_EMAIL + "\n"
                f"Mot de passe généré (affiché une seule fois) : {admin_password}\n"
                "Changez-le dès la première connexion, ou définissez ADMIN_PASSWORD.\n" + "=" * 70,
                flush=True,
            )
        db.execute(
            "INSERT INTO users(name, email, password_hash, role, created_at, last_login_at) "
            "VALUES (?, ?, ?, 'admin', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("Administrateur", ADMIN_EMAIL, generate_password_hash(admin_password)),
        )

    if DEMO_MODE:
        if db.execute("SELECT COUNT(*) FROM users WHERE role != 'admin'").fetchone()[0] == 0:
            seed_demo_data(db)
    else:
        lock_demo_accounts(db)

    db.commit()
    db.close()


def lock_demo_accounts(db):
    """Les comptes de démonstration créés par les anciennes versions avaient
    des mots de passe publiés dans le README : s'ils n'ont jamais été
    changés, on les rend inutilisables (mot de passe aléatoire)."""
    for email, old_password in DEMO_ACCOUNTS.items():
        row = db.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if row and check_password_hash(row["password_hash"], old_password):
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(secrets.token_urlsafe(32)), row["id"]),
            )
            print(f"Compte de démonstration {email} désactivé (mot de passe public).", flush=True)


def seed_demo_data(db):
    start = date.today() + timedelta(days=21)
    profs = [
        ("Camille Dubois", "camille@coursconnect.fr", "prof",
         "Professeure de mathématiques et de physique-chimie, je prépare au brevet et aux révisions de fin d'année."),
        ("Karim Haddad", "karim@coursconnect.fr", "prof",
         "J'aide les lycéens à structurer leur méthode pour la dissertation et les épreuves de SES."),
        ("Léa Marchand", "lea@coursconnect.fr", "etudiant", None),
    ]
    ids = {}
    for name, email, role, bio in profs:
        cur = db.execute(
            "INSERT INTO users(name, email, password_hash, role, bio, teacher_status, cgu_accepted_at, cgu_version, created_at, last_login_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (name, email, generate_password_hash(DEMO_PASSWORD), role, bio,
             "particulier" if role == "prof" else None, TERMS_VERSION),
        )
        ids[email] = cur.lastrowid
    courses = [
        ("Préparer le brevet de mathématiques", "Mathématiques", "3e",
         "Révisions ciblées sur le programme de troisième, avec des exercices type brevet.",
         ids["camille@coursconnect.fr"], [(0, "17:00", 60), (7, "17:00", 60)]),
        ("Dissertation et analyse de texte", "Français", "2nde",
         "Méthode de la dissertation et de l'analyse littéraire pour la seconde.",
         ids["karim@coursconnect.fr"], [(1, "16:30", 60), (8, "16:30", 60)]),
    ]
    for title, subject, level, desc, teacher_id, slots in courses:
        cur = db.execute(
            "INSERT INTO courses(title, subject, level, description, teacher_id, price_cents) VALUES (?, ?, ?, ?, ?, 0)",
            (title, subject, level, desc, teacher_id),
        )
        for offset, slot_time, duration in slots:
            db.execute(
                "INSERT INTO slots(course_id, slot_date, slot_time, duration_minutes) VALUES (?, ?, ?, ?)",
                (cur.lastrowid, (start + timedelta(days=offset)).isoformat(), slot_time, duration),
            )
    print(f"Données de démonstration créées (mot de passe des comptes : {DEMO_PASSWORD}).", flush=True)


# ---------------------------------------------------------------------------
# Journal d'audit
# ---------------------------------------------------------------------------
def audit(db, action, target=None, details=None, actor_id=None):
    ip = None
    if has_request_context():
        ip = client_ip()
        if actor_id is None:
            user = g.get("current_user")
            actor_id = user["id"] if user else None
    db.execute(
        "INSERT INTO audit_log(actor_id, action, target, details, ip) VALUES (?, ?, ?, ?, ?)",
        (actor_id, action, target, (details or "")[:1000] or None, ip),
    )


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))


def auth_fingerprint(password_hash):
    """Empreinte du mot de passe stockée en session : un changement de mot de
    passe invalide immédiatement toutes les autres sessions ouvertes."""
    return hmac.new(app.secret_key.encode(), password_hash.encode(), hashlib.sha256).hexdigest()[:32]


SESSION_MAX_AGE_SECONDS = 12 * 3600


def login_user(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["auth_fp"] = auth_fingerprint(user["password_hash"])
    session["login_at"] = int(time_mod.time())
    db = get_db()
    db.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
    db.commit()


def current_user():
    if "current_user" in g:
        return g.current_user
    user = None
    user_id = session.get("user_id")
    if user_id:
        row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        login_age = time_mod.time() - float(session.get("login_at") or 0)
        if (
            row
            and not row["deleted_at"]
            and 0 <= login_age <= SESSION_MAX_AGE_SECONDS  # durée absolue, même si l'utilisateur reste actif
            and hmac.compare_digest(str(session.get("auth_fp", "")), auth_fingerprint(row["password_hash"]))
        ):
            user = row
        else:
            session.clear()
    g.current_user = user
    return user


def refresh_current_user():
    g.pop("current_user", None)
    return current_user()


ROLE_MESSAGES = {
    ("prof",): "Connectez-vous avec un compte professeur pour accéder à cette page.",
    ("etudiant",): "Connectez-vous avec un compte élève pour accéder à cette page.",
    ("admin",): "Cette page est réservée à l'administration.",
}


def login_required(role=None):
    allowed = None if role is None else ((role,) if isinstance(role, str) else tuple(role))

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or (allowed and user["role"] not in allowed):
                flash(ROLE_MESSAGES.get(allowed, "Connectez-vous pour accéder à cette page."), "error")
                return redirect(url_for("connexion"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


PUBLIC_ENDPOINTS = {
    "static", "parametres", "deconnexion", "mentions_legales", "cgu", "cgv",
    "confidentialite", "presentation", "index",
}


@app.before_request
def enforce_password_change():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint == "stripe_webhook":
        return None
    user = current_user()
    if user and user["must_change_password"]:
        flash("Choisissez un nouveau mot de passe pour continuer.", "error")
        return redirect(url_for("parametres"))
    return None


def dashboard_url_for(role):
    if role == "admin":
        return url_for("admin_dashboard")
    if role == "prof":
        return url_for("prof_dashboard")
    return url_for("etudiant")


# ---------------------------------------------------------------------------
# Filtres et variables de gabarits
# ---------------------------------------------------------------------------
MOIS_FR = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]
MOIS_FR_CAP = [m.capitalize() for m in MOIS_FR]
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
JOURS_COURTS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


@app.template_filter("euros")
def euros(cents):
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    whole, rest = divmod(cents, 100)
    whole_str = f"{whole:,}".replace(",", " ")
    return f"{sign}{whole_str},{rest:02d} €"


@app.template_filter("duree")
def duree(minutes):
    h, m = divmod(int(minutes or 0), 60)
    if h and m:
        return f"{h} h {m:02d}"
    if h:
        return f"{h} h"
    return f"{m} min"


@app.template_filter("date_fr")
def date_fr(value):
    """Horodatage UTC de la base -> date et heure de Paris lisibles."""
    dt = parse_db_utc(value)
    if not dt:
        return value or ""
    local = dt.replace(tzinfo=timezone.utc).astimezone(PARIS)
    return local.strftime("%d/%m/%Y à %H:%M")


@app.template_filter("jour_fr")
def jour_fr(iso_date):
    try:
        d = date.fromisoformat(str(iso_date))
    except ValueError:
        return iso_date
    return f"{JOURS_FR[d.weekday()]} {d.day} {MOIS_FR[d.month]} {d.year}"


@app.template_filter("creneau_fr")
def creneau_fr(slot):
    return f"{jour_fr(slot['slot_date'])} à {str(slot['slot_time'])[:5].replace(':', ' h ')}"


@app.template_global("seance_terminee")
def seance_terminee(slot):
    return safe_slot_end(slot) <= now_local()


@app.template_global("seance_commencee")
def seance_commencee(slot):
    return safe_slot_start(slot) <= now_local()


@app.context_processor
def inject_globals():
    user = current_user()
    ctx = {
        "current_user": user,
        "LEGAL": LEGAL,
        "SITE_NAME": SITE_NAME,
        "TEACHER_STATUSES": TEACHER_STATUSES,
        "PRO_STATUSES": PRO_STATUSES,
        "PLATFORM_FEE_PERCENT": PLATFORM_FEE_PERCENT,
        "TERMS_DATE": TERMS_DATE,
        "MODE_LABELS": MODE_LABELS,
        "unread_messages_total": 0,
        "open_reports_total": 0,
        "legal_missing": [],
    }
    if user:
        db = get_db()
        ctx["unread_messages_total"] = db.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient_id = ? AND read_at IS NULL", (user["id"],)
        ).fetchone()[0]
        if user["role"] == "admin":
            ctx["open_reports_total"] = db.execute("SELECT COUNT(*) FROM reports WHERE status = 'open'").fetchone()[0]
            ctx["legal_missing"] = LEGAL_MISSING
    return ctx


# ---------------------------------------------------------------------------
# Téléversements (photos de profil, pièces jointes)
# ---------------------------------------------------------------------------
class UploadError(ValueError):
    pass


def _read_upload(file_storage):
    data = file_storage.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("Le fichier dépasse la taille maximale autorisée (8 Mo).")
    if not data:
        raise UploadError("Le fichier est vide.")
    return data


def _open_image(data, allowed_formats):
    try:
        img = Image.open(BytesIO(data))
        if img.format not in allowed_formats:
            raise UploadError("Format d'image non pris en charge.")
        if img.width * img.height > MAX_IMAGE_PIXELS_ACCEPTED:
            raise UploadError("Image trop grande (25 mégapixels maximum).")
        img.load()
        return img
    except UploadError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise UploadError("Ce fichier n'est pas une image valide.") from None


def save_profile_photo(file_storage):
    """Ré-encode la photo en JPEG 512 px max, sans métadonnées. Renvoie le nom de fichier."""
    img = _open_image(_read_upload(file_storage), PROFILE_IMAGE_FORMATS)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    else:
        img = img.convert("RGB")
    img.thumbnail((512, 512))
    filename = f"{secrets.token_hex(16)}.jpg"
    os.makedirs(PROFILE_PHOTOS_DIR, exist_ok=True)
    img.save(os.path.join(PROFILE_PHOTOS_DIR, filename), "JPEG", quality=85, optimize=True)
    return filename


def save_message_attachment(file_storage):
    """Images (ré-encodées, sans métadonnées) ou PDF. Renvoie (nom stocké, nom d'origine, type)."""
    original = clean(file_storage.filename or "fichier", 120)
    data = _read_upload(file_storage)
    os.makedirs(MESSAGE_ATTACHMENTS_DIR, exist_ok=True)
    if data[:5] == b"%PDF-":
        stored = f"{secrets.token_hex(16)}.pdf"
        with open(os.path.join(MESSAGE_ATTACHMENTS_DIR, stored), "wb") as fh:
            fh.write(data)
        return stored, original, "pdf"
    img = _open_image(data, set(ATTACHMENT_IMAGE_FORMATS))
    ext = ATTACHMENT_IMAGE_FORMATS[img.format]
    img = ImageOps.exif_transpose(img)
    img.thumbnail((2000, 2000))
    stored = f"{secrets.token_hex(16)}.{ext}"
    path = os.path.join(MESSAGE_ATTACHMENTS_DIR, stored)
    if ext == "jpg":
        img.convert("RGB").save(path, "JPEG", quality=88)
    elif ext == "webp":
        img.save(path, "WEBP", quality=88)
    else:
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGBA")
        img.save(path, "PNG", optimize=True)
    return stored, original, "image"


def delete_file_quietly(directory, filename):
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return
    try:
        os.remove(os.path.join(directory, filename))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Requêtes communes
# ---------------------------------------------------------------------------
def get_slots_map(db, course_ids):
    if not course_ids:
        return {}
    placeholders = ",".join("?" for _ in course_ids)
    rows = db.execute(
        f"""
        SELECT sl.*, u.name AS student_name
        FROM slots sl LEFT JOIN users u ON u.id = sl.reserved_by
        WHERE sl.course_id IN ({placeholders})
        ORDER BY sl.slot_date, sl.slot_time
        """,
        list(course_ids),
    ).fetchall()
    slots_map = {cid: [] for cid in course_ids}
    for r in rows:
        slots_map[r["course_id"]].append(r)
    return slots_map


def get_teacher_ratings_map(db, teacher_ids):
    teacher_ids = sorted({t for t in teacher_ids if t is not None})
    if not teacher_ids:
        return {}
    placeholders = ",".join("?" for _ in teacher_ids)
    rows = db.execute(
        f"SELECT teacher_id, AVG(rating) AS avg_rating, COUNT(*) AS nb FROM ratings "
        f"WHERE teacher_id IN ({placeholders}) GROUP BY teacher_id",
        teacher_ids,
    ).fetchall()
    return {r["teacher_id"]: {"avg": r["avg_rating"], "count": r["nb"]} for r in rows}


def get_admin_id(db=None):
    row = (db or get_db()).execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def teacher_can_receive_payments(teacher):
    return bool(STRIPE_SECRET_KEY and teacher["stripe_account_id"] and teacher["stripe_charges_enabled"])


def get_registration_mode():
    row = get_db().execute("SELECT value FROM settings WHERE key = 'registration_mode'").fetchone()
    return row["value"] if row else "tous"


# ---------------------------------------------------------------------------
# Paiements : réservations, encaissement Stripe, remboursements
# ---------------------------------------------------------------------------
class PaymentError(Exception):
    pass


def next_document_number(db, prefix, teacher_id):
    """Numéro de facture/reçu ou d'avoir : séquence continue par professeur."""
    key = f"{prefix}-{teacher_id or 0}"
    value = db.execute(
        "INSERT INTO doc_sequences(key, value) VALUES (?, 1) "
        "ON CONFLICT(key) DO UPDATE SET value = value + 1 RETURNING value",
        (key,),
    ).fetchone()[0]
    return f"{prefix}{(teacher_id or 0):04d}-{value:05d}"


def free_slot(db, slot_id, only_if_reserved_by=None, only_if_pending=False):
    sql = (
        "UPDATE slots SET reserved_by = NULL, student_notes = NULL, slot_mode = NULL, "
        "reservation_status = NULL, pending_until = NULL, reserved_at = NULL WHERE id = ?"
    )
    params = [slot_id]
    if only_if_reserved_by is not None:
        sql += " AND reserved_by = ?"
        params.append(only_if_reserved_by)
    if only_if_pending:
        sql += " AND reservation_status = 'pending'"
    return db.execute(sql, params).rowcount


def release_pending_order(db, order_id, status="expired"):
    """Abandon d'une réservation non payée : libère les créneaux bloqués."""
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order or order["status"] != "pending":
        return False
    if not db.execute("UPDATE orders SET status = ? WHERE id = ? AND status = 'pending'", (status, order_id)).rowcount:
        return False
    for item in db.execute("SELECT * FROM order_items WHERE order_id = ? AND status = 'pending'", (order_id,)).fetchall():
        if item["slot_id"]:
            free_slot(db, item["slot_id"], only_if_reserved_by=order["student_id"], only_if_pending=True)
    db.execute("UPDATE order_items SET status = 'canceled' WHERE order_id = ? AND status = 'pending'", (order_id,))
    return True


def recompute_order_status(db, order_id):
    order = db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    statuses = [r[0] for r in db.execute("SELECT status FROM order_items WHERE order_id = ?", (order_id,)).fetchall()]
    if not order or not statuses or order["status"] in ("pending", "expired", "canceled"):
        return
    if all(s in ("refunded", "canceled") for s in statuses):
        new = "canceled" if order["status"] == "free" else "refunded"
    elif any(s in ("refunded", "canceled") for s in statuses):
        new = "free" if order["status"] == "free" else "partially_refunded"
    else:
        new = order["status"]
    db.execute("UPDATE orders SET status = ? WHERE id = ?", (new, order_id))


def confirm_order(db, order_id, payment_intent_id=None, free=False):
    """Passe une commande en « payée » (ou gratuite confirmée). Renvoie la
    liste des lignes à rembourser parce que le créneau a été perdu entre-temps."""
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order or order["status"] != "pending":
        return []
    new_status = "free" if free else "paid"
    updated = db.execute(
        "UPDATE orders SET status = ?, paid_at = ?, stripe_payment_intent_id = ? WHERE id = ? AND status = 'pending'",
        (new_status, utc_now_str(), payment_intent_id, order_id),
    ).rowcount
    if not updated:
        return []
    if not free:
        db.execute("UPDATE orders SET invoice_number = ? WHERE id = ?",
                   (next_document_number(db, "F", order["teacher_id"]), order_id))
    lost = []
    for item in db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall():
        kept = item["slot_id"] and db.execute(
            "UPDATE slots SET reservation_status = 'confirmed', pending_until = NULL WHERE id = ? AND reserved_by = ?",
            (item["slot_id"], order["student_id"]),
        ).rowcount
        db.execute("UPDATE order_items SET status = ? WHERE id = ?", (new_status, item["id"]))
        if not kept:
            lost.append(item["id"])
    if order["student_id"] and order["teacher_id"]:
        db.execute(
            "INSERT OR IGNORE INTO contacts(student_id, teacher_id, course_id) VALUES (?, ?, ?)",
            (order["student_id"], order["teacher_id"], order["course_id"]),
        )
    return lost


def refund_order_item(db, item_id, reason, actor_id=None):
    """Rembourse une séance payée (Stripe, avec reprise du virement au
    professeur et de la commission) ou annule une séance gratuite, puis
    libère le créneau. Ne fait pas de commit : l'appelant commit aussitôt."""
    item = db.execute(
        "SELECT oi.*, o.stripe_payment_intent_id, o.student_id, o.teacher_id FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id WHERE oi.id = ?",
        (item_id,),
    ).fetchone()
    if not item:
        raise PaymentError("Séance introuvable.")
    if item["status"] == "free":
        db.execute(
            "UPDATE order_items SET status = 'canceled', refunded_at = ?, refund_reason = ? WHERE id = ? AND status = 'free'",
            (utc_now_str(), reason, item_id),
        )
    elif item["status"] == "paid":
        if not STRIPE_SECRET_KEY or not item["stripe_payment_intent_id"]:
            raise PaymentError("Le remboursement en ligne n'est pas disponible pour le moment.")
        try:
            refund = stripe.Refund.create(
                payment_intent=item["stripe_payment_intent_id"],
                amount=item["amount_cents"],
                reverse_transfer=True,
                refund_application_fee=True,
                metadata={"order_item_id": str(item_id)},
                idempotency_key=f"refund-order-item-{item_id}",
            )
        except stripe.StripeError as exc:
            app.logger.error("Remboursement Stripe refusé (ligne %s) : %s", item_id, exc)
            raise PaymentError(
                "Stripe n'a pas pu effectuer le remboursement. Réessayez plus tard ou contactez-nous."
            ) from exc
        if db.execute(
            "UPDATE order_items SET status = 'refunded', stripe_refund_id = ?, refunded_at = ?, refund_reason = ? "
            "WHERE id = ? AND status = 'paid'",
            (to_plain(refund).get("id"), utc_now_str(), reason, item_id),
        ).rowcount:
            db.execute("UPDATE order_items SET credit_note_number = ? WHERE id = ?",
                       (next_document_number(db, "A", item["teacher_id"]), item_id))
    else:
        raise PaymentError("Cette séance a déjà été remboursée ou annulée.")
    if item["slot_id"] and item["student_id"]:
        free_slot(db, item["slot_id"], only_if_reserved_by=item["student_id"])
    recompute_order_status(db, item["order_id"])
    audit(db, "remboursement" if item["status"] == "paid" else "annulation_gratuite",
          target=f"order_item:{item_id}", details=f"{item['amount_cents']} c. — {reason}", actor_id=actor_id)


def refund_lost_items(db, item_ids):
    for item_id in item_ids:
        try:
            refund_order_item(db, item_id, "Créneau déjà pris au moment du paiement")
            db.commit()
        except PaymentError:
            db.rollback()
            app.logger.error("Remboursement automatique impossible pour la ligne %s", item_id)


def active_item_for_slot(db, slot):
    return db.execute(
        "SELECT oi.*, o.student_id, o.teacher_status, o.paid_at FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE oi.slot_id = ? AND o.student_id = ? AND oi.status IN ('paid', 'free', 'pending') "
        "ORDER BY oi.id DESC LIMIT 1",
        (slot["id"], slot["reserved_by"]),
    ).fetchone()


def student_can_cancel(slot, item):
    """Annulation gratuite jusqu'à 12 h avant la séance. Au-delà, le droit de
    rétractation légal (14 jours, professeur professionnel) reste ouvert tant
    que la séance n'a pas commencé (C. conso., art. L. 221-18 et L. 221-25)."""
    start = safe_slot_start(slot)
    now = now_local()
    if start <= now:
        return False
    if start - now >= CANCELLATION_MIN_DELAY:
        return True
    if item and item["status"] == "paid" and item["teacher_status"] in PRO_STATUSES:
        paid_at = parse_db_utc(item["paid_at"])
        return bool(paid_at and utc_now() - paid_at < WITHDRAWAL_PERIOD)
    return False


def cancel_reservation(db, slot, reason, actor_id=None):
    """Annule la réservation d'un créneau en remboursant ce qui doit l'être."""
    item = active_item_for_slot(db, slot)
    if item and item["status"] == "pending":
        order = db.execute("SELECT * FROM orders WHERE id = ?", (item["order_id"],)).fetchone()
        if order["stripe_checkout_session_id"] and STRIPE_SECRET_KEY:
            try:
                stripe.checkout.Session.expire(order["stripe_checkout_session_id"])
            except stripe.StripeError:
                pass  # déjà expirée ou payée : un paiement tardif est remboursé automatiquement
        release_pending_order(db, item["order_id"], status="canceled")
        return
    if item:
        refund_order_item(db, item["id"], reason, actor_id=actor_id)
        return
    # Réservation de l'ancien système (porte-monnaie interne).
    legacy = db.execute("SELECT * FROM payments WHERE slot_id = ? AND status = 'held'", (slot["id"],)).fetchone()
    if legacy:
        db.execute("UPDATE users SET wallet_cents = wallet_cents + ? WHERE id = ?", (legacy["amount_cents"], legacy["student_id"]))
        db.execute(
            "INSERT INTO wallet_transactions(user_id, amount_cents, kind, description, payment_id) VALUES (?, ?, 'refund', ?, ?)",
            (legacy["student_id"], legacy["amount_cents"], "Remboursement (annulation)", legacy["id"]),
        )
        db.execute("UPDATE payments SET status = 'refunded', resolved_at = CURRENT_TIMESTAMP WHERE id = ?", (legacy["id"],))
    free_slot(db, slot["id"])


def fulfill_checkout_session(db, checkout_session):
    """Traite une session Stripe Checkout terminée (webhook ou retour du
    navigateur). Idempotent. Renvoie les lignes à rembourser après commit."""
    cs = to_plain(checkout_session)
    order_id = (cs.get("metadata") or {}).get("order_id") or cs.get("client_reference_id")
    if not order_id or not str(order_id).isdigit():
        return []
    order = db.execute("SELECT * FROM orders WHERE id = ?", (int(order_id),)).fetchone()
    if not order or order["stripe_checkout_session_id"] != cs.get("id"):
        return []
    if cs.get("payment_status") != "paid":
        return []
    payment_intent = cs.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    if order["status"] in ("expired", "canceled"):
        # Paiement arrivé après l'abandon de la réservation : remboursement intégral.
        try:
            stripe.Refund.create(
                payment_intent=payment_intent, reverse_transfer=True, refund_application_fee=True,
                idempotency_key=f"refund-late-order-{order['id']}",
            )
            audit(db, "remboursement_paiement_tardif", target=f"order:{order['id']}")
        except stripe.StripeError as exc:
            audit(db, "ERREUR_remboursement_paiement_tardif", target=f"order:{order['id']}", details=str(exc))
        return []
    if cs.get("amount_total") != order["amount_cents"] or (cs.get("currency") or "").lower() != "eur":
        audit(db, "ALERTE_montant_incoherent", target=f"order:{order['id']}",
              details=f"attendu {order['amount_cents']}, reçu {cs.get('amount_total')} {cs.get('currency')}")
        return []
    lost = confirm_order(db, order["id"], payment_intent_id=payment_intent)
    audit(db, "paiement_recu", target=f"order:{order['id']}", details=f"{order['amount_cents']} c.")
    return lost


def sync_stripe_account(db, user):
    """Met à jour l'état du compte Stripe Connect d'un professeur."""
    if not (STRIPE_SECRET_KEY and user["stripe_account_id"]):
        return
    try:
        account = to_plain(stripe.Account.retrieve(user["stripe_account_id"]))
    except stripe.StripeError as exc:
        app.logger.warning("Lecture du compte Stripe %s impossible : %s", user["stripe_account_id"], exc)
        return
    apply_stripe_account(db, account)


def apply_stripe_account(db, account):
    db.execute(
        "UPDATE users SET stripe_charges_enabled = ?, stripe_payouts_enabled = ?, stripe_details_submitted = ? "
        "WHERE stripe_account_id = ?",
        (int(bool(account.get("charges_enabled"))), int(bool(account.get("payouts_enabled"))),
         int(bool(account.get("details_submitted"))), account.get("id")),
    )


def handle_stripe_event(db, event):
    """Traite un événement Stripe vérifié. Renvoie les lignes à rembourser."""
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return fulfill_checkout_session(db, obj)
    if etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        order = db.execute("SELECT id FROM orders WHERE stripe_checkout_session_id = ?", (obj.get("id"),)).fetchone()
        if order:
            release_pending_order(db, order["id"], status="expired")
        return []
    if etype == "charge.refunded":
        # Remboursement intégral fait directement depuis le tableau de bord Stripe.
        payment_intent = obj.get("payment_intent")
        if obj.get("refunded") and payment_intent:
            order = db.execute("SELECT * FROM orders WHERE stripe_payment_intent_id = ?", (payment_intent,)).fetchone()
            if order:
                for item in db.execute("SELECT * FROM order_items WHERE order_id = ? AND status = 'paid'", (order["id"],)).fetchall():
                    if db.execute(
                        "UPDATE order_items SET status = 'refunded', refunded_at = ?, refund_reason = ? WHERE id = ? AND status = 'paid'",
                        (utc_now_str(), "Remboursement effectué depuis Stripe", item["id"]),
                    ).rowcount:
                        db.execute("UPDATE order_items SET credit_note_number = ? WHERE id = ?",
                                   (next_document_number(db, "A", order["teacher_id"]), item["id"]))
                    if item["slot_id"] and order["student_id"]:
                        free_slot(db, item["slot_id"], only_if_reserved_by=order["student_id"])
                recompute_order_status(db, order["id"])
        return []
    if etype == "charge.dispute.created":
        payment_intent = obj.get("payment_intent")
        if payment_intent:
            db.execute("UPDATE orders SET disputed = 1 WHERE stripe_payment_intent_id = ?", (payment_intent,))
            audit(db, "litige_bancaire_ouvert", target=f"payment_intent:{payment_intent}")
        return []
    if etype == "account.updated":
        apply_stripe_account(db, obj)
        return []
    return []


def process_overdue_pending_orders(db):
    """Réservations restées « en attente de paiement » après expiration de la
    session Stripe (webhook manqué, navigateur fermé…)."""
    limit = (utc_now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    overdue = db.execute(
        "SELECT * FROM orders WHERE status = 'pending' AND (expires_at IS NULL OR expires_at < ?) "
        "AND created_at < ?",
        (limit, (utc_now() - CHECKOUT_TTL - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    for order in overdue:
        lost = []
        if order["stripe_checkout_session_id"] and STRIPE_SECRET_KEY:
            try:
                cs = to_plain(stripe.checkout.Session.retrieve(order["stripe_checkout_session_id"]))
            except stripe.StripeError:
                continue  # on réessaiera au prochain passage
            if cs.get("status") == "complete" and cs.get("payment_status") == "paid":
                lost = fulfill_checkout_session(db, cs)
            elif cs.get("status") == "open":
                try:
                    stripe.checkout.Session.expire(order["stripe_checkout_session_id"])
                except stripe.StripeError:
                    continue
                release_pending_order(db, order["id"], "expired")
            else:
                release_pending_order(db, order["id"], "expired")
        else:
            release_pending_order(db, order["id"], "canceled")
        db.commit()
        refund_lost_items(db, lost)


# ---------------------------------------------------------------------------
# Justificatifs PDF (facture ou reçu, avoir, récapitulatif annuel)
# ---------------------------------------------------------------------------
def pdf_text(value):
    return xml_escape(str(value or ""))


def _pdf_styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=16, alignment=0, spaceAfter=4),
        "normal": ParagraphStyle("n", parent=styles["Normal"], fontName="Helvetica", fontSize=9.5, leading=13),
        "small": ParagraphStyle("s", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=11, textColor=colors.HexColor("#555555")),
        "right": ParagraphStyle("r", parent=styles["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, alignment=TA_RIGHT),
    }


def _pdf_table(rows, widths):
    table = Table(rows, colWidths=widths)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#bbbbbb")),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def teacher_vat_mention(status):
    if status == "micro":
        return "TVA non applicable, art. 293 B du CGI."
    return "Prestation fournie par un particulier non assujetti à la TVA."


def pdf_parties(order, st):
    status = order["teacher_status"] or "particulier"
    seller = f"<b>{pdf_text(order['teacher_name'])}</b><br/>{pdf_text(TEACHER_STATUSES.get(status, ''))}"
    if order["teacher_siret"]:
        seller += f"<br/>SIRET : {pdf_text(order['teacher_siret'])}"
    buyer = f"<b>{pdf_text(order['student_name'])}</b><br/>{pdf_text(order['student_email'])}"
    table = Table(
        [[Paragraph("Prestataire", st["small"]), Paragraph("Client", st["small"])],
         [Paragraph(seller, st["normal"]), Paragraph(buyer, st["normal"])]],
        colWidths=[85 * mm, 85 * mm],
    )
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return table


def pdf_footer(st):
    return Paragraph(
        f"Document établi par {pdf_text(LEGAL['site_name'])} ({pdf_text(LEGAL['editor_name'])}), plateforme de mise en "
        "relation, au nom et pour le compte du professeur, conformément aux conditions générales de vente. "
        "Le paiement a été encaissé par Stripe Payments Europe pour le compte du professeur ; "
        "la plateforme ne détient pas les fonds.",
        st["small"],
    )


def build_invoice_pdf(order, items):
    st = _pdf_styles()
    buf = BytesIO()
    is_pro = order["teacher_status"] in PRO_STATUSES
    label = "Facture" if is_pro else "Reçu de paiement"
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm,
                            leftMargin=20 * mm, rightMargin=20 * mm, title=f"{label} {order['invoice_number']}")
    paid = parse_db_utc(order["paid_at"])
    paid_str = paid.replace(tzinfo=timezone.utc).astimezone(PARIS).strftime("%d/%m/%Y") if paid else ""
    rows = [["Désignation", "Séance", "Montant TTC"]]
    total = 0
    for it in items:
        rows.append([
            Paragraph(f"Cours particulier : {pdf_text(order['course_title'])}<br/>"
                      f"<font size=8>{pdf_text(duree(it['duration_minutes']))} · "
                      f"{pdf_text(MODE_LABELS.get(it['slot_mode'] or 'en_ligne', ''))}</font>", st["normal"]),
            f"{it['slot_date']} {str(it['slot_time'])[:5]}",
            euros(it["amount_cents"]).replace(" ", " ").replace(" ", " "),
        ])
        total += it["amount_cents"]
    rows.append(["", "Total TTC", euros(total).replace(" ", " ").replace(" ", " ")])
    elements = [
        Paragraph(f"{label} n° {pdf_text(order['invoice_number'])}", st["title"]),
        Paragraph(f"Date : {paid_str} · Commande n° {order['id']}", st["normal"]),
        Spacer(1, 10), pdf_parties(order, st), Spacer(1, 14),
        _pdf_table(rows, [100 * mm, 38 * mm, 32 * mm]), Spacer(1, 8),
        Paragraph(pdf_text(teacher_vat_mention(order["teacher_status"])), st["normal"]),
        Paragraph(f"Payé par carte bancaire le {paid_str}.", st["normal"]),
        Spacer(1, 18), pdf_footer(st),
    ]
    doc.build(elements)
    buf.seek(0)
    return buf


def build_credit_note_pdf(order, item):
    st = _pdf_styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm,
                            leftMargin=20 * mm, rightMargin=20 * mm, title=f"Avoir {item['credit_note_number']}")
    refunded = parse_db_utc(item["refunded_at"])
    refunded_str = refunded.replace(tzinfo=timezone.utc).astimezone(PARIS).strftime("%d/%m/%Y") if refunded else ""
    amount = euros(item["amount_cents"]).replace(" ", " ").replace(" ", " ")
    rows = [["Désignation", "Séance", "Montant TTC"],
            [Paragraph(f"Remboursement : {pdf_text(order['course_title'])}<br/><font size=8>"
                       f"{pdf_text(item['refund_reason'] or '')}</font>", st["normal"]),
             f"{item['slot_date']} {str(item['slot_time'])[:5]}", f"- {amount}"]]
    elements = [
        Paragraph(f"Avoir n° {pdf_text(item['credit_note_number'])}", st["title"]),
        Paragraph(f"Date : {refunded_str} · Se rapporte au document n° {pdf_text(order['invoice_number'])}", st["normal"]),
        Spacer(1, 10), pdf_parties(order, st), Spacer(1, 14),
        _pdf_table(rows, [100 * mm, 38 * mm, 32 * mm]), Spacer(1, 8),
        Paragraph(f"Montant remboursé sur la carte utilisée pour le paiement : {amount}.", st["normal"]),
        Spacer(1, 18), pdf_footer(st),
    ]
    doc.build(elements)
    buf.seek(0)
    return buf


def teacher_year_summary(db, teacher_id, year):
    rows = db.execute(
        """
        SELECT oi.amount_cents, oi.platform_fee_cents, oi.status, o.paid_at
        FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE o.teacher_id = ? AND oi.status IN ('paid', 'refunded') AND substr(o.paid_at, 1, 4) = ?
        """,
        (teacher_id, str(year)),
    ).fetchall()
    paid = [r for r in rows if r["status"] == "paid"]
    gross = sum(r["amount_cents"] for r in paid)
    fees = sum(r["platform_fee_cents"] for r in paid)
    return {"year": year, "count": len(paid), "gross": gross, "fees": fees, "net": gross - fees,
            "refunded": sum(1 for r in rows if r["status"] == "refunded")}


def build_year_summary_pdf(teacher, summary):
    st = _pdf_styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm,
                            leftMargin=20 * mm, rightMargin=20 * mm, title=f"Récapitulatif {summary['year']}")
    fmt = lambda c: euros(c).replace(" ", " ").replace(" ", " ")  # noqa: E731
    rows = [["Rubrique", "", "Montant"],
            ["Séances payées (hors remboursements)", str(summary["count"]), fmt(summary["gross"])],
            [f"Commission de la plateforme ({PLATFORM_FEE_PERCENT} %)", "", f"- {fmt(summary['fees'])}"],
            ["Montant net perçu", "", fmt(summary["net"])]]
    elements = [
        Paragraph(f"Récapitulatif annuel {summary['year']}", st["title"]),
        Paragraph("Transactions réalisées par l'intermédiaire de la plateforme (article 242 bis du CGI).", st["normal"]),
        Spacer(1, 10),
        Paragraph(f"<b>{pdf_text(teacher['name'])}</b><br/>{pdf_text(teacher['email'])}<br/>"
                  f"{pdf_text(TEACHER_STATUSES.get(teacher['teacher_status'] or '', 'Statut non renseigné'))}"
                  + (f"<br/>SIRET : {pdf_text(teacher['siret'])}" if teacher["siret"] else ""), st["normal"]),
        Spacer(1, 14), _pdf_table(rows, [110 * mm, 20 * mm, 40 * mm]), Spacer(1, 12),
        Paragraph("Les revenus perçus par l'intermédiaire de plateformes en ligne sont soumis à l'impôt sur le revenu "
                  "et, selon les cas, aux cotisations sociales. Informations : www.impots.gouv.fr et www.urssaf.fr. "
                  "Les plateformes transmettent chaque année à l'administration fiscale les informations relatives aux "
                  "revenus de leurs utilisateurs (directive (UE) 2021/514, dite DAC7).", st["small"]),
        Spacer(1, 12), Paragraph(f"Document établi le {now_local().strftime('%d/%m/%Y')} par {pdf_text(LEGAL['site_name'])}.", st["small"]),
    ]
    doc.build(elements)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Suppression et anonymisation des comptes
# ---------------------------------------------------------------------------
def upcoming_reservations(db, user):
    """Créneaux à venir impliquant l'utilisateur (comme élève ou comme prof)."""
    if user["role"] == "prof":
        rows = db.execute(
            "SELECT sl.* FROM slots sl JOIN courses c ON c.id = sl.course_id WHERE c.teacher_id = ? AND sl.reserved_by IS NOT NULL",
            (user["id"],),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM slots WHERE reserved_by = ?", (user["id"],)).fetchall()
    now = now_local()
    return [r for r in rows if safe_slot_end(r) > now]


def delete_user_messages(db, user_id):
    for m in db.execute(
        "SELECT attachment_filename FROM messages WHERE (sender_id = ? OR recipient_id = ?) AND attachment_filename IS NOT NULL",
        (user_id, user_id),
    ).fetchall():
        delete_file_quietly(MESSAGE_ATTACHMENTS_DIR, m["attachment_filename"])
    db.execute("DELETE FROM messages WHERE sender_id = ? OR recipient_id = ?", (user_id, user_id))


def erase_user(db, user, reason, actor_id=None, force_refunds=False):
    """Supprime un compte. Les commandes sont des pièces comptables à
    conserver 10 ans : si l'utilisateur en a, le compte est anonymisé plutôt
    que supprimé. Lève PaymentError si un remboursement échoue."""
    upcoming = upcoming_reservations(db, user)
    if upcoming and not force_refunds:
        raise PaymentError("Ce compte a encore des séances réservées à venir : annulez-les d'abord.")
    for slot in upcoming:
        cancel_reservation(db, slot, f"Suppression de compte ({reason})", actor_id=actor_id)
    has_orders = db.execute(
        "SELECT 1 FROM orders WHERE student_id = ? OR teacher_id = ? LIMIT 1", (user["id"], user["id"])
    ).fetchone() or db.execute(
        "SELECT 1 FROM payments WHERE student_id = ? OR teacher_id = ? LIMIT 1", (user["id"], user["id"])
    ).fetchone()
    delete_file_quietly(PROFILE_PHOTOS_DIR, user["photo"])
    delete_user_messages(db, user["id"])
    if has_orders:
        # Les créneaux libres sont supprimés ; les cours restent pour les justificatifs.
        db.execute(
            "DELETE FROM slots WHERE reserved_by IS NULL AND course_id IN (SELECT id FROM courses WHERE teacher_id = ?)",
            (user["id"],),
        )
        db.execute("DELETE FROM contacts WHERE student_id = ? OR teacher_id = ?", (user["id"], user["id"]))
        db.execute("DELETE FROM ratings WHERE student_id = ? OR teacher_id = ?", (user["id"], user["id"]))
        db.execute(
            "UPDATE users SET name = 'Compte supprimé', email = ?, password_hash = ?, bio = NULL, photo = NULL, "
            "education_level = NULL, approved = 0, deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (f"supprime-{user['id']}-{secrets.token_hex(4)}@invalid", generate_password_hash(secrets.token_urlsafe(32)), user["id"]),
        )
    else:
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    audit(db, "suppression_compte", target=f"user:{user['id']}", details=reason, actor_id=actor_id)


# ---------------------------------------------------------------------------
# Tâche de maintenance (réservations expirées, durées de conservation)
# ---------------------------------------------------------------------------
_last_daily_run = {"date": None}


def run_maintenance_once():
    db = connect_db()
    try:
        process_overdue_pending_orders(db)
        db.execute("DELETE FROM signals WHERE created_at < ?",
                   ((utc_now() - RETENTION_SIGNALS).strftime("%Y-%m-%d %H:%M:%S"),))
        db.execute("DELETE FROM whiteboard_strokes WHERE created_at < ?",
                   ((utc_now() - RETENTION_WHITEBOARD).strftime("%Y-%m-%d %H:%M:%S"),))
        db.commit()
        today = date.today()
        if _last_daily_run["date"] != today:
            _last_daily_run["date"] = today
            db.execute("DELETE FROM audit_log WHERE created_at < ?",
                       ((utc_now() - RETENTION_AUDIT_LOG).strftime("%Y-%m-%d %H:%M:%S"),))
            db.execute("DELETE FROM stripe_events WHERE received_at < ?",
                       ((utc_now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S"),))
            db.execute("DELETE FROM reports WHERE status = 'closed' AND resolved_at < ?",
                       ((utc_now() - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S"),))
            limit = (utc_now() - RETENTION_INACTIVE_ACCOUNT).strftime("%Y-%m-%d %H:%M:%S")
            for user in db.execute(
                "SELECT * FROM users WHERE role != 'admin' AND deleted_at IS NULL AND last_login_at < ?", (limit,)
            ).fetchall():
                try:
                    erase_user(db, user, "inactivité de plus de 3 ans", force_refunds=False)
                except PaymentError:
                    db.rollback()
                    continue
                db.commit()
            db.commit()
    finally:
        db.close()


def start_maintenance_thread():
    lock_handle = None
    try:
        import fcntl  # un seul worker gunicorn exécute la maintenance

        lock_handle = open(os.path.join(DATA_DIR, ".maintenance.lock"), "w")
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        if lock_handle is not None:
            lock_handle.close()
            return  # un autre processus détient déjà le verrou

    def loop():
        while True:
            try:
                run_maintenance_once()
            except Exception:  # la boucle ne doit jamais s'arrêter
                app.logger.exception("Erreur dans la tâche de maintenance")
            time_mod.sleep(MAINTENANCE_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="maintenance").start()
    app.config["_maintenance_lock"] = lock_handle


# ---------------------------------------------------------------------------
# Accueil et pages d'information
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("presentation"))


@app.route("/presentation")
def presentation():
    return render_template("presentation.html")


@app.route("/mentions-legales")
def mentions_legales():
    return render_template("mentions_legales.html")


@app.route("/cgu")
def cgu():
    return render_template("cgu.html")


@app.route("/cgv")
def cgv():
    return render_template("cgv.html", cancellation_hours=int(CANCELLATION_MIN_DELAY.total_seconds() // 3600))


@app.route("/confidentialite")
def confidentialite():
    return render_template("confidentialite.html", stun_urls=WEBRTC_STUN_URLS)


REPORT_CATEGORIES = {
    "contenu_illicite": "Contenu illicite (propos haineux, harcèlement, contenu sexuel, etc.)",
    "mineur": "Mise en danger d'un mineur",
    "fraude": "Fraude, usurpation d'identité ou faux profil",
    "seance": "Problème avec une séance ou un paiement",
    "autre": "Autre",
}


@app.route("/signaler", methods=["GET", "POST"])
def signaler():
    user = current_user()
    db = get_db()
    item = None
    item_id = request.values.get("seance", "")
    if user and item_id.isdigit():
        item = db.execute(
            "SELECT oi.*, o.course_title, o.student_id, o.teacher_id FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE oi.id = ? AND (o.student_id = ? OR o.teacher_id = ?)",
            (int(item_id), user["id"], user["id"]),
        ).fetchone()

    if request.method == "POST":
        if is_rate_limited(f"report:{client_ip()}", 5, 3600):
            flash("Trop de signalements envoyés depuis cette adresse. Réessayez plus tard.", "error")
            return render_template("signaler.html", categories=REPORT_CATEGORIES, item=item, form=request.form), 429
        category = request.form.get("category", "")
        description = form_value("description", "report", multiline=True)
        location = clean(request.form.get("location", ""), 300)
        name = form_value("name", "name") if not user else user["name"]
        email = clean(request.form.get("email", ""), LIMITS["email"]).lower() if not user else user["email"]
        errors = []
        if category not in REPORT_CATEGORIES:
            errors.append("Choisissez le motif du signalement.")
        if len(description) < 10:
            errors.append("Décrivez le problème en quelques phrases.")
        if not user and email and not EMAIL_RE.match(email):
            errors.append("L'adresse e-mail n'est pas valide.")
        if not request.form.get("good_faith"):
            errors.append("Merci de confirmer que votre signalement est fait de bonne foi.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("signaler.html", categories=REPORT_CATEGORIES, item=item, form=request.form)
        db.execute(
            "INSERT INTO reports(reporter_id, reporter_name, reporter_email, category, location, description, order_item_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"] if user else None, name or None, email or None, category, location or None, description,
             item["id"] if item else None),
        )
        db.commit()
        flash("Votre signalement a été transmis à l'équipe. Il sera examiné dans les meilleurs délais.", "success")
        return redirect(url_for("presentation"))
    return render_template("signaler.html", categories=REPORT_CATEGORIES, item=item, form={})


# ---------------------------------------------------------------------------
# Liste et calendrier des cours
# ---------------------------------------------------------------------------
def parse_course_filters(args):
    return {k: clean(args.get(k, ""), 80) for k in ("q", "subject", "level", "teacher_id", "min_rating", "mode", "city")}


def build_course_query(filters):
    today = now_local().date().isoformat()
    conditions = [
        "u.approved = 1",
        "u.deleted_at IS NULL",
        "EXISTS (SELECT 1 FROM slots s WHERE s.course_id = c.id AND s.reserved_by IS NULL AND s.slot_date >= ?)",
    ]
    params = [today]
    if STRIPE_SECRET_KEY:
        conditions.append("(c.price_cents = 0 OR (u.stripe_account_id IS NOT NULL AND u.stripe_charges_enabled = 1))")
    else:
        conditions.append("c.price_cents = 0")
    if filters["q"]:
        conditions.append("(c.title LIKE ? ESCAPE '\\' OR c.description LIKE ? ESCAPE '\\')")
        like = "%" + filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like, like]
    if filters["subject"] in MATIERES:
        conditions.append("INSTR(',' || REPLACE(c.subject, ', ', ',') || ',', ',' || ? || ',') > 0")
        params.append(filters["subject"])
    if filters["level"] in NIVEAUX:
        conditions.append("INSTR(',' || REPLACE(c.level, ', ', ',') || ',', ',' || ? || ',') > 0")
        params.append(filters["level"])
    if filters["mode"] == "en_ligne":
        conditions.append("c.mode IN ('en_ligne', 'les_deux')")
    elif filters["mode"] == "presentiel":
        conditions.append("c.mode IN ('presentiel', 'les_deux')")
    if filters["city"]:
        conditions.append("c.city LIKE ? ESCAPE '\\'")
        params.append("%" + filters["city"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if filters["teacher_id"].isdigit():
        conditions.append("c.teacher_id = ?")
        params.append(int(filters["teacher_id"]))
    if filters["min_rating"].isdigit() and 1 <= int(filters["min_rating"]) <= 5:
        conditions.append("(SELECT AVG(rating) FROM ratings r WHERE r.teacher_id = c.teacher_id) >= ?")
        params.append(int(filters["min_rating"]))
    return " AND ".join(conditions), params


def fetch_filtered_courses(db, filters):
    where_clause, params = build_course_query(filters)
    return db.execute(
        f"""
        SELECT c.*, u.name AS teacher_name, u.bio AS teacher_bio, u.photo AS teacher_photo,
               u.education_level AS teacher_education_level, u.teacher_status, u.siret AS teacher_siret
        FROM courses c JOIN users u ON u.id = c.teacher_id
        WHERE {where_clause}
        ORDER BY c.id DESC
        """,
        params,
    ).fetchall()


def approved_teachers(db):
    return db.execute(
        "SELECT id, name FROM users WHERE role = 'prof' AND approved = 1 AND deleted_at IS NULL ORDER BY name"
    ).fetchall()


@app.route("/cours")
def cours():
    db = get_db()
    filters = parse_course_filters(request.args)
    rows = fetch_filtered_courses(db, filters)
    now = now_local()
    slots_map = {
        cid: [s for s in slots if safe_slot_start(s) > now]
        for cid, slots in get_slots_map(db, [r["id"] for r in rows]).items()
    }
    bookable = {
        s["id"] for slots in slots_map.values() for s in slots
        if s["reserved_by"] is None and safe_slot_start(s) >= now + BOOKING_MIN_NOTICE
    }
    return render_template(
        "cours.html",
        courses=rows,
        slots_map=slots_map,
        bookable=bookable,
        ratings_map=get_teacher_ratings_map(db, [r["teacher_id"] for r in rows]),
        matieres=MATIERES,
        niveaux=NIVEAUX,
        teachers=approved_teachers(db),
        filters=filters,
    )


def resolve_month(args):
    today = now_local().date()
    try:
        year = int(args.get("year", today.year))
        month = int(args.get("month", today.month))
        if not (1 <= month <= 12 and today.year - 5 <= year <= today.year + 5):
            raise ValueError
    except (TypeError, ValueError):
        year, month = today.year, today.month
    return year, month


def month_nav(year, month):
    prev = (year - 1, 12) if month == 1 else (year, month - 1)
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return prev, nxt


def build_month_weeks(year, month, events_by_date):
    today = now_local().date()
    weeks = []
    for week in calendar_mod.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weeks.append([
            {"date": day, "in_month": day.month == month, "is_today": day == today,
             "events": sorted(events_by_date.get(day.isoformat(), []), key=lambda e: e["time"])}
            for day in week
        ])
    return weeks


@app.route("/cours/calendrier")
def cours_calendrier():
    db = get_db()
    filters = parse_course_filters(request.args)
    year, month = resolve_month(request.args)
    (py, pm), (ny, nm) = month_nav(year, month)
    courses = fetch_filtered_courses(db, filters)
    slots_map = get_slots_map(db, [c["id"] for c in courses])
    now = now_local()
    events = {}
    for c in courses:
        for s in slots_map.get(c["id"], []):
            if s["reserved_by"] is None and safe_slot_start(s) > now:
                events.setdefault(s["slot_date"], []).append({
                    "title": c["title"], "subtitle": f"{c['subject']} · {c['level']} · {c['teacher_name']}",
                    "time": s["slot_time"], "duration": s["duration_minutes"], "status": "disponible",
                    "course_id": c["id"],
                })
    nav = {k: v for k, v in filters.items() if v}
    return render_template(
        "cours_calendrier.html",
        weeks=build_month_weeks(year, month, events),
        month_label=f"{MOIS_FR_CAP[month]} {year}",
        jours=JOURS_COURTS,
        prev_link=url_for("cours_calendrier", year=py, month=pm, **nav),
        next_link=url_for("cours_calendrier", year=ny, month=nm, **nav),
        today_link=url_for("cours_calendrier", **nav),
        matieres=MATIERES, niveaux=NIVEAUX, teachers=approved_teachers(db),
        filters=filters, courses_count=len(courses),
    )


# ---------------------------------------------------------------------------
# Réservation et paiement
# ---------------------------------------------------------------------------
@app.route("/cours/<int:course_id>/reserver", methods=["POST"])
@login_required(role="etudiant")
def reserver(course_id):
    db = get_db()
    student = current_user()
    back = redirect(url_for("cours") + f"#cours-{course_id}")
    if is_rate_limited(f"booking:{student['id']}", 10, 600):
        flash("Trop de tentatives de réservation. Patientez quelques minutes.", "error")
        return back

    course = db.execute(
        """
        SELECT c.*, u.name AS teacher_name, u.approved, u.deleted_at, u.teacher_status, u.siret,
               u.stripe_account_id, u.stripe_charges_enabled
        FROM courses c JOIN users u ON u.id = c.teacher_id WHERE c.id = ?
        """,
        (course_id,),
    ).fetchone()
    if not course or not course["approved"] or course["deleted_at"]:
        flash("Ce cours n'est pas disponible.", "error")
        return redirect(url_for("cours"))

    slot_ids = []
    for raw in request.form.getlist("slot_id[]"):
        if raw.isdigit() and int(raw) not in slot_ids:
            slot_ids.append(int(raw))
    if not slot_ids:
        flash("Sélectionnez au moins un créneau.", "error")
        return back
    if len(slot_ids) > MAX_SLOTS_PER_BOOKING:
        flash(f"Vous pouvez réserver au maximum {MAX_SLOTS_PER_BOOKING} créneaux à la fois.", "error")
        return back
    if not request.form.get("accept_cgv"):
        flash("Vous devez accepter les conditions générales de vente pour réserver.", "error")
        return back

    price = int(course["price_cents"] or 0)
    is_free = price == 0
    teacher_is_pro = course["teacher_status"] in PRO_STATUSES
    if not is_free:
        if not STRIPE_SECRET_KEY or not (course["stripe_account_id"] and course["stripe_charges_enabled"]):
            flash("Le paiement en ligne n'est pas encore disponible pour ce professeur.", "error")
            return back
        if teacher_is_pro and not request.form.get("waiver"):
            flash("Cochez la case relative au droit de rétractation pour confirmer la réservation.", "error")
            return back

    notes = form_value("notes", "notes", multiline=True) or None
    now = now_local()
    pending_until = (utc_now() + CHECKOUT_TTL + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    reserved = []
    for slot_id in slot_ids:
        slot = db.execute("SELECT * FROM slots WHERE id = ? AND course_id = ?", (slot_id, course_id)).fetchone()
        if not slot or safe_slot_start(slot) < now + BOOKING_MIN_NOTICE:
            continue
        if course["mode"] == "les_deux":
            chosen = request.form.get(f"slot_mode_{slot_id}", "")
            mode = chosen if chosen in ("en_ligne", "presentiel") else "en_ligne"
        else:
            mode = course["mode"]
        if db.execute(
            "UPDATE slots SET reserved_by = ?, student_notes = ?, slot_mode = ?, reservation_status = ?, "
            "pending_until = ?, reserved_at = CURRENT_TIMESTAMP WHERE id = ? AND course_id = ? AND reserved_by IS NULL",
            (student["id"], notes, mode, "confirmed" if is_free else "pending",
             None if is_free else pending_until, slot_id, course_id),
        ).rowcount:
            reserved.append((slot, mode))
    if not reserved:
        db.rollback()
        flash("Ces créneaux ne sont plus disponibles (déjà réservés, ou trop proches de l'heure actuelle).", "error")
        return back

    fee = 0 if is_free else platform_fee(price)
    cur = db.execute(
        """
        INSERT INTO orders(student_id, teacher_id, course_id, course_title, student_name, student_email,
                           teacher_name, teacher_status, teacher_siret, amount_cents, platform_fee_cents, status,
                           stripe_account_id, waiver_accepted_at, terms_version, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (student["id"], course["teacher_id"], course_id, course["title"], student["name"], student["email"],
         course["teacher_name"], course["teacher_status"] or "particulier",
         course["siret"] if teacher_is_pro else None, price * len(reserved), fee * len(reserved),
         course["stripe_account_id"], utc_now_str() if request.form.get("waiver") else None, TERMS_VERSION,
         None if is_free else (utc_now() + CHECKOUT_TTL).strftime("%Y-%m-%d %H:%M:%S")),
    )
    order_id = cur.lastrowid
    for slot, mode in reserved:
        db.execute(
            "INSERT INTO order_items(order_id, slot_id, slot_date, slot_time, duration_minutes, slot_mode, amount_cents, platform_fee_cents, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (order_id, slot["id"], slot["slot_date"], slot["slot_time"], slot["duration_minutes"], mode, price, fee),
        )
    partial = len(reserved) < len(slot_ids)

    if is_free:
        confirm_order(db, order_id, free=True)
        db.commit()
        flash(f"Réservation confirmée ({len(reserved)} séance(s)).", "success")
        if partial:
            flash("Certains créneaux choisis n'étaient plus disponibles.", "error")
        return redirect(url_for("etudiant"))

    db.commit()  # les créneaux restent bloqués pendant le paiement
    try:
        checkout = to_plain(stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            locale="fr",
            line_items=[{
                "quantity": len(reserved),
                "price_data": {
                    "currency": "eur",
                    "unit_amount": price,
                    "product_data": {
                        "name": f"Cours particulier : {course['title']}"[:250],
                        "description": f"Séance(s) avec {course['teacher_name']}"[:250],
                    },
                },
            }],
            payment_intent_data={
                "application_fee_amount": fee * len(reserved),
                "on_behalf_of": course["stripe_account_id"],
                "transfer_data": {"destination": course["stripe_account_id"]},
                "metadata": {"order_id": str(order_id)},
                "description": f"Commande {order_id} – {course['title']}"[:250],
            },
            customer_email=student["email"],
            client_reference_id=str(order_id),
            metadata={"order_id": str(order_id)},
            expires_at=int(time_mod.time() + CHECKOUT_TTL.total_seconds()),
            success_url=external_url("paiement_retour", order_id=order_id) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=external_url("paiement_annule", order_id=order_id),
            idempotency_key=f"checkout-order-{order_id}",
        ))
    except stripe.StripeError as exc:
        app.logger.error("Création de la session Stripe impossible (commande %s) : %s", order_id, exc)
        release_pending_order(db, order_id, status="canceled")
        db.commit()
        flash("Le paiement n'a pas pu être lancé. Aucun montant n'a été débité ; réessayez dans un instant.", "error")
        return back

    db.execute("UPDATE orders SET stripe_checkout_session_id = ? WHERE id = ?", (checkout["id"], order_id))
    db.commit()
    if partial:
        flash("Certains créneaux choisis n'étaient plus disponibles : seuls les créneaux restants seront payés.", "error")
    return redirect(checkout["url"], code=303)


def own_order(order_id):
    return get_db().execute(
        "SELECT * FROM orders WHERE id = ? AND student_id = ?", (order_id, current_user()["id"])
    ).fetchone()


@app.route("/paiement/retour/<int:order_id>")
@login_required(role="etudiant")
def paiement_retour(order_id):
    db = get_db()
    order = own_order(order_id)
    if not order:
        abort(404)
    if order["status"] == "pending" and order["stripe_checkout_session_id"] and STRIPE_SECRET_KEY:
        try:
            cs = stripe.checkout.Session.retrieve(order["stripe_checkout_session_id"])
            lost = fulfill_checkout_session(db, cs)
            db.commit()
            refund_lost_items(db, lost)
            if lost:
                flash("Un des créneaux a été réservé par quelqu'un d'autre pendant votre paiement : "
                      "il vous a été remboursé automatiquement.", "error")
        except stripe.StripeError:
            db.rollback()
            flash("Paiement en cours de vérification : votre réservation apparaîtra dans quelques instants.", "success")
            return redirect(url_for("etudiant"))
    order = own_order(order_id)
    if order["status"] in ("paid", "partially_refunded"):
        flash("Paiement reçu, votre réservation est confirmée.", "success")
    elif order["status"] == "pending":
        flash("Paiement en cours de vérification : votre réservation apparaîtra dans quelques instants.", "success")
    else:
        flash("Cette réservation n'a pas abouti.", "error")
    return redirect(url_for("etudiant"))


@app.route("/paiement/annule/<int:order_id>")
@login_required(role="etudiant")
def paiement_annule(order_id):
    db = get_db()
    order = own_order(order_id)
    if not order:
        abort(404)
    if order["status"] == "pending":
        done = False
        if order["stripe_checkout_session_id"] and STRIPE_SECRET_KEY:
            try:
                cs = to_plain(stripe.checkout.Session.retrieve(order["stripe_checkout_session_id"]))
                if cs.get("status") == "complete":
                    lost = fulfill_checkout_session(db, cs)
                    db.commit()
                    refund_lost_items(db, lost)
                    flash("Le paiement a bien été reçu : votre réservation est confirmée.", "success")
                    return redirect(url_for("etudiant"))
                if cs.get("status") == "open":
                    stripe.checkout.Session.expire(order["stripe_checkout_session_id"])
                done = True
            except stripe.StripeError:
                done = False
        else:
            done = True
        if done:
            release_pending_order(db, order_id, status="canceled")
            db.commit()
    flash("Paiement abandonné : aucun montant n'a été débité et les créneaux ont été libérés.", "success")
    return redirect(url_for("cours"))


@app.route("/paiement/reprendre/<int:order_id>")
@login_required(role="etudiant")
def paiement_reprendre(order_id):
    order = own_order(order_id)
    if not order or order["status"] != "pending" or not order["stripe_checkout_session_id"] or not STRIPE_SECRET_KEY:
        flash("Ce paiement ne peut plus être repris.", "error")
        return redirect(url_for("etudiant"))
    try:
        cs = to_plain(stripe.checkout.Session.retrieve(order["stripe_checkout_session_id"]))
    except stripe.StripeError:
        flash("Impossible de joindre le service de paiement. Réessayez dans un instant.", "error")
        return redirect(url_for("etudiant"))
    if cs.get("status") == "open" and cs.get("url"):
        return redirect(cs["url"], code=303)
    return redirect(url_for("paiement_retour", order_id=order_id))


@app.route("/stripe/webhook", methods=["POST"])
@csrf.exempt  # appel serveur à serveur authentifié par la signature Stripe
def stripe_webhook():
    secrets_list = [s for s in (STRIPE_WEBHOOK_SECRET, STRIPE_CONNECT_WEBHOOK_SECRET) if s]
    if not secrets_list:
        return jsonify({"error": "webhook non configuré"}), 503
    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    event = None
    for secret in secrets_list:
        try:
            event = to_plain(stripe.Webhook.construct_event(payload, signature, secret))
            break
        except stripe.SignatureVerificationError:
            continue
        except ValueError:
            return jsonify({"error": "contenu invalide"}), 400
    if event is None:
        return jsonify({"error": "signature invalide"}), 400

    db = get_db()
    if db.execute("SELECT 1 FROM stripe_events WHERE id = ?", (event.get("id"),)).fetchone():
        return jsonify({"received": True})
    try:
        lost = handle_stripe_event(db, event)
        db.execute("INSERT INTO stripe_events(id, type) VALUES (?, ?)", (event.get("id"), event.get("type") or ""))
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Erreur de traitement du webhook Stripe %s", event.get("id"))
        return jsonify({"error": "erreur interne"}), 500
    refund_lost_items(db, lost)
    return jsonify({"received": True})


@app.route("/reservations/<int:slot_id>/annuler", methods=["POST"])
@login_required(role="etudiant")
def annuler_reservation(slot_id):
    db = get_db()
    user = current_user()
    slot = db.execute("SELECT * FROM slots WHERE id = ? AND reserved_by = ?", (slot_id, user["id"])).fetchone()
    if not slot:
        flash("Réservation introuvable ou déjà annulée.", "error")
        return redirect(url_for("etudiant"))
    item = active_item_for_slot(db, slot)
    if not (item and item["status"] == "pending") and not student_can_cancel(slot, item):
        flash("Cette séance ne peut plus être annulée en ligne (moins de 12 heures avant le début). "
              "En cas de problème, utilisez le formulaire de signalement.", "error")
        return redirect(url_for("etudiant"))
    try:
        cancel_reservation(db, slot, "Annulation par l'élève")
        db.commit()
    except PaymentError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("etudiant"))
    flash("Séance annulée. Si elle était payée, le remboursement est en cours sur votre carte (quelques jours ouvrés).", "success")
    return redirect(url_for("etudiant"))


# ---------------------------------------------------------------------------
# Paiements : historique, justificatifs, compte Stripe du professeur
# ---------------------------------------------------------------------------
@app.route("/paiements")
@login_required()
def paiements():
    db = get_db()
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin_paiements"))
    if user["role"] == "prof":
        if user["stripe_account_id"] and not user["stripe_charges_enabled"]:
            sync_stripe_account(db, user)
            db.commit()
            user = refresh_current_user()
        items = db.execute(
            """
            SELECT oi.*, o.course_title, o.student_name, o.paid_at, o.invoice_number, o.id AS order_id
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            WHERE o.teacher_id = ? AND oi.status IN ('paid', 'refunded')
            ORDER BY o.paid_at DESC, oi.id DESC LIMIT 300
            """,
            (user["id"],),
        ).fetchall()
        years = sorted({int(str(i["paid_at"])[:4]) for i in items if i["paid_at"]} | {now_local().year}, reverse=True)
        return render_template(
            "paiements_prof.html", items=items, years=years,
            summaries=[teacher_year_summary(db, user["id"], y) for y in years],
            stripe_configured=bool(STRIPE_SECRET_KEY),
        )
    orders = db.execute(
        "SELECT * FROM orders WHERE student_id = ? AND status != 'canceled' ORDER BY id DESC LIMIT 200", (user["id"],)
    ).fetchall()
    items_by_order = {}
    if orders:
        placeholders = ",".join("?" for _ in orders)
        for it in db.execute(
            f"SELECT * FROM order_items WHERE order_id IN ({placeholders}) ORDER BY slot_date, slot_time",
            [o["id"] for o in orders],
        ).fetchall():
            items_by_order.setdefault(it["order_id"], []).append(it)
    return render_template("paiements_eleve.html", orders=orders, items_by_order=items_by_order)


@app.route("/paiements/stripe/activer", methods=["POST"])
@login_required(role="prof")
def stripe_connect_activer():
    db = get_db()
    user = current_user()
    if not STRIPE_SECRET_KEY:
        flash("Le paiement en ligne n'est pas encore configuré sur le site.", "error")
        return redirect(url_for("paiements"))
    if user["teacher_status"] not in TEACHER_STATUSES:
        flash("Indiquez d'abord votre statut (particulier ou micro-entrepreneur) dans votre profil.", "error")
        return redirect(url_for("prof_profil"))
    if is_rate_limited(f"connect:{user['id']}", 10, 3600):
        flash("Trop de tentatives. Réessayez plus tard.", "error")
        return redirect(url_for("paiements"))
    account_id = user["stripe_account_id"]
    try:
        if not account_id:
            account = to_plain(stripe.Account.create(
                type="express",
                country="FR",
                email=user["email"],
                business_type="individual",
                capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
                business_profile={"mcc": "8299", "product_description": "Cours particuliers"},
                metadata={"user_id": str(user["id"])},
                idempotency_key=f"connect-account-{user['id']}",
            ))
            account_id = account["id"]
            db.execute("UPDATE users SET stripe_account_id = ? WHERE id = ?", (account_id, user["id"]))
            audit(db, "stripe_compte_cree", target=f"user:{user['id']}")
            db.commit()
        link = to_plain(stripe.AccountLink.create(
            account=account_id, type="account_onboarding",
            refresh_url=external_url("stripe_connect_relancer"),
            return_url=external_url("stripe_connect_retour"),
        ))
    except stripe.StripeError as exc:
        db.rollback()
        app.logger.error("Stripe Connect (user %s) : %s", user["id"], exc)
        flash("Stripe n'a pas pu ouvrir l'activation des paiements. Réessayez plus tard.", "error")
        return redirect(url_for("paiements"))
    return redirect(link["url"], code=303)


@app.route("/paiements/stripe/relancer")
@login_required(role="prof")
def stripe_connect_relancer():
    user = current_user()
    if not (STRIPE_SECRET_KEY and user["stripe_account_id"]):
        return redirect(url_for("paiements"))
    try:
        link = to_plain(stripe.AccountLink.create(
            account=user["stripe_account_id"], type="account_onboarding",
            refresh_url=external_url("stripe_connect_relancer"),
            return_url=external_url("stripe_connect_retour"),
        ))
    except stripe.StripeError:
        flash("Stripe est momentanément indisponible. Réessayez plus tard.", "error")
        return redirect(url_for("paiements"))
    return redirect(link["url"], code=303)


@app.route("/paiements/stripe/retour")
@login_required(role="prof")
def stripe_connect_retour():
    db = get_db()
    sync_stripe_account(db, current_user())
    db.commit()
    user = refresh_current_user()
    if user["stripe_charges_enabled"]:
        flash("Paiements activés : les élèves peuvent désormais réserver vos cours payants.", "success")
    else:
        flash("Votre dossier Stripe n'est pas encore complet ou est en cours de vérification.", "error")
    return redirect(url_for("paiements"))


@app.route("/paiements/stripe/tableau-de-bord")
@login_required(role="prof")
def stripe_connect_tableau():
    user = current_user()
    if not (STRIPE_SECRET_KEY and user["stripe_account_id"]):
        return redirect(url_for("paiements"))
    try:
        link = to_plain(stripe.Account.create_login_link(user["stripe_account_id"]))
    except stripe.StripeError:
        flash("Le tableau de bord Stripe n'est pas accessible pour le moment.", "error")
        return redirect(url_for("paiements"))
    return redirect(link["url"], code=303)


@app.route("/facture/<int:order_id>")
@login_required()
def telecharger_facture(order_id):
    db = get_db()
    user = current_user()
    order = db.execute("SELECT * FROM orders WHERE id = ? AND invoice_number IS NOT NULL", (order_id,)).fetchone()
    if not order:
        abort(404)
    if user["id"] not in (order["student_id"], order["teacher_id"]) and user["role"] != "admin":
        abort(404)
    items = db.execute(
        "SELECT * FROM order_items WHERE order_id = ? AND status IN ('paid', 'refunded') ORDER BY slot_date, slot_time",
        (order_id,),
    ).fetchall()
    pdf = build_invoice_pdf(order, items)
    prefix = "facture" if order["teacher_status"] in PRO_STATUSES else "recu"
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{prefix}-{order['invoice_number']}.pdf")


@app.route("/avoir/<int:item_id>")
@login_required()
def telecharger_avoir(item_id):
    db = get_db()
    user = current_user()
    item = db.execute("SELECT * FROM order_items WHERE id = ? AND credit_note_number IS NOT NULL", (item_id,)).fetchone()
    order = item and db.execute("SELECT * FROM orders WHERE id = ?", (item["order_id"],)).fetchone()
    if not order or (user["id"] not in (order["student_id"], order["teacher_id"]) and user["role"] != "admin"):
        abort(404)
    return send_file(build_credit_note_pdf(order, item), mimetype="application/pdf", as_attachment=True,
                     download_name=f"avoir-{item['credit_note_number']}.pdf")


@app.route("/paiements/recapitulatif/<int:year>")
@login_required(role="prof")
def recapitulatif_annuel(year):
    if not (2020 <= year <= now_local().year):
        abort(404)
    user = current_user()
    summary = teacher_year_summary(get_db(), user["id"], year)
    return send_file(build_year_summary_pdf(user, summary), mimetype="application/pdf", as_attachment=True,
                     download_name=f"recapitulatif-{year}.pdf")


# ---------------------------------------------------------------------------
# Authentification : connexion, inscription, déconnexion
# ---------------------------------------------------------------------------
@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    if current_user():
        return redirect(dashboard_url_for(current_user()["role"]))
    if request.method == "POST":
        email = clean(request.form.get("email", ""), LIMITS["email"]).lower()
        password = request.form.get("password", "")[:MAX_PASSWORD_LENGTH + 1]
        if is_rate_limited(f"login-ip:{client_ip()}", 20, 900) or is_rate_limited(f"login-mail:{email}", 8, 900):
            flash("Trop de tentatives de connexion. Réessayez dans un quart d'heure.", "error")
            return render_template("connexion.html", email=email), 429
        user = get_db().execute("SELECT * FROM users WHERE email = ? AND deleted_at IS NULL", (email,)).fetchone()
        valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        if user and valid and len(password) <= MAX_PASSWORD_LENGTH:
            login_user(user)
            if user["role"] == "admin":
                db = get_db()
                audit(db, "connexion_admin", actor_id=user["id"])
                db.commit()
            if user["must_change_password"]:
                flash("Votre mot de passe a été réinitialisé : choisissez-en un nouveau.", "error")
                return redirect(url_for("parametres"))
            return redirect(dashboard_url_for(user["role"]))
        flash("Adresse e-mail ou mot de passe incorrect.", "error")
        return render_template("connexion.html", email=email)
    return render_template("connexion.html", email="")


@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    if current_user():
        return redirect(dashboard_url_for(current_user()["role"]))
    registration_mode = get_registration_mode()
    if request.method == "POST":
        if is_rate_limited(f"signup:{client_ip()}", 8, 3600):
            flash("Trop d'inscriptions depuis cette adresse. Réessayez plus tard.", "error")
            return render_template("inscription.html", registration_mode=registration_mode, form=request.form), 429
        name = form_value("name", "name")
        email = clean(request.form.get("email", ""), LIMITS["email"]).lower()
        password = request.form.get("password", "")
        role = request.form.get("role")
        errors = []
        if role not in ("prof", "etudiant"):
            errors.append("Choisissez un type de compte.")
        elif registration_mode == "profs" and role == "etudiant":
            errors.append("Les inscriptions sont actuellement réservées aux professeurs.")
        if len(name) < 2:
            errors.append("Indiquez votre nom.")
        elif any(word in name.lower() for word in RESERVED_NAME_WORDS):
            errors.append("Ce nom n'est pas autorisé (il pourrait être confondu avec l'équipe du site).")
        if not EMAIL_RE.match(email):
            errors.append("L'adresse e-mail n'est pas valide.")
        problem = password_problem(password)
        if problem:
            errors.append(problem)
        if not request.form.get("adult"):
            errors.append("Le compte doit être créé par une personne majeure (l'élève majeur ou son représentant légal).")
        if not request.form.get("accept_terms"):
            errors.append("Vous devez accepter les conditions générales d'utilisation.")
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            errors.append("Un compte existe déjà avec cette adresse e-mail.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("inscription.html", registration_mode=registration_mode, form=request.form)
        cur = db.execute(
            "INSERT INTO users(name, email, password_hash, role, approved, cgu_accepted_at, cgu_version, created_at, last_login_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (name, email, generate_password_hash(password), role, 0 if role == "prof" else 1, TERMS_VERSION),
        )
        db.commit()
        login_user(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
        if role == "prof":
            flash("Compte créé. Votre profil sera visible après validation par l'équipe ; "
                  "complétez-le en attendant.", "success")
            return redirect(url_for("prof_profil"))
        flash("Compte créé.", "success")
        return redirect(url_for("cours"))
    return render_template("inscription.html", registration_mode=registration_mode, form={})


@app.route("/deconnexion", methods=["POST"])
def deconnexion():
    session.clear()
    flash("Vous êtes déconnecté.", "success")
    return redirect(url_for("presentation"))


# ---------------------------------------------------------------------------
# Fichiers téléversés
# ---------------------------------------------------------------------------
@app.route("/photos/<filename>")
def photo_profil(filename):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", filename):
        abort(404)
    db = get_db()
    owner = db.execute("SELECT * FROM users WHERE photo = ? AND deleted_at IS NULL", (filename,)).fetchone()
    if not owner:
        abort(404)
    public = owner["role"] == "prof" and owner["approved"]
    if not public:
        viewer = current_user()
        allowed = viewer and (
            viewer["id"] == owner["id"] or viewer["role"] == "admin"
            or db.execute(
                "SELECT 1 FROM contacts WHERE (student_id = ? AND teacher_id = ?) OR (student_id = ? AND teacher_id = ?)",
                (viewer["id"], owner["id"], owner["id"], viewer["id"]),
            ).fetchone()
        )
        if not allowed:
            abort(404)
    response = send_from_directory(PROFILE_PHOTOS_DIR, filename, mimetype="image/jpeg", max_age=0 if not public else 3600)
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    if not public:
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/messagerie/pieces-jointes/<int:message_id>/<filename>")
@login_required()
def uploaded_message_attachment(message_id, filename):
    user = current_user()
    message = get_db().execute(
        "SELECT * FROM messages WHERE id = ? AND attachment_filename = ?", (message_id, filename)
    ).fetchone()
    if not message or (user["id"] not in (message["sender_id"], message["recipient_id"]) and user["role"] != "admin"):
        abort(404)
    is_pdf = message["attachment_type"] == "pdf"
    response = send_from_directory(
        MESSAGE_ATTACHMENTS_DIR, message["attachment_filename"],
        as_attachment=is_pdf,
        download_name=message["attachment_original_name"] or message["attachment_filename"],
        max_age=0,
    )
    response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ---------------------------------------------------------------------------
# Professeur
# ---------------------------------------------------------------------------
@app.route("/prof/tableau-de-bord")
@login_required(role="prof")
def prof_dashboard():
    db = get_db()
    user = current_user()
    courses = db.execute("SELECT * FROM courses WHERE teacher_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
    slots_map = get_slots_map(db, [c["id"] for c in courses])
    item_map = {}
    for row in db.execute(
        """
        SELECT oi.slot_id, oi.status, oi.amount_cents, o.id AS order_id, o.invoice_number
        FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE o.teacher_id = ? AND oi.status IN ('pending', 'paid', 'free') AND oi.slot_id IS NOT NULL
        """,
        (user["id"],),
    ).fetchall():
        item_map[row["slot_id"]] = row
    now = now_local()
    upcoming = sorted(
        [(c, s) for c in courses for s in slots_map.get(c["id"], []) if s["reserved_by"] and safe_slot_end(s) > now],
        key=lambda cs: safe_slot_start(cs[1]),
    )
    return render_template(
        "prof_dashboard.html",
        courses=courses,
        slots_map=slots_map,
        item_map=item_map,
        upcoming=upcoming,
        my_rating=get_teacher_ratings_map(db, [user["id"]]).get(user["id"]),
        payments_ready=teacher_can_receive_payments(user),
        stripe_configured=bool(STRIPE_SECRET_KEY),
        cancellable={s["id"]: (safe_slot_start(s) - now >= CANCELLATION_MIN_DELAY) for _, s in upcoming},
    )


def handle_profile_form(user, template, extra):
    """Traitement commun des profils professeur et élève."""
    db = get_db()
    bio = form_value("bio", "bio", multiline=True)
    level = clean(request.form.get("education_level", ""), 60)
    allowed_levels = NIVEAUX_ETUDE_PROF if user["role"] == "prof" else NIVEAUX
    if level and level not in allowed_levels:
        level = ""
    updates = {"bio": bio or None, "education_level": level or None}
    if user["role"] == "prof":
        status = request.form.get("teacher_status", "")
        siret = re.sub(r"\s", "", request.form.get("siret", ""))
        if status not in TEACHER_STATUSES:
            flash("Indiquez votre statut.", "error")
            return render_template(template, user=user, **extra)
        if status in PRO_STATUSES and not siret_is_valid(siret):
            flash("Le numéro SIRET n'est pas valide (14 chiffres).", "error")
            return render_template(template, user=user, **extra)
        updates["teacher_status"] = status
        updates["siret"] = siret if status in PRO_STATUSES else None
    photo = request.files.get("photo")
    old_photo = None
    if photo and photo.filename:
        try:
            updates["photo"] = save_profile_photo(photo)
            old_photo = user["photo"]
        except UploadError as exc:
            flash(str(exc), "error")
            return render_template(template, user=user, **extra)
    if request.form.get("remove_photo") and "photo" not in updates:
        updates["photo"] = None
        old_photo = user["photo"]
    assignments = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user["id"]))
    db.commit()
    delete_file_quietly(PROFILE_PHOTOS_DIR, old_photo)
    flash("Profil enregistré.", "success")
    return None


@app.route("/prof/profil", methods=["GET", "POST"])
@login_required(role="prof")
def prof_profil():
    user = current_user()
    extra = {"niveaux_etude": NIVEAUX_ETUDE_PROF}
    if request.method == "POST":
        result = handle_profile_form(user, "prof_profil.html", extra)
        if result is not None:
            return result
        return redirect(url_for("prof_profil"))
    return render_template("prof_profil.html", user=user, **extra)


def parse_slots_from_form():
    errors, slots = [], []
    now = now_local()
    for date_val, time_val, duration_val in zip(
        request.form.getlist("slot_date[]"), request.form.getlist("slot_time[]"), request.form.getlist("slot_duration[]")
    ):
        date_val, time_val, duration_val = date_val.strip(), time_val.strip()[:5], duration_val.strip()
        if not (date_val or time_val):
            continue
        try:
            start = datetime.strptime(f"{date_val} {time_val}", "%Y-%m-%d %H:%M")
            duration = int(duration_val)
        except ValueError:
            errors.append("Chaque créneau doit avoir une date, une heure et une durée valides.")
            continue
        if not (15 <= duration <= 480):
            errors.append("La durée d'une séance doit être comprise entre 15 minutes et 8 heures.")
            continue
        if start <= now + BOOKING_MIN_NOTICE or start > now + timedelta(days=366):
            errors.append("Les créneaux doivent être situés dans le futur, à moins d'un an.")
            continue
        slots.append((start.strftime("%Y-%m-%d"), start.strftime("%H:%M"), duration))
    if len(slots) > MAX_SLOTS_PER_COURSE:
        errors.append(f"{MAX_SLOTS_PER_COURSE} créneaux au maximum par cours.")
    return slots, errors


@app.route("/prof/creer", methods=["GET", "POST"])
@login_required(role="prof")
def prof_creer():
    user = current_user()
    context = {"matieres": MATIERES, "niveaux": NIVEAUX, "modes": MODES}
    if not user["approved"]:
        flash("Votre profil doit être validé par l'équipe avant de publier des cours.", "error")
        return redirect(url_for("prof_dashboard"))
    if user["teacher_status"] not in TEACHER_STATUSES:
        flash("Indiquez d'abord votre statut dans votre profil.", "error")
        return redirect(url_for("prof_profil"))
    if request.method == "POST":
        title = form_value("title", "title")
        subjects = [s for s in request.form.getlist("subject") if s in MATIERES]
        levels = [n for n in request.form.getlist("level") if n in NIVEAUX]
        description = form_value("description", "description", multiline=True)
        mode = request.form.get("mode", "")
        city = form_value("city", "city")
        errors = []
        if len(title) < 3:
            errors.append("Indiquez un titre.")
        if not subjects:
            errors.append("Choisissez au moins une matière.")
        if not levels:
            errors.append("Choisissez au moins un niveau.")
        if len(description) < 10:
            errors.append("Ajoutez une description d'au moins quelques mots.")
        if mode not in MODES:
            errors.append("Indiquez si le cours a lieu en ligne, en présentiel, ou les deux.")
        if mode in ("presentiel", "les_deux") and not city:
            errors.append("Indiquez la ville des cours en présentiel.")
        if mode == "en_ligne":
            city = ""
        try:
            price_cents = parse_euros(request.form.get("price", ""))
            if price_cents and not (PRICE_MIN_CENTS <= price_cents <= PRICE_MAX_CENTS):
                raise ValueError
        except ValueError:
            price_cents = None
            errors.append(f"Indiquez un prix entre {PRICE_MIN_CENTS // 100} € et {PRICE_MAX_CENTS // 100} € (ou 0 pour un cours gratuit).")
        if price_cents and not teacher_can_receive_payments(user):
            errors.append("Activez d'abord les paiements (rubrique « Paiements ») pour proposer un cours payant.")
        slots, slot_errors = parse_slots_from_form()
        errors += slot_errors
        if not slots and not slot_errors:
            errors.append("Ajoutez au moins un créneau.")
        if errors:
            for e in dict.fromkeys(errors):
                flash(e, "error")
            return render_template("prof_creer.html", form=request.form, selected_subjects=subjects,
                                   selected_levels=levels, **context)
        db = get_db()
        cur = db.execute(
            "INSERT INTO courses(title, subject, level, description, teacher_id, mode, city, price_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, ", ".join(subjects), ", ".join(levels), description, user["id"], mode, city or None, price_cents),
        )
        for date_val, time_val, duration in slots:
            db.execute(
                "INSERT INTO slots(course_id, slot_date, slot_time, duration_minutes) VALUES (?, ?, ?, ?)",
                (cur.lastrowid, date_val, time_val, duration),
            )
        db.commit()
        flash(f"Cours publié : {title}", "success")
        return redirect(url_for("prof_dashboard"))
    return render_template("prof_creer.html", form={}, selected_subjects=[], selected_levels=[], **context)


def delete_course_with_refunds(db, course, actor_id, reason):
    """Supprime un cours après remboursement des séances à venir. Les
    séances passées restent dans les commandes (justificatifs)."""
    slots = db.execute("SELECT * FROM slots WHERE course_id = ?", (course["id"],)).fetchall()
    now = now_local()
    for slot in slots:
        if slot["reserved_by"] and safe_slot_end(slot) > now:
            cancel_reservation(db, slot, reason, actor_id=actor_id)
    db.execute("DELETE FROM courses WHERE id = ?", (course["id"],))
    audit(db, "suppression_cours", target=f"course:{course['id']}", details=reason, actor_id=actor_id)


@app.route("/prof/cours/<int:course_id>/supprimer", methods=["POST"])
@login_required(role="prof")
def prof_supprimer(course_id):
    db = get_db()
    user = current_user()
    course = db.execute("SELECT * FROM courses WHERE id = ? AND teacher_id = ?", (course_id, user["id"])).fetchone()
    if not course:
        abort(404)
    now = now_local()
    blocking = [
        s for s in db.execute("SELECT * FROM slots WHERE course_id = ? AND reserved_by IS NOT NULL", (course_id,)).fetchall()
        if safe_slot_end(s) > now and safe_slot_start(s) - now < CANCELLATION_MIN_DELAY
    ]
    if blocking:
        flash("Impossible de supprimer ce cours : une séance réservée commence dans moins de 12 heures.", "error")
        return redirect(url_for("prof_dashboard"))
    try:
        delete_course_with_refunds(db, course, user["id"], "Cours annulé par le professeur")
        db.commit()
    except PaymentError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("prof_dashboard"))
    flash("Cours supprimé. Les élèves concernés ont été remboursés.", "success")
    return redirect(url_for("prof_dashboard"))


@app.route("/prof/creneaux/<int:slot_id>/annuler", methods=["POST"])
@login_required(role="prof")
def prof_annuler_creneau(slot_id):
    db = get_db()
    user = current_user()
    slot = db.execute(
        "SELECT sl.* FROM slots sl JOIN courses c ON c.id = sl.course_id WHERE sl.id = ? AND c.teacher_id = ?",
        (slot_id, user["id"]),
    ).fetchone()
    if not slot:
        abort(404)
    now = now_local()
    if slot["reserved_by"] is None:
        db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        db.commit()
        flash("Créneau retiré.", "success")
        return redirect(url_for("prof_dashboard"))
    if safe_slot_start(slot) - now < CANCELLATION_MIN_DELAY:
        flash("Cette séance commence dans moins de 12 heures et ne peut plus être annulée en ligne. "
              "Prévenez l'élève et contactez l'équipe via le formulaire de signalement.", "error")
        return redirect(url_for("prof_dashboard"))
    try:
        cancel_reservation(db, slot, "Séance annulée par le professeur", actor_id=user["id"])
        db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        db.commit()
    except PaymentError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("prof_dashboard"))
    flash("Séance annulée, l'élève a été remboursé.", "success")
    return redirect(url_for("prof_dashboard"))


# ---------------------------------------------------------------------------
# Élève
# ---------------------------------------------------------------------------
@app.route("/etudiant/profil", methods=["GET", "POST"])
@login_required(role="etudiant")
def etudiant_profil():
    user = current_user()
    extra = {"niveaux": NIVEAUX}
    if request.method == "POST":
        result = handle_profile_form(user, "etudiant_profil.html", extra)
        if result is not None:
            return result
        return redirect(url_for("etudiant_profil"))
    return render_template("etudiant_profil.html", user=user, **extra)


@app.route("/etudiant/mes-cours")
@login_required(role="etudiant")
def etudiant():
    db = get_db()
    user = current_user()
    rows = db.execute(
        """
        SELECT sl.*, c.title, c.subject, c.level, c.teacher_id, c.city AS course_city,
               u.name AS teacher_name, u.deleted_at AS teacher_deleted
        FROM slots sl JOIN courses c ON c.id = sl.course_id JOIN users u ON u.id = c.teacher_id
        WHERE sl.reserved_by = ?
        ORDER BY sl.slot_date, sl.slot_time
        """,
        (user["id"],),
    ).fetchall()
    items = {}
    for r in rows:
        items[r["id"]] = active_item_for_slot(db, r)
    now = now_local()
    upcoming = [r for r in rows if safe_slot_end(r) > now]
    past = [r for r in rows if safe_slot_end(r) <= now][::-1]
    rated = {r["teacher_id"]: r["rating"] for r in db.execute(
        "SELECT teacher_id, rating FROM ratings WHERE student_id = ?", (user["id"],)
    ).fetchall()}
    rate_targets = {}
    for r in past:
        if r["reservation_status"] == "confirmed" and not r["teacher_deleted"]:
            rate_targets.setdefault(r["teacher_id"], r["teacher_name"])
    pending_orders = db.execute(
        "SELECT * FROM orders WHERE student_id = ? AND status = 'pending' ORDER BY id DESC", (user["id"],)
    ).fetchall()
    legacy_balance = user["wallet_cents"] if "wallet_cents" in user.keys() else 0
    return render_template(
        "etudiant.html", upcoming=upcoming, past=past, items=items, rated=rated, rate_targets=rate_targets,
        pending_orders=pending_orders, legacy_balance=legacy_balance or 0,
        can_cancel={r["id"]: (items[r["id"]] and items[r["id"]]["status"] == "pending") or student_can_cancel(r, items[r["id"]])
                    for r in upcoming},
    )


def has_completed_session_with(db, student_id, teacher_id):
    rows = db.execute(
        "SELECT oi.slot_date, oi.slot_time, oi.duration_minutes FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE o.student_id = ? AND o.teacher_id = ? AND oi.status IN ('paid', 'free')",
        (student_id, teacher_id),
    ).fetchall()
    rows += db.execute(
        "SELECT sl.slot_date, sl.slot_time, sl.duration_minutes FROM slots sl JOIN courses c ON c.id = sl.course_id "
        "WHERE sl.reserved_by = ? AND c.teacher_id = ? AND sl.reservation_status = 'confirmed'",
        (student_id, teacher_id),
    ).fetchall()
    now = now_local()
    return any(safe_slot_end(r) <= now for r in rows)


@app.route("/profs/<int:teacher_id>/noter", methods=["POST"])
@login_required(role="etudiant")
def noter_prof(teacher_id):
    db = get_db()
    user = current_user()
    teacher = db.execute("SELECT * FROM users WHERE id = ? AND role = 'prof' AND deleted_at IS NULL", (teacher_id,)).fetchone()
    if not teacher:
        abort(404)
    if not has_completed_session_with(db, user["id"], teacher_id):
        flash("Vous pourrez noter ce professeur après avoir suivi une séance avec lui.", "error")
        return redirect(url_for("etudiant"))
    try:
        rating = int(request.form.get("rating", ""))
    except ValueError:
        rating = 0
    if not 1 <= rating <= 5:
        flash("Choisissez une note entre 1 et 5.", "error")
        return redirect(url_for("etudiant"))
    db.execute(
        "INSERT INTO ratings(student_id, teacher_id, rating) VALUES (?, ?, ?) "
        "ON CONFLICT(student_id, teacher_id) DO UPDATE SET rating = excluded.rating, created_at = CURRENT_TIMESTAMP",
        (user["id"], teacher_id, rating),
    )
    db.commit()
    flash(f"Note enregistrée : {rating}/5.", "success")
    return redirect(url_for("etudiant"))


@app.route("/etudiant/calendrier")
@login_required(role="etudiant")
def etudiant_calendrier():
    db = get_db()
    year, month = resolve_month(request.args)
    (py, pm), (ny, nm) = month_nav(year, month)
    events = {}
    for s in db.execute(
        "SELECT sl.*, c.title, c.subject, c.level, u.name AS teacher_name FROM slots sl "
        "JOIN courses c ON c.id = sl.course_id JOIN users u ON u.id = c.teacher_id WHERE sl.reserved_by = ?",
        (current_user()["id"],),
    ).fetchall():
        events.setdefault(s["slot_date"], []).append({
            "title": s["title"], "subtitle": f"{s['subject']} · {s['level']} · {s['teacher_name']}",
            "time": s["slot_time"], "duration": s["duration_minutes"], "status": "reserve",
        })
    return render_template(
        "calendrier.html", weeks=build_month_weeks(year, month, events), month_label=f"{MOIS_FR_CAP[month]} {year}",
        jours=JOURS_COURTS, prev_link=url_for("etudiant_calendrier", year=py, month=pm),
        next_link=url_for("etudiant_calendrier", year=ny, month=nm), today_link=url_for("etudiant_calendrier"),
        role_mode="etudiant",
    )


@app.route("/prof/calendrier")
@login_required(role="prof")
def prof_calendrier():
    db = get_db()
    year, month = resolve_month(request.args)
    (py, pm), (ny, nm) = month_nav(year, month)
    events = {}
    for s in db.execute(
        "SELECT sl.*, c.title, c.subject, c.level, st.name AS student_name FROM slots sl "
        "JOIN courses c ON c.id = sl.course_id LEFT JOIN users st ON st.id = sl.reserved_by WHERE c.teacher_id = ?",
        (current_user()["id"],),
    ).fetchall():
        reserved = s["reserved_by"] is not None
        events.setdefault(s["slot_date"], []).append({
            "title": s["title"],
            "subtitle": f"{s['subject']} · {s['level']} · " + (s["student_name"] or "") if reserved else "libre",
            "time": s["slot_time"], "duration": s["duration_minutes"], "status": "reserve" if reserved else "disponible",
        })
    return render_template(
        "calendrier.html", weeks=build_month_weeks(year, month, events), month_label=f"{MOIS_FR_CAP[month]} {year}",
        jours=JOURS_COURTS, prev_link=url_for("prof_calendrier", year=py, month=pm),
        next_link=url_for("prof_calendrier", year=ny, month=nm), today_link=url_for("prof_calendrier"),
        role_mode="prof",
    )


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------
@app.route("/admin/tableau-de-bord")
@login_required(role="admin")
def admin_dashboard():
    db = get_db()
    users_sql = (
        "SELECT u.*, (SELECT COUNT(*) FROM courses c WHERE c.teacher_id = u.id) AS course_count, "
        "(SELECT COUNT(*) FROM slots s WHERE s.reserved_by = u.id) AS booking_count "
        "FROM users u WHERE u.role = ? AND u.deleted_at IS NULL {extra} ORDER BY u.name"
    )
    profs = db.execute(users_sql.format(extra="AND u.approved = 1"), ("prof",)).fetchall()
    pending = db.execute(users_sql.format(extra="AND u.approved = 0"), ("prof",)).fetchall()
    students = db.execute(users_sql.format(extra=""), ("etudiant",)).fetchall()
    courses = db.execute(
        "SELECT c.*, u.name AS teacher_name, (SELECT COUNT(*) FROM slots s WHERE s.course_id = c.id) AS slot_count, "
        "(SELECT COUNT(*) FROM slots s WHERE s.course_id = c.id AND s.reserved_by IS NOT NULL) AS reserved_count "
        "FROM courses c JOIN users u ON u.id = c.teacher_id ORDER BY c.id DESC LIMIT 300"
    ).fetchall()
    fees = db.execute(
        "SELECT COALESCE(SUM(platform_fee_cents), 0) FROM order_items WHERE status = 'paid'"
    ).fetchone()[0]
    legacy_count = db.execute(
        "SELECT (SELECT COUNT(*) FROM users WHERE wallet_cents != 0) + (SELECT COUNT(*) FROM payments WHERE status = 'held') "
        "+ (SELECT COUNT(*) FROM withdrawals WHERE status = 'pending')"
    ).fetchone()[0]
    return render_template(
        "admin_dashboard.html", profs=profs, pending=pending, students=students, courses=courses,
        registration_mode=get_registration_mode(), fees_total=fees, legacy_count=legacy_count,
        teacher_statuses=TEACHER_STATUSES,
    )


@app.route("/admin/parametres/inscriptions", methods=["POST"])
@login_required(role="admin")
def admin_definir_mode_inscription():
    mode = request.form.get("registration_mode")
    if mode not in ("tous", "profs"):
        abort(400)
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES ('registration_mode', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (mode,),
    )
    audit(db, "mode_inscription", details=mode)
    db.commit()
    flash("Réglage enregistré.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/utilisateurs/<int:user_id>/valider", methods=["POST"])
@login_required(role="admin")
def admin_valider_prof(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ? AND role = 'prof' AND deleted_at IS NULL", (user_id,)).fetchone()
    if not target:
        abort(404)
    db.execute("UPDATE users SET approved = 1 WHERE id = ?", (user_id,))
    audit(db, "validation_prof", target=f"user:{user_id}")
    db.commit()
    flash(f"Profil de {target['name']} validé.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/utilisateurs/<int:user_id>/supprimer", methods=["POST"])
@login_required(role="admin")
def admin_supprimer_utilisateur(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,)).fetchone()
    if not target or target["role"] == "admin":
        flash("Ce compte ne peut pas être supprimé.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        erase_user(db, target, "suppression par l'administration", actor_id=current_user()["id"], force_refunds=True)
        db.commit()
    except PaymentError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin_dashboard"))
    flash(f"Compte de {target['name']} supprimé (séances à venir remboursées).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/utilisateurs/<int:user_id>/reinitialiser-mot-de-passe", methods=["POST"])
@login_required(role="admin")
def admin_reinitialiser_mot_de_passe(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,)).fetchone()
    if not target or target["role"] == "admin":
        flash("Ce mot de passe ne peut pas être réinitialisé ici.", "error")
        return redirect(url_for("admin_dashboard"))
    temporary = secrets.token_urlsafe(12)
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (generate_password_hash(temporary), user_id),
    )
    audit(db, "reinitialisation_mot_de_passe", target=f"user:{user_id}")
    db.commit()
    return render_template("admin_mot_de_passe.html", target=target, temporary=temporary)


@app.route("/admin/cours/<int:course_id>/supprimer", methods=["POST"])
@login_required(role="admin")
def admin_supprimer_cours(course_id):
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        abort(404)
    try:
        delete_course_with_refunds(db, course, current_user()["id"], "Cours retiré par l'administration")
        db.commit()
    except PaymentError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin_dashboard"))
    flash(f"Cours « {course['title']} » supprimé.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/paiements")
@login_required(role="admin")
def admin_paiements():
    db = get_db()
    orders = db.execute(
        "SELECT * FROM orders WHERE status NOT IN ('canceled', 'expired') ORDER BY id DESC LIMIT 300"
    ).fetchall()
    items_by_order = {}
    if orders:
        placeholders = ",".join("?" for _ in orders)
        for it in db.execute(f"SELECT * FROM order_items WHERE order_id IN ({placeholders})", [o["id"] for o in orders]).fetchall():
            items_by_order.setdefault(it["order_id"], []).append(it)
    totals = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS gross, COALESCE(SUM(platform_fee_cents), 0) AS fees "
        "FROM order_items WHERE status = 'paid'"
    ).fetchone()
    return render_template("admin_paiements.html", orders=orders, items_by_order=items_by_order, totals=totals)


@app.route("/admin/paiements/<int:item_id>/rembourser", methods=["POST"])
@login_required(role="admin")
def admin_rembourser(item_id):
    db = get_db()
    reason = form_value("reason", "admin_note") or "Remboursement décidé par l'administration"
    try:
        refund_order_item(db, item_id, reason, actor_id=current_user()["id"])
        db.commit()
    except PaymentError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin_paiements"))
    flash("Séance remboursée.", "success")
    return redirect(url_for("admin_paiements"))


@app.route("/admin/signalements")
@login_required(role="admin")
def admin_signalements():
    reports = get_db().execute(
        "SELECT r.*, oi.order_id FROM reports r LEFT JOIN order_items oi ON oi.id = r.order_item_id "
        "ORDER BY (r.status = 'open') DESC, r.id DESC LIMIT 300"
    ).fetchall()
    return render_template("admin_signalements.html", reports=reports, categories=REPORT_CATEGORIES)


@app.route("/admin/signalements/<int:report_id>/clore", methods=["POST"])
@login_required(role="admin")
def admin_signalement_clore(report_id):
    db = get_db()
    note = form_value("admin_note", "admin_note") or None
    db.execute(
        "UPDATE reports SET status = 'closed', admin_note = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (note, report_id),
    )
    audit(db, "signalement_clos", target=f"report:{report_id}", details=note)
    db.commit()
    flash("Signalement clos.", "success")
    return redirect(url_for("admin_signalements"))


@app.route("/admin/anciens-soldes")
@login_required(role="admin")
def admin_anciens_soldes():
    db = get_db()
    return render_template(
        "admin_anciens_soldes.html",
        balances=db.execute("SELECT * FROM users WHERE wallet_cents != 0 ORDER BY wallet_cents DESC").fetchall(),
        held=db.execute(
            "SELECT p.*, s.name AS student_name, t.name AS teacher_name FROM payments p "
            "JOIN users s ON s.id = p.student_id JOIN users t ON t.id = p.teacher_id WHERE p.status = 'held' ORDER BY p.id"
        ).fetchall(),
        withdrawals=db.execute(
            "SELECT w.*, u.name AS user_name, u.email AS user_email FROM withdrawals w JOIN users u ON u.id = w.user_id "
            "WHERE w.status = 'pending' ORDER BY w.id"
        ).fetchall(),
    )


@app.route("/admin/anciens-soldes/utilisateurs/<int:user_id>/regler", methods=["POST"])
@login_required(role="admin")
def admin_ancien_solde_regler(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target or not target["wallet_cents"]:
        abort(404)
    amount = target["wallet_cents"]
    db.execute(
        "INSERT INTO wallet_transactions(user_id, amount_cents, kind, description) VALUES (?, ?, 'withdrawal', ?)",
        (user_id, -amount, "Solde de l'ancien porte-monnaie réglé hors plateforme"),
    )
    db.execute("UPDATE users SET wallet_cents = 0, legacy_settled_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    audit(db, "ancien_solde_regle", target=f"user:{user_id}", details=f"{amount} c.")
    db.commit()
    flash("Solde marqué comme réglé.", "success")
    return redirect(url_for("admin_anciens_soldes"))


@app.route("/admin/anciens-soldes/paiements/<int:payment_id>/regler", methods=["POST"])
@login_required(role="admin")
def admin_ancien_paiement_regler(payment_id):
    db = get_db()
    outcome = request.form.get("outcome")
    if outcome not in ("released", "refunded"):
        abort(400)
    if db.execute(
        "UPDATE payments SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'held'",
        (outcome, payment_id),
    ).rowcount:
        audit(db, "ancien_paiement_regle", target=f"payment:{payment_id}", details=outcome)
        db.commit()
        flash("Paiement marqué comme réglé.", "success")
    return redirect(url_for("admin_anciens_soldes"))


@app.route("/admin/anciens-soldes/retraits/<int:withdrawal_id>/<action>", methods=["POST"])
@login_required(role="admin")
def admin_ancien_retrait(withdrawal_id, action):
    db = get_db()
    demande = db.execute("SELECT * FROM withdrawals WHERE id = ? AND status = 'pending'", (withdrawal_id,)).fetchone()
    if not demande or action not in ("payer", "refuser"):
        abort(404)
    if action == "payer":
        db.execute("UPDATE withdrawals SET status = 'paid', resolved_at = CURRENT_TIMESTAMP WHERE id = ?", (withdrawal_id,))
    else:
        db.execute("UPDATE users SET wallet_cents = wallet_cents + ? WHERE id = ?", (demande["amount_cents"], demande["user_id"]))
        db.execute(
            "INSERT INTO wallet_transactions(user_id, amount_cents, kind, description) VALUES (?, ?, 'refund', ?)",
            (demande["user_id"], demande["amount_cents"], "Retrait refusé : montant recrédité"),
        )
        db.execute(
            "UPDATE withdrawals SET status = 'rejected', admin_note = 'Refusé', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (withdrawal_id,),
        )
    audit(db, f"ancien_retrait_{action}", target=f"withdrawal:{withdrawal_id}")
    db.commit()
    flash("Demande de retrait traitée.", "success")
    return redirect(url_for("admin_anciens_soldes"))


@app.route("/admin/journal")
@login_required(role="admin")
def admin_journal():
    rows = get_db().execute(
        "SELECT a.*, u.name AS actor_name FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id ORDER BY a.id DESC LIMIT 500"
    ).fetchall()
    return render_template("admin_journal.html", rows=rows)


@app.route("/admin/messagerie")
@login_required(role="admin")
def admin_messagerie():
    conversations = get_db().execute(
        """
        SELECT ct.student_id, ct.teacher_id, s.name AS student_name, t.name AS teacher_name,
               (SELECT COUNT(*) FROM messages m WHERE (m.sender_id = ct.student_id AND m.recipient_id = ct.teacher_id)
                   OR (m.sender_id = ct.teacher_id AND m.recipient_id = ct.student_id)) AS message_count,
               (SELECT MAX(m.created_at) FROM messages m WHERE (m.sender_id = ct.student_id AND m.recipient_id = ct.teacher_id)
                   OR (m.sender_id = ct.teacher_id AND m.recipient_id = ct.student_id)) AS last_at
        FROM contacts ct JOIN users s ON s.id = ct.student_id JOIN users t ON t.id = ct.teacher_id
        GROUP BY ct.student_id, ct.teacher_id
        ORDER BY last_at IS NULL, last_at DESC
        """
    ).fetchall()
    return render_template("admin_messagerie.html", conversations=conversations)


@app.route("/admin/messagerie/<int:student_id>/<int:teacher_id>", methods=["GET", "POST"])
@login_required(role="admin")
def admin_messagerie_thread(student_id, teacher_id):
    """Consultation d'une conversation par la modération : uniquement sur
    motif (signalement, protection d'un mineur…), motif obligatoire et
    journalisé (proportionnalité exigée par le RGPD)."""
    db = get_db()
    if not db.execute("SELECT 1 FROM contacts WHERE student_id = ? AND teacher_id = ?", (student_id, teacher_id)).fetchone():
        abort(404)
    student = db.execute("SELECT * FROM users WHERE id = ?", (student_id,)).fetchone()
    teacher = db.execute("SELECT * FROM users WHERE id = ?", (teacher_id,)).fetchone()
    if request.method == "POST":
        reason = form_value("reason", "admin_note")
        if len(reason) < 5:
            flash("Indiquez le motif de la consultation.", "error")
            return render_template("admin_messagerie_thread.html", student=student, teacher=teacher, thread=None)
        audit(db, "lecture_conversation", target=f"conversation:{student_id}-{teacher_id}", details=reason)
        db.commit()
        thread = db.execute(
            "SELECT * FROM messages WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?) ORDER BY id",
            (student_id, teacher_id, teacher_id, student_id),
        ).fetchall()
        return render_template("admin_messagerie_thread.html", student=student, teacher=teacher, thread=thread, reason=reason)
    return render_template("admin_messagerie_thread.html", student=student, teacher=teacher, thread=None)


# ---------------------------------------------------------------------------
# Messagerie
# ---------------------------------------------------------------------------
def get_contact_preview(db, user_id, other_id):
    last = db.execute(
        "SELECT body, created_at, sender_id, attachment_filename FROM messages "
        "WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?) ORDER BY id DESC LIMIT 1",
        (user_id, other_id, other_id, user_id),
    ).fetchone()
    unread = db.execute(
        "SELECT COUNT(*) FROM messages WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL", (other_id, user_id)
    ).fetchone()[0]
    return last, unread


def get_contacts(user):
    db = get_db()
    if user["role"] == "admin":
        rows = db.execute(
            "SELECT id, name, role FROM users WHERE role IN ('prof', 'etudiant') AND deleted_at IS NULL ORDER BY role, name"
        ).fetchall()
    elif user["role"] == "etudiant":
        rows = db.execute(
            "SELECT u.id, u.name, u.role FROM contacts ct JOIN users u ON u.id = ct.teacher_id "
            "WHERE ct.student_id = ? AND u.deleted_at IS NULL GROUP BY u.id ORDER BY u.name",
            (user["id"],),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT u.id, u.name, u.role FROM contacts ct JOIN users u ON u.id = ct.student_id "
            "WHERE ct.teacher_id = ? AND u.deleted_at IS NULL GROUP BY u.id ORDER BY u.name",
            (user["id"],),
        ).fetchall()
    contacts = []
    for r in rows:
        last, unread = get_contact_preview(db, user["id"], r["id"])
        contacts.append({"id": r["id"], "name": r["name"], "role": r["role"], "last": last, "unread": unread})
    if user["role"] != "admin":
        admin_id = get_admin_id(db)
        if admin_id:
            last, unread = get_contact_preview(db, user["id"], admin_id)
            contacts.append({"id": admin_id, "name": "Équipe " + SITE_NAME, "role": "admin", "last": last, "unread": unread})
    contacts.sort(key=lambda c: (c["last"]["created_at"] if c["last"] else ""), reverse=True)
    return contacts


def is_allowed_contact(user, other_id):
    db = get_db()
    other = db.execute("SELECT role, deleted_at FROM users WHERE id = ?", (other_id,)).fetchone()
    if not other or other["deleted_at"] or other_id == user["id"]:
        return False
    if user["role"] == "admin":
        return other["role"] in ("prof", "etudiant")
    if other["role"] == "admin":
        return True
    if user["role"] == "etudiant":
        return bool(db.execute("SELECT 1 FROM contacts WHERE student_id = ? AND teacher_id = ?", (user["id"], other_id)).fetchone())
    if user["role"] == "prof":
        return bool(db.execute("SELECT 1 FROM contacts WHERE teacher_id = ? AND student_id = ?", (user["id"], other_id)).fetchone())
    return False


@app.route("/messagerie")
@login_required()
def messagerie():
    return render_template("messagerie.html", contacts=get_contacts(current_user()))


@app.route("/messagerie/diffusion", methods=["POST"])
@login_required(role="admin")
def messagerie_diffusion():
    user = current_user()
    body = form_value("body", "message", multiline=True)
    if not body:
        flash("Écrivez un message avant de l'envoyer.", "error")
        return redirect(url_for("messagerie"))
    db = get_db()
    recipients = db.execute("SELECT id FROM users WHERE role IN ('prof', 'etudiant') AND deleted_at IS NULL").fetchall()
    db.executemany(
        "INSERT INTO messages(sender_id, recipient_id, body) VALUES (?, ?, ?)",
        [(user["id"], r["id"], body) for r in recipients],
    )
    audit(db, "message_diffuse", details=f"{len(recipients)} destinataires")
    db.commit()
    flash(f"Message envoyé à {len(recipients)} personne(s).", "success")
    return redirect(url_for("messagerie"))


@app.route("/messagerie/<int:contact_id>", methods=["GET", "POST"])
@login_required()
def messagerie_conversation(contact_id):
    user = current_user()
    db = get_db()
    contact = db.execute("SELECT id, name, role FROM users WHERE id = ?", (contact_id,)).fetchone()
    if not contact or not is_allowed_contact(user, contact_id):
        flash("Vous ne pouvez écrire qu'aux personnes avec qui vous avez réservé une séance, ou à l'équipe.", "error")
        return redirect(url_for("messagerie"))
    if request.method == "POST":
        if is_rate_limited(f"msg:{user['id']}", 30, 60):
            flash("Vous envoyez trop de messages. Patientez une minute.", "error")
            return redirect(url_for("messagerie_conversation", contact_id=contact_id))
        body = form_value("body", "message", multiline=True)
        attachment = request.files.get("attachment")
        stored = original = kind = None
        if attachment and attachment.filename:
            try:
                stored, original, kind = save_message_attachment(attachment)
            except UploadError as exc:
                flash(str(exc), "error")
                return redirect(url_for("messagerie_conversation", contact_id=contact_id))
        if body or stored:
            db.execute(
                "INSERT INTO messages(sender_id, recipient_id, body, attachment_filename, attachment_original_name, attachment_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user["id"], contact_id, body, stored, original, kind),
            )
            db.commit()
        return redirect(url_for("messagerie_conversation", contact_id=contact_id) + "#dernier")
    db.execute(
        "UPDATE messages SET read_at = CURRENT_TIMESTAMP WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
        (contact_id, user["id"]),
    )
    db.commit()
    thread = db.execute(
        "SELECT * FROM messages WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?) ORDER BY id",
        (user["id"], contact_id, contact_id, user["id"]),
    ).fetchall()
    display_name = ("Équipe " + SITE_NAME) if contact["role"] == "admin" and user["role"] != "admin" else contact["name"]
    return render_template("messagerie.html", contacts=get_contacts(user), active_contact=contact,
                           active_name=display_name, thread=thread)


# ---------------------------------------------------------------------------
# Visioconférence et tableau blanc
# ---------------------------------------------------------------------------
def get_course_and_partner(course_id, slot_id, user):
    db = get_db()
    row = db.execute(
        "SELECT sl.*, c.teacher_id, c.title FROM slots sl JOIN courses c ON c.id = sl.course_id WHERE sl.id = ? AND c.id = ?",
        (slot_id, course_id),
    ).fetchone()
    if not row or row["reservation_status"] != "confirmed" or not row["reserved_by"]:
        return None, None, None
    if user["role"] == "prof" and row["teacher_id"] == user["id"]:
        return row, row, row["reserved_by"]
    if user["role"] == "etudiant" and row["reserved_by"] == user["id"]:
        return row, row, row["teacher_id"]
    return None, None, None


def visio_json_guard(course_id, slot_id):
    user = current_user()
    course, slot, partner_id = get_course_and_partner(course_id, slot_id, user)
    if not course:
        return None, None, None, (jsonify({"error": "accès refusé"}), 403)
    return user, slot, partner_id, None


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio")
@login_required(role=("prof", "etudiant"))
def visio(course_id, slot_id):
    user = current_user()
    course, slot, partner_id = get_course_and_partner(course_id, slot_id, user)
    if not course:
        flash("Vous n'avez pas accès à la visioconférence de ce créneau.", "error")
        return redirect(dashboard_url_for(user["role"]))
    if slot["slot_mode"] != "en_ligne":
        flash("Cette séance a lieu en présentiel.", "error")
        return redirect(dashboard_url_for(user["role"]))
    db = get_db()
    partner = db.execute("SELECT id, name FROM users WHERE id = ?", (partner_id,)).fetchone()
    chat = db.execute(
        "SELECT * FROM messages WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?) "
        "ORDER BY id DESC LIMIT 100",
        (user["id"], partner_id, partner_id, user["id"]),
    ).fetchall()[::-1]
    db.execute(
        "UPDATE messages SET read_at = CURRENT_TIMESTAMP WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
        (partner_id, user["id"]),
    )
    db.commit()
    config = {
        "myUserId": user["id"],
        "partnerName": partner["name"],
        "lastChatId": chat[-1]["id"] if chat else 0,
        "stunUrls": WEBRTC_STUN_URLS,
        "urls": {
            "send": url_for("visio_envoyer", course_id=course_id, slot_id=slot_id),
            "poll": url_for("visio_recevoir", course_id=course_id, slot_id=slot_id),
            "chatSend": url_for("visio_chat_envoyer", course_id=course_id, slot_id=slot_id),
            "chatPoll": url_for("visio_chat_recevoir", course_id=course_id, slot_id=slot_id),
            "wbDraw": url_for("visio_tableau_dessiner", course_id=course_id, slot_id=slot_id),
            "wbPoll": url_for("visio_tableau_recevoir", course_id=course_id, slot_id=slot_id),
            "wbUndo": url_for("visio_tableau_annuler", course_id=course_id, slot_id=slot_id),
        },
    }
    return render_template("visio.html", course=course, slot=slot, partner=partner, chat_messages=chat, visio_config=config)


MAX_SIGNAL_BYTES = 20_000


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/envoyer", methods=["POST"])
@login_required(role=("prof", "etudiant"))
def visio_envoyer(course_id, slot_id):
    user, slot, partner_id, error = visio_json_guard(course_id, slot_id)
    if error:
        return error
    if is_rate_limited(f"signal:{user['id']}", 300, 60):
        return jsonify({"error": "trop de requêtes"}), 429
    data = request.get_json(silent=True) or {}
    signal_type = data.get("type")
    payload = json.dumps(data.get("payload"))
    if signal_type not in ("offer", "answer", "candidate", "leave") or payload == "null" or len(payload) > MAX_SIGNAL_BYTES:
        return jsonify({"error": "signal invalide"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO signals(course_id, sender_id, recipient_id, type, payload) VALUES (?, ?, ?, ?, ?)",
        (course_id, user["id"], partner_id, signal_type, payload),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/recevoir")
@login_required(role=("prof", "etudiant"))
def visio_recevoir(course_id, slot_id):
    user, slot, partner_id, error = visio_json_guard(course_id, slot_id)
    if error:
        return error
    rows = get_db().execute(
        "SELECT * FROM signals WHERE course_id = ? AND recipient_id = ? AND sender_id = ? AND id > ? ORDER BY id LIMIT 200",
        (course_id, user["id"], partner_id, request.args.get("since", 0, type=int)),
    ).fetchall()
    return jsonify({"signals": [{"id": r["id"], "type": r["type"], "payload": json.loads(r["payload"])} for r in rows]})


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/message/envoyer", methods=["POST"])
@login_required(role=("prof", "etudiant"))
def visio_chat_envoyer(course_id, slot_id):
    user, slot, partner_id, error = visio_json_guard(course_id, slot_id)
    if error:
        return error
    if is_rate_limited(f"msg:{user['id']}", 30, 60):
        return jsonify({"error": "trop de messages"}), 429
    body = clean((request.get_json(silent=True) or {}).get("body"), LIMITS["message"], multiline=True)
    if not body:
        return jsonify({"error": "message vide"}), 400
    db = get_db()
    cur = db.execute("INSERT INTO messages(sender_id, recipient_id, body) VALUES (?, ?, ?)", (user["id"], partner_id, body))
    db.commit()
    row = db.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"message": {"id": row["id"], "body": row["body"], "created_at": date_fr(row["created_at"]), "sender_id": row["sender_id"]}})


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/message/recevoir")
@login_required(role=("prof", "etudiant"))
def visio_chat_recevoir(course_id, slot_id):
    user, slot, partner_id, error = visio_json_guard(course_id, slot_id)
    if error:
        return error
    db = get_db()
    rows = db.execute(
        "SELECT * FROM messages WHERE ((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)) "
        "AND id > ? ORDER BY id LIMIT 200",
        (user["id"], partner_id, partner_id, user["id"], request.args.get("since", 0, type=int)),
    ).fetchall()
    if rows:
        db.execute(
            "UPDATE messages SET read_at = CURRENT_TIMESTAMP WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
            (partner_id, user["id"]),
        )
        db.commit()
    return jsonify({"messages": [
        {"id": r["id"], "body": r["body"], "created_at": date_fr(r["created_at"]), "sender_id": r["sender_id"]} for r in rows
    ]})


MAX_WHITEBOARD_POINTS = 2000


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/tableau/dessiner", methods=["POST"])
@login_required(role=("prof", "etudiant"))
def visio_tableau_dessiner(course_id, slot_id):
    user, slot, partner_id, error = visio_json_guard(course_id, slot_id)
    if error:
        return error
    if is_rate_limited(f"wb:{user['id']}", 240, 60):
        return jsonify({"error": "trop de requêtes"}), 429
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind == "clear":
        stroke = {}
    elif kind == "stroke":
        points, color, width, mode = data.get("points"), data.get("color"), data.get("width"), data.get("mode")
        valid_points = (
            isinstance(points, list) and 0 < len(points) <= MAX_WHITEBOARD_POINTS
            and all(isinstance(p, list) and len(p) == 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in p)
                    for p in points)
        )
        if not (valid_points and isinstance(color, str) and HEX_COLOR_RE.match(color)
                and isinstance(width, (int, float)) and not isinstance(width, bool) and 1 <= width <= 40
                and mode in ("draw", "erase")):
            return jsonify({"error": "trait invalide"}), 400
        stroke = {"points": [[round(x, 4), round(y, 4)] for x, y in points], "color": color, "width": width, "mode": mode}
    else:
        return jsonify({"error": "type invalide"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO whiteboard_strokes(course_id, slot_id, sender_id, kind, data) VALUES (?, ?, ?, ?, ?)",
        (course_id, slot_id, user["id"], kind, json.dumps(stroke)),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/tableau/annuler", methods=["POST"])
@login_required(role=("prof", "etudiant"))
def visio_tableau_annuler(course_id, slot_id):
    user, slot, partner_id, error = visio_json_guard(course_id, slot_id)
    if error:
        return error
    db = get_db()
    last = db.execute(
        "SELECT id FROM whiteboard_strokes WHERE slot_id = ? AND sender_id = ? AND kind = 'stroke' ORDER BY id DESC LIMIT 1",
        (slot_id, user["id"]),
    ).fetchone()
    if not last:
        return jsonify({"error": "aucun trait à annuler"}), 404
    db.execute("DELETE FROM whiteboard_strokes WHERE id = ?", (last["id"],))
    cur = db.execute(
        "INSERT INTO whiteboard_strokes(course_id, slot_id, sender_id, kind, data) VALUES (?, ?, ?, 'undo', ?)",
        (course_id, slot_id, user["id"], json.dumps({"target_id": last["id"]})),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "target_id": last["id"]})


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/tableau/recevoir")
@login_required(role=("prof", "etudiant"))
def visio_tableau_recevoir(course_id, slot_id):
    user, slot, partner_id, error = visio_json_guard(course_id, slot_id)
    if error:
        return error
    rows = get_db().execute(
        "SELECT * FROM whiteboard_strokes WHERE slot_id = ? AND id > ? ORDER BY id LIMIT 2000",
        (slot_id, request.args.get("since", 0, type=int)),
    ).fetchall()
    return jsonify({"strokes": [
        {"id": r["id"], "kind": r["kind"], "sender_id": r["sender_id"], "data": json.loads(r["data"])} for r in rows
    ]})


# ---------------------------------------------------------------------------
# Paramètres du compte : mot de passe, export, suppression
# ---------------------------------------------------------------------------
@app.route("/parametres", methods=["GET", "POST"])
@login_required()
def parametres():
    user = current_user()
    if request.method == "POST":
        if is_rate_limited(f"pwdchange:{user['id']}", 10, 900):
            flash("Trop de tentatives. Réessayez dans quelques minutes.", "error")
            return render_template("parametres.html", user=user), 429
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        if not check_password_hash(user["password_hash"], current_password[:MAX_PASSWORD_LENGTH + 1]):
            flash("Mot de passe actuel incorrect.", "error")
            return render_template("parametres.html", user=user)
        problem = password_problem(new_password)
        if problem:
            flash(problem, "error")
            return render_template("parametres.html", user=user)
        if new_password != request.form.get("confirm_password", ""):
            flash("Les deux mots de passe ne correspondent pas.", "error")
            return render_template("parametres.html", user=user)
        if new_password == current_password:
            flash("Choisissez un mot de passe différent de l'actuel.", "error")
            return render_template("parametres.html", user=user)
        db = get_db()
        new_hash = generate_password_hash(new_password)
        db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (new_hash, user["id"]))
        audit(db, "changement_mot_de_passe", target=f"user:{user['id']}")
        db.commit()
        session["auth_fp"] = auth_fingerprint(new_hash)  # les autres sessions sont déconnectées
        flash("Mot de passe modifié. Vos autres sessions ont été déconnectées.", "success")
        return redirect(url_for("parametres"))
    return render_template("parametres.html", user=user)


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


@app.route("/parametres/mes-donnees")
@login_required()
def exporter_donnees():
    """Droit d'accès et à la portabilité (RGPD, art. 15 et 20)."""
    db = get_db()
    user = current_user()
    uid = user["id"]
    profile = {k: user[k] for k in user.keys() if k not in ("password_hash",)}
    data = {
        "export_genere_le": utc_now_str() + " UTC",
        "profil": profile,
        "cours_publies": rows_to_dicts(db.execute("SELECT * FROM courses WHERE teacher_id = ?", (uid,)).fetchall()),
        "creneaux_reserves": rows_to_dicts(db.execute("SELECT * FROM slots WHERE reserved_by = ?", (uid,)).fetchall()),
        "commandes": rows_to_dicts(db.execute(
            "SELECT * FROM orders WHERE student_id = ? OR teacher_id = ?", (uid, uid)
        ).fetchall()),
        "messages": rows_to_dicts(db.execute(
            "SELECT id, sender_id, recipient_id, body, created_at, read_at, attachment_original_name FROM messages "
            "WHERE sender_id = ? OR recipient_id = ?", (uid, uid)
        ).fetchall()),
        "notes_donnees": rows_to_dicts(db.execute("SELECT teacher_id, rating, created_at FROM ratings WHERE student_id = ?", (uid,)).fetchall()),
        "signalements": rows_to_dicts(db.execute("SELECT * FROM reports WHERE reporter_id = ?", (uid,)).fetchall()),
    }
    buf = BytesIO(json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
    return send_file(buf, mimetype="application/json", as_attachment=True, download_name="mes-donnees.json")


@app.route("/parametres/supprimer-compte", methods=["POST"])
@login_required(role=("prof", "etudiant"))
def supprimer_compte():
    db = get_db()
    user = current_user()
    if is_rate_limited(f"delete:{user['id']}", 5, 900):
        flash("Trop de tentatives. Réessayez plus tard.", "error")
        return redirect(url_for("parametres"))
    if not check_password_hash(user["password_hash"], request.form.get("password", "")[:MAX_PASSWORD_LENGTH + 1]):
        flash("Mot de passe incorrect : le compte n'a pas été supprimé.", "error")
        return redirect(url_for("parametres"))
    try:
        erase_user(db, user, "demande de l'utilisateur", actor_id=user["id"], force_refunds=False)
        db.commit()
    except PaymentError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("parametres"))
    session.clear()
    flash("Votre compte a été supprimé.", "success")
    return redirect(url_for("presentation"))


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------
@app.errorhandler(CSRFError)
def handle_csrf_error(exc):
    if request.is_json or request.path.startswith("/cours/") and "/visio/" in request.path:
        return jsonify({"error": "jeton de sécurité invalide"}), 400
    flash("La page a expiré. Rechargez-la puis recommencez.", "error")
    return redirect_back(url_for("presentation"))


@app.errorhandler(413)
def handle_too_large(exc):
    flash("Fichier trop volumineux (8 Mo maximum).", "error")
    return redirect_back(url_for("presentation"))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(429)
@app.errorhandler(500)
def handle_http_error(exc):
    code = getattr(exc, "code", 500) or 500
    titles = {400: "Requête invalide", 403: "Accès refusé", 404: "Page introuvable", 405: "Action non autorisée",
              429: "Trop de requêtes", 500: "Erreur du serveur"}
    if request.path.startswith("/cours/") and "/visio/" in request.path:
        return jsonify({"error": titles.get(code, "erreur")}), code
    return render_template("erreur.html", code=code, title=titles.get(code, "Erreur")), code


# ---------------------------------------------------------------------------
init_db()
if not os.environ.get("DISABLE_MAINTENANCE_THREAD"):
    start_maintenance_thread()

if __name__ == "__main__":
    # Le débogueur Werkzeug permet d'exécuter du code depuis le navigateur :
    # il n'est activé qu'explicitement (FLASK_DEBUG=1), jamais en production.
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
