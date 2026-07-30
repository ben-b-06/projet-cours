"""
CoursConnect — plateforme de cours en ligne (version Python / Flask + SQLite)

Lancer :
    pip install -r requirements.txt
    python app.py

Puis ouvrir http://127.0.0.1:5000
"""

import os
import json
import secrets
import threading
import time as time_mod
import calendar as calendar_mod
from datetime import date, datetime, timedelta
from functools import wraps
import sqlite3

import stripe
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/var/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "coursconnect.db")
PROFILE_PHOTOS_DIR = os.path.join(DATA_DIR, "uploads", "profils")
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"  # à remplacer par une vraie valeur secrète en production
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 Mo max pour les photos de profil

# Choix fermés pour la matière et le niveau scolaire (du collège au lycée)
MATIERES = ["Mathématiques", "Physique-Chimie","SI", "Français", "SVT", "SES", "Histoire-Géographie"]
NIVEAUX = ["6e", "5e", "4e", "3e", "2nde", "1re", "Terminale"]
NIVEAUX_ETUDE_PROF = [
    "Baccalauréat", "prepa MP", "prepa PC", "prepa TSI", "prepa PSI", "prepa PT", "prepa MPI", "prepa ECG", "prepa MP","prépa littéraire" ,"Licence (Bac+3)", "Master (Bac+5)",
    "Doctorat", "École d'ingénieur", "École de commerce", "Autre"
]

# Modalité d'un cours : uniquement en ligne, uniquement en présentiel, ou les deux
# (dans ce dernier cas, l'élève choisit la modalité pour chaque créneau qu'il réserve).
MODES = ["en_ligne", "presentiel", "les_deux"]
MODE_LABELS = {"en_ligne": "En ligne", "presentiel": "En présentiel", "les_deux": "En ligne ou en présentiel"}

# Identifiants du compte administrateur unique — aucun autre admin ne peut être créé.
ADMIN_EMAIL = "admin@cours.fr"
ADMIN_PASSWORD = "nezufnze48746è_ç"


# ---------------------------------------------------------------------------
# Paiements — Stripe + portefeuille interne + séquestre (escrow)
# ---------------------------------------------------------------------------
# Clés à définir en variables d'environnement (jamais en dur dans le code) :
#   STRIPE_SECRET_KEY      -> clé secrète Stripe (sk_test_... / sk_live_...)
#   STRIPE_PUBLISHABLE_KEY -> clé publique Stripe (pk_test_... / pk_live_...)
#   STRIPE_WEBHOOK_SECRET  -> secret de signature du webhook (whsec_...)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY

# Montants de recharge proposés à l'élève, en euros.
WALLET_TOPUP_AMOUNTS = [10, 20, 50, 100]

# Délai laissé à l'élève, après la fin du cours, pour confirmer que tout
# s'est bien passé ou pour demander un remboursement. Passé ce délai, le
# paiement séquestré est automatiquement versé au professeur.
PAYMENT_HOLD_DELAY = timedelta(hours=24)

# Intervalle (en secondes) entre deux passages du thread d'arrière-plan qui
# libère automatiquement les paiements arrivés au bout de leur délai.
AUTO_RELEASE_POLL_SECONDS = 60


@app.template_filter("euros")
def euros(cents):
    """Formate un montant en centimes vers une chaîne '12,50 €'."""
    if cents is None:
        cents = 0
    return f"{cents / 100:.2f}".replace(".", ",") + " €"


# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Crée les tables si nécessaire et ajoute des données de démonstration."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'prof', 'etudiant')),
            bio TEXT,
            photo TEXT,
            approved INTEGER NOT NULL DEFAULT 1
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

        -- Chaque créneau se réserve individuellement : un cours peut donc être
        -- suivi par plusieurs élèves différents, chacun sur son propre créneau.
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

        -- Historique des mises en relation élève/prof : rempli dès qu'une
        -- réservation est faite et jamais supprimé, pour permettre la
        -- messagerie même après une éventuelle désinscription.
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
            read_at TEXT
        );

        -- Petits messages techniques (offres/réponses SDP, candidats ICE) échangés
        -- entre les deux participants d'un appel vidéo, relus par polling.
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Paiement séquestré (escrow) pour un créneau réservé et payé par un
        -- élève. Le professeur n'est crédité qu'à la confirmation de l'élève,
        -- au remboursement (annulation), ou automatiquement 24h après la fin
        -- du cours si l'élève n'a rien fait entre-temps.
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id) ON DELETE CASCADE,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held', 'released', 'refunded')),
            release_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );

        -- Mouvements du portefeuille interne d'un utilisateur : recharges
        -- Stripe, mises en séquestre, versements reçus, remboursements.
        CREATE TABLE IF NOT EXISTS wallet_transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount_cents INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('topup', 'hold', 'release', 'refund')),
            description TEXT,
            stripe_session_id TEXT,
            payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Note laissée par un élève sur un professeur avec qui un cours a été
        -- réservé. Un élève ne peut laisser qu'une seule note par prof (elle est
        -- mise à jour s'il note à nouveau). Seule la moyenne est visible par le
        -- prof concerné : le détail des notes individuelles ne lui est jamais montré.
        CREATE TABLE IF NOT EXISTS ratings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            comment TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, teacher_id)
        );

        -- Réglages globaux du site (une seule ligne par clé).
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    # Réglage : qui peut s'inscrire sur le site.
    #   - 'tous'  : professeurs ET élèves peuvent créer un compte (par défaut)
    #   - 'profs' : seuls les professeurs peuvent s'inscrire
    existing_setting = db.execute(
        "SELECT value FROM settings WHERE key = 'registration_mode'"
    ).fetchone()
    if not existing_setting:
        db.execute(
            "INSERT INTO settings(key, value) VALUES ('registration_mode', 'tous')"
        )

    # Migrations : ajoute les colonnes introduites après la création initiale des
    # tables, pour les bases existantes qui ne les ont pas encore.
    user_columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
    if "bio" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN bio TEXT")
    if "photo" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN photo TEXT")
    if "approved" not in user_columns:
        # Les comptes déjà existants (créés avant l'ajout de la validation admin)
        # restent approuvés automatiquement, seuls les nouveaux profs inscrits
        # après cette migration devront être validés par l'administrateur.
        db.execute("ALTER TABLE users ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
    if "education_level" not in user_columns:
        # Pour un professeur : son niveau d'étude (ex. Master, Doctorat...).
        # Pour un élève : sa classe (ex. Terminale, 6e...).
        db.execute("ALTER TABLE users ADD COLUMN education_level TEXT")
    if "wallet_cents" not in user_columns:
        # Solde du portefeuille interne (en centimes d'euro), utilisé aussi
        # bien pour créditer un élève (recharge Stripe) que pour créditer un
        # professeur (versement d'un cours confirmé/libéré automatiquement).
        db.execute("ALTER TABLE users ADD COLUMN wallet_cents INTEGER NOT NULL DEFAULT 0")

    course_columns_price = [row[1] for row in db.execute("PRAGMA table_info(courses)").fetchall()]
    if "price_cents" not in course_columns_price:
        # Prix d'une séance pour ce cours, en centimes d'euro.
        db.execute("ALTER TABLE courses ADD COLUMN price_cents INTEGER NOT NULL DEFAULT 0")

    slot_columns = [row[1] for row in db.execute("PRAGMA table_info(slots)").fetchall()]
    if "reserved_by" not in slot_columns:
        db.execute("ALTER TABLE slots ADD COLUMN reserved_by INTEGER REFERENCES users(id)")
    if "student_notes" not in slot_columns:
        db.execute("ALTER TABLE slots ADD COLUMN student_notes TEXT")
    if "slot_mode" not in slot_columns:
        db.execute("ALTER TABLE slots ADD COLUMN slot_mode TEXT")

    course_columns_full = [row[1] for row in db.execute("PRAGMA table_info(courses)").fetchall()]
    if "mode" not in course_columns_full:
        db.execute("ALTER TABLE courses ADD COLUMN mode TEXT NOT NULL DEFAULT 'en_ligne'")
    if "city" not in course_columns_full:
        db.execute("ALTER TABLE courses ADD COLUMN city TEXT")

    # Anciennes bases : la réservation se faisait au niveau du cours entier.
    # On la reporte sur chacun de ses créneaux, puis on abandonne les colonnes
    # devenues inutiles sur la table courses.
    course_columns = [row[1] for row in db.execute("PRAGMA table_info(courses)").fetchall()]
    if "reserved_by" in course_columns:
        old_courses = db.execute(
            "SELECT id, reserved_by, student_notes FROM courses WHERE reserved_by IS NOT NULL"
        ).fetchall()
        for c in old_courses:
            db.execute(
                "UPDATE slots SET reserved_by = ?, student_notes = ? WHERE course_id = ? AND reserved_by IS NULL",
                (c[1], c[2], c[0]),
            )
        db.execute("ALTER TABLE courses DROP COLUMN reserved_by")
    if "student_notes" in course_columns:
        db.execute("ALTER TABLE courses DROP COLUMN student_notes")

    os.makedirs(PROFILE_PHOTOS_DIR, exist_ok=True)

    # Le compte admin est toujours garanti d'exister, avec des identifiants fixes.
    admin_row = db.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
    if not admin_row:
        db.execute(
            "INSERT INTO users(name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            ("Administrateur", ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD), "admin"),
        )

    already_seeded = db.execute("SELECT COUNT(*) FROM users WHERE role != 'admin'").fetchone()[0] > 0
    if not already_seeded:
        seed(db)

    db.commit()
    db.close()


def seed(db):
    demo_users = [
        (
            "Camille Dubois",
            "camille@coursconnect.fr",
            "prof123",
            "prof",
            "Professeure de mathématiques et de physique-chimie depuis 8 ans, "
            "spécialisée dans la préparation du brevet et les révisions ciblées.",
        ),
        (
            "Karim Haddad",
            "karim@coursconnect.fr",
            "prof123",
            "prof",
            "Enseignant de français et de SES, j'aide les lycéens à structurer "
            "leur méthodologie pour la dissertation et les épreuves finales.",
        ),
        ("Léa Marchand", "lea@coursconnect.fr", "etu123", "etudiant", None),
    ]
    ids = {}
    for name, email, password, role, bio in demo_users:
        cur = db.execute(
            "INSERT INTO users(name, email, password_hash, role, bio) VALUES (?, ?, ?, ?, ?)",
            (name, email, generate_password_hash(password), role, bio),
        )
        ids[email] = cur.lastrowid

    demo_courses = [
        (
            "Préparer le brevet de mathématiques",
            "Mathématiques",
            "3e",
            "Révisions ciblées sur le programme de troisième, avec exercices type brevet.",
            ids["camille@coursconnect.fr"],
            [("2026-09-08", "17:00", 60), ("2026-09-15", "17:00", 60)],
        ),
        (
            "Mécanique et énergie",
            "Physique-Chimie",
            "1re",
            "Les bases de la mécanique et des transferts d'énergie au programme de première.",
            ids["camille@coursconnect.fr"],
            [("2026-09-10", "18:00", 90)],
        ),
        (
            "Dissertation et analyse de texte",
            "Français",
            "2nde",
            "Méthodologie de la dissertation et de l'analyse littéraire pour la seconde.",
            ids["karim@coursconnect.fr"],
            [("2026-09-09", "16:30", 60), ("2026-09-16", "16:30", 60)],
        ),
        (
            "Révisions SES — Terminale",
            "SES",
            "Terminale",
            "Synthèse des grands thèmes de SES en vue des épreuves de terminale.",
            ids["karim@coursconnect.fr"],
            [("2026-09-12", "14:00", 120)],
        ),
    ]
    for title, subject, level, description, teacher_id, slots in demo_courses:
        cur = db.execute(
            "INSERT INTO courses(title, subject, level, description, teacher_id) VALUES (?, ?, ?, ?, ?)",
            (title, subject, level, description, teacher_id),
        )
        course_id = cur.lastrowid
        reserve_first_slot = title.startswith("Préparer le brevet") or title.startswith("Révisions SES")
        for i, (slot_date, slot_time, duration) in enumerate(slots):
            slot_cur = db.execute(
                "INSERT INTO slots(course_id, slot_date, slot_time, duration_minutes) VALUES (?, ?, ?, ?)",
                (course_id, slot_date, slot_time, duration),
            )
            # Seul le tout premier créneau de ces cours est déjà réservé, pour
            # illustrer qu'un même cours peut avoir des créneaux libres et
            # d'autres pris par un élève.
            if reserve_first_slot and i == 0:
                db.execute(
                    "UPDATE slots SET reserved_by = ? WHERE id = ?",
                    (ids["lea@coursconnect.fr"], slot_cur.lastrowid),
                )
                db.execute(
                    "INSERT OR IGNORE INTO contacts(student_id, teacher_id, course_id) VALUES (?, ?, ?)",
                    (ids["lea@coursconnect.fr"], teacher_id, course_id),
                )

    # Un petit échange de démonstration pour illustrer la messagerie.
    db.execute(
        "INSERT INTO messages(sender_id, recipient_id, body) VALUES (?, ?, ?)",
        (ids["lea@coursconnect.fr"], ids["camille@coursconnect.fr"],
         "Bonjour, avant notre premier cours, pourriez-vous me confirmer si nous verrons les fractions ?"),
    )
    db.execute(
        "INSERT INTO messages(sender_id, recipient_id, body) VALUES (?, ?, ?)",
        (ids["camille@coursconnect.fr"], ids["lea@coursconnect.fr"],
         "Bonjour Léa, oui tout à fait, on commencera justement par les fractions."),
    )


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------
def get_slots_map(db, course_ids):
    """Retourne {course_id: [slot, ...]} triés par date/heure pour une liste d'ids de
    cours. Chaque créneau porte son propre statut de réservation (reserved_by,
    student_name, student_notes), puisque la réservation se fait créneau par créneau."""
    if not course_ids:
        return {}
    placeholders = ",".join("?" for _ in course_ids)
    rows = db.execute(
        f"""
        SELECT sl.*, u.name AS student_name
        FROM slots sl
        LEFT JOIN users u ON u.id = sl.reserved_by
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
    """Retourne {teacher_id: {"avg": moyenne, "count": nb_notes}} pour une liste
    d'ids de profs. Ne renvoie jamais le détail des notes individuelles."""
    teacher_ids = [tid for tid in {t for t in teacher_ids if t is not None}]
    if not teacher_ids:
        return {}
    placeholders = ",".join("?" for _ in teacher_ids)
    rows = db.execute(
        f"""
        SELECT teacher_id, AVG(rating) AS avg_rating, COUNT(*) AS nb_ratings
        FROM ratings
        WHERE teacher_id IN ({placeholders})
        GROUP BY teacher_id
        """,
        teacher_ids,
    ).fetchall()
    return {r["teacher_id"]: {"avg": r["avg_rating"], "count": r["nb_ratings"]} for r in rows}


# ---------------------------------------------------------------------------
# Portefeuille & séquestre des paiements
# ---------------------------------------------------------------------------
def credit_wallet(db, user_id, amount_cents, kind, description=None, stripe_session_id=None, payment_id=None):
    """Crédite (amount_cents > 0) ou débite (amount_cents < 0) le portefeuille
    d'un utilisateur et journalise le mouvement. Ne fait pas de commit."""
    db.execute("UPDATE users SET wallet_cents = wallet_cents + ? WHERE id = ?", (amount_cents, user_id))
    db.execute(
        """
        INSERT INTO wallet_transactions(user_id, amount_cents, kind, description, stripe_session_id, payment_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, amount_cents, kind, description, stripe_session_id, payment_id),
    )


def slot_end_datetime(slot):
    """Renvoie la date/heure de fin d'un créneau (datetime), utilisée pour
    calculer le moment où le séquestre du paiement devient libérable."""
    start = datetime.strptime(f"{slot['slot_date']} {slot['slot_time']}", "%Y-%m-%d %H:%M")
    return start + timedelta(minutes=int(slot["duration_minutes"]))


def create_payment_hold(db, slot, course, student_id):
    """Débite le portefeuille de l'élève du prix du cours et met la somme en
    séquestre (statut 'held') jusqu'à confirmation, remboursement, ou
    libération automatique 24h après la fin du cours."""
    amount = course["price_cents"] or 0
    if amount <= 0:
        return None
    release_at = slot_end_datetime(slot) + PAYMENT_HOLD_DELAY
    credit_wallet(
        db, student_id, -amount, "hold",
        description=f"Paiement séquestré pour « {course['title']} »",
    )
    cur = db.execute(
        """
        INSERT INTO payments(slot_id, course_id, student_id, teacher_id, amount_cents, status, release_at)
        VALUES (?, ?, ?, ?, ?, 'held', ?)
        """,
        (slot["id"], course["id"], student_id, course["teacher_id"], amount, release_at.strftime("%Y-%m-%d %H:%M:%S")),
    )
    return cur.lastrowid


def release_payment(db, payment, reason="auto"):
    """Verse le paiement séquestré au professeur (crédite son portefeuille)."""
    credit_wallet(
        db, payment["teacher_id"], payment["amount_cents"], "release",
        description="Cours confirmé — versement" if reason == "student" else "Versement automatique après 24h",
        payment_id=payment["id"],
    )
    db.execute(
        "UPDATE payments SET status = 'released', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (payment["id"],),
    )


def refund_payment(db, payment):
    """Rembourse l'élève : recrédite son portefeuille interne avec la somme
    initialement séquestrée."""
    credit_wallet(
        db, payment["student_id"], payment["amount_cents"], "refund",
        description="Remboursement demandé par l'élève",
        payment_id=payment["id"],
    )
    db.execute(
        "UPDATE payments SET status = 'refunded', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (payment["id"],),
    )


def run_auto_release_once():
    """Parcourt les paiements toujours en séquestre dont le délai de 24h est
    dépassé et les verse automatiquement au professeur."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        due = db.execute(
            "SELECT * FROM payments WHERE status = 'held' AND release_at <= ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
        for payment in due:
            release_payment(db, payment, reason="auto")
        if due:
            db.commit()
    finally:
        db.close()


def start_auto_release_background_thread():
    """Démarre un thread démon qui libère périodiquement les paiements dont
    le délai de 24h est écoulé. Solution simple adaptée à un déploiement
    mono-processus ; pour un déploiement multi-worker (ex. plusieurs workers
    gunicorn), préférer un vrai job planifié (cron, Celery beat...) qui
    appelle run_auto_release_once()."""

    def loop():
        while True:
            try:
                run_auto_release_once()
            except Exception as exc:  # ne jamais laisser le thread mourir silencieusement
                print(f"[auto-release] erreur : {exc}")
            time_mod.sleep(AUTO_RELEASE_POLL_SECONDS)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


@app.template_filter("duree_lisible")
def duree_lisible(minutes):
    minutes = int(minutes)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m:02d}"
    if h:
        return f"{h} h"
    return f"{m} min"


MOIS_FR = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]
JOURS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


def build_month_weeks(year, month, events_by_date):
    """Construit les semaines (listes de 7 jours) du mois demandé, avec les
    évènements du jour attachés à chaque case, pour affichage en grille."""
    cal = calendar_mod.Calendar(firstweekday=0)
    weeks = []
    for week in cal.monthdatescalendar(year, month):
        week_cells = []
        for day in week:
            key = day.isoformat()
            week_cells.append(
                {
                    "date": day,
                    "in_month": day.month == month,
                    "is_today": day == date.today(),
                    "events": events_by_date.get(key, []),
                }
            )
        weeks.append(week_cells)
    return weeks


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


@app.context_processor
def inject_current_user():
    return {"current_user": current_user()}


def get_registration_mode():
    """Renvoie 'tous' (profs + élèves) ou 'profs' (professeurs uniquement)."""
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = 'registration_mode'").fetchone()
    return row["value"] if row else "tous"


def set_registration_mode(mode):
    if mode not in ("tous", "profs"):
        return
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES ('registration_mode', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (mode,),
    )
    db.commit()


def dashboard_url_for(role):
    if role == "admin":
        return url_for("admin_dashboard")
    if role == "prof":
        return url_for("prof_dashboard")
    return url_for("etudiant")


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or (role and user["role"] != role):
                message = (
                    "Connectez-vous avec un compte professeur pour accéder à cette page."
                    if role == "prof"
                    else "Connectez-vous avec un compte étudiant pour accéder à cette page."
                    if role == "etudiant"
                    else "Connectez-vous avec un compte administrateur pour accéder à cette page."
                    if role == "admin"
                    else "Connectez-vous pour accéder à cette page."
                )
                flash(message, "error")
                return redirect(url_for("connexion"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


# ---------------------------------------------------------------------------
# Routes — Accueil
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("presentation"))


@app.route("/presentation")
def presentation():
    return render_template("presentation.html")


def parse_course_filters(args):
    """Lit les paramètres de filtre de recherche de cours depuis la querystring
    (partagé entre la vue liste et la vue calendrier)."""
    return {
        "q": args.get("q", "").strip(),
        "subject": args.get("subject", "").strip(),
        "level": args.get("level", "").strip(),
        "teacher_id": args.get("teacher_id", "").strip(),
        "min_rating": args.get("min_rating", "").strip(),
        "mode": args.get("mode", "").strip(),
        "city": args.get("city", "").strip(),
    }


def build_course_query(filters):
    """Construit la clause WHERE + les paramètres SQL correspondant aux
    filtres de recherche de cours (partagé entre la vue liste et la vue
    calendrier)."""
    # On ne liste que les cours ayant au moins un créneau encore libre : la
    # réservation se fait désormais créneau par créneau, pas cours entier.
    conditions = [
        "EXISTS (SELECT 1 FROM slots s WHERE s.course_id = c.id AND s.reserved_by IS NULL)",
        # Les cours d'un prof pas encore validé par l'administrateur ne sont
        # jamais affichés publiquement.
        "u.approved = 1",
    ]
    params = []

    if filters["q"]:
        conditions.append("(c.title LIKE ? OR c.description LIKE ?)")
        like = f"%{filters['q']}%"
        params.extend([like, like])

    if filters["subject"] in MATIERES:
        conditions.append("c.subject = ?")
        params.append(filters["subject"])

    if filters["level"] in NIVEAUX:
        conditions.append("c.level = ?")
        params.append(filters["level"])

    if filters["mode"] == "en_ligne":
        conditions.append("c.mode IN ('en_ligne', 'les_deux')")
    elif filters["mode"] == "presentiel":
        conditions.append("c.mode IN ('presentiel', 'les_deux')")

    if filters["city"]:
        conditions.append("c.city LIKE ?")
        params.append(f"%{filters['city']}%")

    if filters["teacher_id"].isdigit():
        conditions.append("c.teacher_id = ?")
        params.append(int(filters["teacher_id"]))

    if filters["min_rating"].isdigit() and 1 <= int(filters["min_rating"]) <= 5:
        # Un prof sans aucune note (AVG = NULL) n'est jamais retenu par ce filtre.
        conditions.append(
            "(SELECT AVG(rating) FROM ratings r WHERE r.teacher_id = c.teacher_id) >= ?"
        )
        params.append(int(filters["min_rating"]))

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    return where_clause, params


def fetch_filtered_courses(db, filters):
    where_clause, params = build_course_query(filters)
    rows = db.execute(
        f"""
        SELECT c.*, u.name AS teacher_name, u.bio AS teacher_bio, u.photo AS teacher_photo,
               u.education_level AS teacher_education_level
        FROM courses c
        JOIN users u ON u.id = c.teacher_id
        WHERE {where_clause}
        ORDER BY c.id DESC
        """,
        params,
    ).fetchall()
    return rows


@app.route("/cours")
def cours():
    db = get_db()
    user = current_user()

    filters = parse_course_filters(request.args)
    rows = fetch_filtered_courses(db, filters)

    teachers = db.execute(
        "SELECT id, name FROM users WHERE role = 'prof' AND approved = 1 ORDER BY name"
    ).fetchall()

    slots_map = get_slots_map(db, [r["id"] for r in rows])
    ratings_map = get_teacher_ratings_map(db, [r["teacher_id"] for r in rows])
    return render_template(
        "cours.html",
        courses=rows,
        slots_map=slots_map,
        ratings_map=ratings_map,
        matieres=MATIERES,
        niveaux=NIVEAUX,
        teachers=teachers,
        mode_labels=MODE_LABELS,
        filters=filters,
    )


@app.route("/cours/calendrier")
def cours_calendrier():
    """Vue calendrier de la recherche de cours : affiche, sur une grille
    mensuelle, tous les créneaux encore disponibles correspondant aux mêmes
    filtres que la liste des cours (matière, niveau, modalité, ville,
    professeur, note minimale, recherche libre)."""
    db = get_db()
    filters = parse_course_filters(request.args)
    year, month = resolve_month(request.args)
    (prev_year, prev_month), (next_year, next_month) = month_nav(year, month)

    courses = fetch_filtered_courses(db, filters)
    teachers = db.execute(
        "SELECT id, name FROM users WHERE role = 'prof' AND approved = 1 ORDER BY name"
    ).fetchall()

    slots_map = get_slots_map(db, [c["id"] for c in courses])

    events_by_date = {}
    for c in courses:
        for s in slots_map.get(c["id"], []):
            if s["reserved_by"] is not None:
                continue
            events_by_date.setdefault(s["slot_date"], []).append(
                {
                    "title": c["title"],
                    "subtitle": f"{c['subject']} · {c['level']} · {c['teacher_name']}",
                    "time": s["slot_time"],
                    "duration": s["duration_minutes"],
                    "status": "disponible",
                    "course_id": c["id"],
                }
            )

    # Chaque case du calendrier trie ses créneaux par heure.
    for evs in events_by_date.values():
        evs.sort(key=lambda e: e["time"])

    weeks = build_month_weeks(year, month, events_by_date)

    nav_args = {k: v for k, v in filters.items() if v}
    return render_template(
        "cours_calendrier.html",
        weeks=weeks,
        month_label=f"{MOIS_FR[month]} {year}",
        jours=JOURS_FR,
        prev_link=url_for("cours_calendrier", year=prev_year, month=prev_month, **nav_args),
        next_link=url_for("cours_calendrier", year=next_year, month=next_month, **nav_args),
        today_link=url_for("cours_calendrier", **nav_args),
        matieres=MATIERES,
        niveaux=NIVEAUX,
        teachers=teachers,
        filters=filters,
        courses_count=len(courses),
    )


@app.route("/cours/<int:course_id>/inscription", methods=["POST"])
@login_required(role="etudiant")
def inscription_cours(course_id):
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        flash("Cours introuvable.", "error")
        return redirect(url_for("cours"))

    slot_ids = [int(sid) for sid in request.form.getlist("slot_id[]") if sid.isdigit()]
    notes = request.form.get("notes", "").strip() or None

    if not slot_ids:
        flash("Sélectionnez au moins un créneau à réserver.", "error")
        return redirect(url_for("cours"))

    price = course["price_cents"] or 0
    total_cost = price * len(slot_ids)
    student = current_user()
    if total_cost > (student["wallet_cents"] or 0):
        flash(
            f"Solde insuffisant : cette réservation coûte {euros(total_cost)} mais votre "
            f"portefeuille contient {euros(student['wallet_cents'])}. Rechargez votre compte.",
            "error",
        )
        return redirect(url_for("portefeuille"))

    # Modalité effective du créneau : fixée par le cours, sauf si celui-ci
    # propose les deux, auquel cas l'élève choisit pour chaque créneau réservé.
    reserved = 0
    for slot_id in slot_ids:
        if course["mode"] == "les_deux":
            chosen = request.form.get(f"slot_mode_{slot_id}", "")
            slot_mode = chosen if chosen in ("en_ligne", "presentiel") else "en_ligne"
        else:
            slot_mode = course["mode"]
        cur = db.execute(
            """
            UPDATE slots SET reserved_by = ?, student_notes = ?, slot_mode = ?
            WHERE id = ? AND course_id = ? AND reserved_by IS NULL
            """,
            (student["id"], notes, slot_mode, slot_id, course_id),
        )
        if cur.rowcount:
            reserved += 1
            slot = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            create_payment_hold(db, slot, course, student["id"])

    if reserved:
        # On garde une trace durable de la mise en relation élève/prof pour la
        # messagerie, même si l'élève annule ses créneaux plus tard.
        db.execute(
            "INSERT OR IGNORE INTO contacts(student_id, teacher_id, course_id) VALUES (?, ?, ?)",
            (student["id"], course["teacher_id"], course_id),
        )
        db.commit()
        if reserved == len(slot_ids):
            flash(
                f"{reserved} créneau(x) réservé(s) et payé(s) ({euros(price * reserved)}) ! "
                "L'argent est conservé en séquestre et sera versé au professeur 24h après le "
                "cours, sauf si vous confirmez avant ou demandez un remboursement.",
                "success",
            )
        else:
            flash(
                f"{reserved} créneau(x) réservé(s) — certains créneaux choisis venaient "
                "d'être pris par un autre élève.",
                "error",
            )
    else:
        db.rollback()
        flash("Les créneaux choisis venaient d'être réservés par un autre élève.", "error")
    return redirect(url_for("cours"))


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/desinscription", methods=["POST"])
@login_required(role="etudiant")
def desinscription_cours(course_id, slot_id):
    db = get_db()
    cur = db.execute(
        """
        UPDATE slots SET reserved_by = NULL, student_notes = NULL, slot_mode = NULL
        WHERE id = ? AND course_id = ? AND reserved_by = ?
        """,
        (slot_id, course_id, current_user()["id"]),
    )
    if cur.rowcount:
        # Si un paiement était en séquestre pour ce créneau, on rembourse
        # intégralement l'élève sur son portefeuille interne.
        payment = db.execute(
            "SELECT * FROM payments WHERE slot_id = ? AND status = 'held'", (slot_id,)
        ).fetchone()
        if payment:
            refund_payment(db, payment)
    db.commit()
    flash("Vous avez annulé ce créneau (le paiement éventuel a été remboursé sur votre portefeuille).", "success")
    return redirect(request.referrer or url_for("etudiant"))


# ---------------------------------------------------------------------------
# Routes — Portefeuille & paiements (Stripe)
# ---------------------------------------------------------------------------
@app.route("/portefeuille")
@login_required(role="etudiant")
def portefeuille():
    db = get_db()
    user = current_user()
    transactions = db.execute(
        "SELECT * FROM wallet_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 50",
        (user["id"],),
    ).fetchall()
    return render_template(
        "portefeuille.html",
        transactions=transactions,
        topup_amounts=WALLET_TOPUP_AMOUNTS,
        stripe_configured=bool(STRIPE_SECRET_KEY),
    )


@app.route("/portefeuille/recharger", methods=["POST"])
@login_required(role="etudiant")
def portefeuille_recharger():
    if not STRIPE_SECRET_KEY:
        flash(
            "Le paiement par carte n'est pas encore configuré sur ce serveur "
            "(variable d'environnement STRIPE_SECRET_KEY manquante).",
            "error",
        )
        return redirect(url_for("portefeuille"))

    amount_raw = request.form.get("amount", "").strip().replace(",", ".")
    try:
        amount_euros = float(amount_raw)
        if amount_euros < 1:
            raise ValueError
    except (TypeError, ValueError):
        flash("Montant de recharge invalide.", "error")
        return redirect(url_for("portefeuille"))

    amount_cents = round(amount_euros * 100)
    user = current_user()

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "eur",
                        "product_data": {"name": "Recharge du portefeuille CoursConnect"},
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            customer_email=user["email"],
            client_reference_id=str(user["id"]),
            metadata={"user_id": str(user["id"]), "amount_cents": str(amount_cents)},
            success_url=url_for("portefeuille_succes", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("portefeuille_annule", _external=True),
        )
    except Exception as exc:
        flash(f"Impossible de démarrer le paiement Stripe : {exc}", "error")
        return redirect(url_for("portefeuille"))

    return redirect(checkout_session.url, code=303)


def credit_wallet_from_checkout_session(checkout_session):
    """Crédite le portefeuille d'un élève à partir d'une Session Stripe Checkout
    payée, en s'assurant de ne jamais la créditer deux fois (idempotence)."""
    if checkout_session.get("payment_status") != "paid":
        return False
    db = get_db()
    already = db.execute(
        "SELECT id FROM wallet_transactions WHERE stripe_session_id = ?",
        (checkout_session["id"],),
    ).fetchone()
    if already:
        return False
    user_id = int(checkout_session["metadata"]["user_id"])
    amount_cents = int(checkout_session["metadata"]["amount_cents"])
    credit_wallet(
        db, user_id, amount_cents, "topup",
        description="Recharge par carte bancaire (Stripe)",
        stripe_session_id=checkout_session["id"],
    )
    db.commit()
    return True


@app.route("/portefeuille/succes")
@login_required(role="etudiant")
def portefeuille_succes():
    session_id = request.args.get("session_id")
    if session_id and STRIPE_SECRET_KEY:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            if credit_wallet_from_checkout_session(checkout_session):
                flash("Votre portefeuille a été rechargé avec succès !", "success")
            else:
                flash("Ce paiement a déjà été pris en compte.", "success")
        except Exception as exc:
            flash(f"Paiement reçu mais impossible de vérifier la session Stripe : {exc}", "error")
    return redirect(url_for("portefeuille"))


@app.route("/portefeuille/annule")
@login_required(role="etudiant")
def portefeuille_annule():
    flash("Recharge annulée : aucun montant n'a été débité.", "error")
    return redirect(url_for("portefeuille"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Point d'entrée webhook Stripe : filet de sécurité qui crédite le
    portefeuille même si l'élève ferme son navigateur avant la redirection
    vers /portefeuille/succes. À déclarer dans le Dashboard Stripe avec
    l'événement 'checkout.session.completed'."""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        return jsonify({"error": str(exc)}), 400

    if event["type"] == "checkout.session.completed":
        checkout_session = event["data"]["object"]
        with app.app_context():
            credit_wallet_from_checkout_session(checkout_session)

    return jsonify({"received": True})


@app.route("/etudiant/paiement/<int:payment_id>/confirmer", methods=["POST"])
@login_required(role="etudiant")
def confirmer_paiement(payment_id):
    db = get_db()
    payment = db.execute(
        "SELECT * FROM payments WHERE id = ? AND student_id = ? AND status = 'held'",
        (payment_id, current_user()["id"]),
    ).fetchone()
    if not payment:
        flash("Paiement introuvable ou déjà réglé.", "error")
        return redirect(url_for("etudiant"))
    release_payment(db, payment, reason="student")
    db.commit()
    flash("Merci ! Le professeur a été payé pour ce cours.", "success")
    return redirect(request.referrer or url_for("etudiant"))


@app.route("/etudiant/paiement/<int:payment_id>/rembourser", methods=["POST"])
@login_required(role="etudiant")
def rembourser_paiement(payment_id):
    db = get_db()
    payment = db.execute(
        "SELECT * FROM payments WHERE id = ? AND student_id = ? AND status = 'held'",
        (payment_id, current_user()["id"]),
    ).fetchone()
    if not payment:
        flash("Paiement introuvable ou déjà réglé.", "error")
        return redirect(url_for("etudiant"))
    refund_payment(db, payment)
    db.commit()
    flash("Remboursement effectué sur votre portefeuille.", "success")
    return redirect(request.referrer or url_for("etudiant"))


# ---------------------------------------------------------------------------
# Routes — Authentification
# ---------------------------------------------------------------------------
@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            flash(f"Bienvenue, {user['name']} !", "success")
            return redirect(dashboard_url_for(user["role"]))
        flash("E-mail ou mot de passe incorrect.", "error")
    return render_template("connexion.html")


DEMO_EMAILS = {
    "prof": "camille@coursconnect.fr",
    "etudiant": "lea@coursconnect.fr",
}


@app.route("/connexion/demo/<role>")
def connexion_demo(role):
    email = DEMO_EMAILS.get(role)
    user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone() if email else None
    if user:
        session["user_id"] = user["id"]
        flash(f"Connecté(e) en tant que {user['name']}.", "success")
        return redirect(dashboard_url_for(user["role"]))
    return redirect(url_for("connexion"))


@app.route("/uploads/profils/<filename>")
def uploaded_profile(filename):
    return send_from_directory(PROFILE_PHOTOS_DIR, filename)


@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    registration_mode = get_registration_mode()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role")

        if role not in ("prof", "etudiant"):
            flash("Merci de choisir un rôle valide.", "error")
            return render_template("inscription.html", registration_mode=registration_mode)

        if registration_mode == "profs" and role == "etudiant":
            flash("Les inscriptions sont actuellement réservées aux professeurs.", "error")
            return render_template("inscription.html", registration_mode=registration_mode)

        if not name or not email or len(password) < 4:
            flash("Merci de renseigner un nom, un e-mail et un mot de passe d'au moins 4 caractères.", "error")
            return render_template("inscription.html", registration_mode=registration_mode)

        db = get_db()
        if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            flash("Un compte existe déjà avec cet e-mail.", "error")
            return render_template("inscription.html", registration_mode=registration_mode)

        # Un professeur qui s'inscrit doit être validé par l'administrateur
        # avant que son profil et ses cours n'apparaissent publiquement.
        approved = 0 if role == "prof" else 1

        cur = db.execute(
            "INSERT INTO users(name, email, password_hash, role, approved) VALUES (?, ?, ?, ?, ?)",
            (name, email, generate_password_hash(password), role, approved),
        )
        db.commit()
        session["user_id"] = cur.lastrowid
        if role == "prof":
            flash(
                f"Compte créé, bienvenue {name} ! Votre profil est en attente de validation par "
                "l'administrateur : il ne sera visible publiquement qu'une fois validé.",
                "success",
            )
        else:
            flash(f"Compte créé. Bienvenue, {name} !", "success")
        return redirect(dashboard_url_for(role))
    return render_template("inscription.html", registration_mode=registration_mode)


@app.route("/deconnexion")
def deconnexion():
    session.clear()
    flash("Vous êtes déconnecté(e).", "success")
    return redirect(url_for("presentation"))


# ---------------------------------------------------------------------------
# Routes — Professeur
# ---------------------------------------------------------------------------
@app.route("/prof/tableau-de-bord")
@login_required(role="prof")
def prof_dashboard():
    db = get_db()
    user = current_user()
    rows = db.execute(
        "SELECT * FROM courses WHERE teacher_id = ? ORDER BY id DESC",
        (user["id"],),
    ).fetchall()
    slots_map = get_slots_map(db, [r["id"] for r in rows])
    reserved_count = sum(1 for slots in slots_map.values() for s in slots if s["reserved_by"])
    # Le prof ne voit que sa moyenne et le nombre de notes reçues, jamais le
    # détail des notes individuelles laissées par ses élèves.
    my_rating = get_teacher_ratings_map(db, [user["id"]]).get(user["id"])
    pending_cents = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE teacher_id = ? AND status = 'held'",
        (user["id"],),
    ).fetchone()[0]
    return render_template(
        "prof_dashboard.html",
        courses=rows,
        reserved_count=reserved_count,
        slots_map=slots_map,
        my_rating=my_rating,
        pending_cents=pending_cents,
    )


def allowed_photo(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_PHOTO_EXTENSIONS


@app.route("/prof/profil", methods=["GET", "POST"])
@login_required(role="prof")
def prof_profil():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        bio = request.form.get("bio", "").strip()
        education_level = request.form.get("education_level", "").strip()
        photo_file = request.files.get("photo")
        photo_filename = user["photo"]

        if photo_file and photo_file.filename:
            if not allowed_photo(photo_file.filename):
                flash("Format d'image non pris en charge (utilisez JPG, PNG ou WEBP).", "error")
                return render_template("prof_profil.html", user=user, niveaux_etude=NIVEAUX_ETUDE_PROF)
            ext = photo_file.filename.rsplit(".", 1)[1].lower()
            new_filename = f"prof-{user['id']}.{ext}"
            os.makedirs(PROFILE_PHOTOS_DIR, exist_ok=True)
            photo_file.save(os.path.join(PROFILE_PHOTOS_DIR, new_filename))
            photo_filename = new_filename

        db.execute(
            "UPDATE users SET bio = ?, photo = ?, education_level = ? WHERE id = ?",
            (bio or None, photo_filename, education_level or None, user["id"]),
        )
        db.commit()
        flash("Profil mis à jour.", "success")
        return redirect(url_for("prof_profil"))

    return render_template("prof_profil.html", user=user, niveaux_etude=NIVEAUX_ETUDE_PROF)


@app.route("/prof/creer", methods=["GET", "POST"])
@login_required(role="prof")
def prof_creer():
    if not current_user()["approved"]:
        flash(
            "Votre compte est en attente de validation par l'administrateur : vous pourrez "
            "publier des cours une fois votre profil validé.",
            "error",
        )
        return redirect(url_for("prof_dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        subject = request.form.get("subject", "")
        level = request.form.get("level", "")
        description = request.form.get("description", "").strip()
        mode = request.form.get("mode", "")
        city = request.form.get("city", "").strip()
        price_raw = request.form.get("price", "").strip().replace(",", ".")

        slot_dates = request.form.getlist("slot_date[]")
        slot_times = request.form.getlist("slot_time[]")
        slot_durations = request.form.getlist("slot_duration[]")

        errors = []
        if not title:
            errors.append("Merci de renseigner un titre.")
        if subject not in MATIERES:
            errors.append("Merci de choisir une matière dans la liste proposée.")
        if level not in NIVEAUX:
            errors.append("Merci de choisir un niveau scolaire dans la liste proposée.")
        if not description:
            errors.append("Merci de renseigner une description.")
        if mode not in MODES:
            errors.append("Merci de choisir si le cours a lieu en ligne, en présentiel, ou les deux.")
        if mode in ("presentiel", "les_deux") and not city:
            errors.append("Merci de renseigner la ville où le cours a lieu en présentiel.")
        if mode == "en_ligne":
            city = ""

        price_cents = None
        try:
            price_value = float(price_raw)
            if price_value < 0:
                raise ValueError
            price_cents = round(price_value * 100)
        except (TypeError, ValueError):
            errors.append("Merci d'indiquer un prix par séance valide (ex. 25 ou 25,50).")

        slots = []
        for date_val, time_val, duration_val in zip(slot_dates, slot_times, slot_durations):
            date_val = date_val.strip()
            time_val = time_val.strip()
            if not date_val and not time_val and not duration_val.strip():
                continue  # ligne de créneau laissée vide, on l'ignore
            if not date_val or not time_val:
                errors.append("Chaque créneau doit avoir une date et une heure.")
                continue
            try:
                duration = int(duration_val)
                if duration <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("La durée de chaque créneau doit être un nombre de minutes positif.")
                continue
            slots.append((date_val, time_val, duration))

        if not slots:
            errors.append("Ajoutez au moins un créneau (date, heure et durée) pour ce cours.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "prof_creer.html",
                matieres=MATIERES,
                niveaux=NIVEAUX,
                modes=MODES,
                mode_labels=MODE_LABELS,
                form=request.form,
            )

        db = get_db()
        cur = db.execute(
            "INSERT INTO courses(title, subject, level, description, teacher_id, mode, city, price_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, subject, level, description, current_user()["id"], mode, city or None, price_cents),
        )
        course_id = cur.lastrowid
        for date_val, time_val, duration in slots:
            db.execute(
                "INSERT INTO slots(course_id, slot_date, slot_time, duration_minutes) VALUES (?, ?, ?, ?)",
                (course_id, date_val, time_val, duration),
            )
        db.commit()
        flash(f"Cours publié : {title}", "success")
        return redirect(url_for("prof_dashboard"))

    return render_template(
        "prof_creer.html", matieres=MATIERES, niveaux=NIVEAUX, modes=MODES, mode_labels=MODE_LABELS, form={}
    )


@app.route("/prof/cours/<int:course_id>/supprimer", methods=["POST"])
@login_required(role="prof")
def prof_supprimer(course_id):
    db = get_db()
    db.execute(
        "DELETE FROM courses WHERE id = ? AND teacher_id = ?",
        (course_id, current_user()["id"]),
    )
    db.commit()
    flash("Cours supprimé.", "success")
    return redirect(url_for("prof_dashboard"))


# ---------------------------------------------------------------------------
# Routes — Administration
# ---------------------------------------------------------------------------
@app.route("/admin/tableau-de-bord")
@login_required(role="admin")
def admin_dashboard():
    db = get_db()
    profs = db.execute(
        """
        SELECT u.*, (SELECT COUNT(*) FROM courses c WHERE c.teacher_id = u.id) AS course_count
        FROM users u WHERE u.role = 'prof' AND u.approved = 1 ORDER BY u.name
        """
    ).fetchall()
    profs_en_attente = db.execute(
        """
        SELECT u.*, (SELECT COUNT(*) FROM courses c WHERE c.teacher_id = u.id) AS course_count
        FROM users u WHERE u.role = 'prof' AND u.approved = 0 ORDER BY u.name
        """
    ).fetchall()
    etudiants = db.execute(
        """
        SELECT u.*, (SELECT COUNT(*) FROM slots sl WHERE sl.reserved_by = u.id) AS course_count
        FROM users u WHERE u.role = 'etudiant' ORDER BY u.name
        """
    ).fetchall()
    courses_rows = db.execute(
        """
        SELECT c.*, u.name AS teacher_name
        FROM courses c
        JOIN users u ON u.id = c.teacher_id
        ORDER BY c.id DESC
        """
    ).fetchall()
    return render_template(
        "admin_dashboard.html",
        profs=profs,
        profs_en_attente=profs_en_attente,
        etudiants=etudiants,
        courses=courses_rows,
        slots_map=get_slots_map(db, [c["id"] for c in courses_rows]),
        registration_mode=get_registration_mode(),
    )


@app.route("/admin/parametres/inscriptions", methods=["POST"])
@login_required(role="admin")
def admin_definir_mode_inscription():
    mode = request.form.get("registration_mode")
    if mode not in ("tous", "profs"):
        flash("Mode d'inscription invalide.", "error")
        return redirect(url_for("admin_dashboard"))
    set_registration_mode(mode)
    if mode == "profs":
        flash("Les inscriptions sont désormais réservées aux professeurs.", "success")
    else:
        flash("Les inscriptions sont désormais ouvertes à tous (professeurs et élèves).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/utilisateurs/<int:user_id>/valider", methods=["POST"])
@login_required(role="admin")
def admin_valider_prof(user_id):
    """Valide l'inscription d'un professeur : son profil et ses cours
    deviennent alors visibles publiquement."""
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target or target["role"] != "prof":
        flash("Professeur introuvable.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute("UPDATE users SET approved = 1 WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Le profil de {target['name']} est validé et désormais visible publiquement.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/utilisateurs/<int:user_id>/supprimer", methods=["POST"])
@login_required(role="admin")
def admin_supprimer_utilisateur(user_id):
    if user_id == current_user()["id"]:
        flash("Vous ne pouvez pas supprimer votre propre compte administrateur.", "error")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("Utilisateur introuvable.", "error")
        return redirect(url_for("admin_dashboard"))
    if target["role"] == "admin":
        flash("Impossible de supprimer un autre compte administrateur.", "error")
        return redirect(url_for("admin_dashboard"))

    # Grâce à ON DELETE CASCADE, ses cours (si prof) et ses inscriptions
    # (si étudiant) sont automatiquement supprimés avec lui.
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Le compte de {target['name']} a été supprimé.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/cours/<int:course_id>/supprimer", methods=["POST"])
@login_required(role="admin")
def admin_supprimer_cours(course_id):
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        flash("Cours introuvable.", "error")
        return redirect(url_for("admin_dashboard"))
    db.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    db.commit()
    flash(f"Le cours « {course['title']} » a été supprimé.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/messagerie")
@login_required(role="admin")
def admin_messagerie():
    db = get_db()
    conversations = db.execute(
        """
        SELECT ct.student_id, ct.teacher_id,
               s.name AS student_name, t.name AS teacher_name,
               (
                   SELECT COUNT(*) FROM messages m
                   WHERE (m.sender_id = ct.student_id AND m.recipient_id = ct.teacher_id)
                      OR (m.sender_id = ct.teacher_id AND m.recipient_id = ct.student_id)
               ) AS message_count,
               (
                   SELECT MAX(m.created_at) FROM messages m
                   WHERE (m.sender_id = ct.student_id AND m.recipient_id = ct.teacher_id)
                      OR (m.sender_id = ct.teacher_id AND m.recipient_id = ct.student_id)
               ) AS last_at,
               (
                   SELECT m.body FROM messages m
                   WHERE (m.sender_id = ct.student_id AND m.recipient_id = ct.teacher_id)
                      OR (m.sender_id = ct.teacher_id AND m.recipient_id = ct.student_id)
                   ORDER BY m.id DESC LIMIT 1
               ) AS last_body
        FROM contacts ct
        JOIN users s ON s.id = ct.student_id
        JOIN users t ON t.id = ct.teacher_id
        GROUP BY ct.student_id, ct.teacher_id
        ORDER BY last_at IS NULL, last_at DESC
        """
    ).fetchall()
    return render_template("admin_messagerie.html", conversations=conversations)


@app.route("/admin/messagerie/<int:student_id>/<int:teacher_id>")
@login_required(role="admin")
def admin_messagerie_thread(student_id, teacher_id):
    db = get_db()
    contact = db.execute(
        "SELECT * FROM contacts WHERE student_id = ? AND teacher_id = ?",
        (student_id, teacher_id),
    ).fetchone()
    if not contact:
        flash("Cette conversation n'existe pas.", "error")
        return redirect(url_for("admin_messagerie"))

    student = db.execute("SELECT * FROM users WHERE id = ?", (student_id,)).fetchone()
    teacher = db.execute("SELECT * FROM users WHERE id = ?", (teacher_id,)).fetchone()
    thread = db.execute(
        """
        SELECT * FROM messages
        WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
        ORDER BY id ASC
        """,
        (student_id, teacher_id, teacher_id, student_id),
    ).fetchall()
    return render_template(
        "admin_messagerie_thread.html", student=student, teacher=teacher, thread=thread
    )


# ---------------------------------------------------------------------------
# Routes — Étudiant
# ---------------------------------------------------------------------------
@app.route("/etudiant/profil", methods=["GET", "POST"])
@login_required(role="etudiant")
def etudiant_profil():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        bio = request.form.get("bio", "").strip()
        classe = request.form.get("education_level", "").strip()
        photo_file = request.files.get("photo")
        photo_filename = user["photo"]

        if photo_file and photo_file.filename:
            if not allowed_photo(photo_file.filename):
                flash("Format d'image non pris en charge (utilisez JPG, PNG ou WEBP).", "error")
                return render_template("etudiant_profil.html", user=user, niveaux=NIVEAUX)
            ext = photo_file.filename.rsplit(".", 1)[1].lower()
            new_filename = f"etudiant-{user['id']}.{ext}"
            os.makedirs(PROFILE_PHOTOS_DIR, exist_ok=True)
            photo_file.save(os.path.join(PROFILE_PHOTOS_DIR, new_filename))
            photo_filename = new_filename

        db.execute(
            "UPDATE users SET bio = ?, photo = ?, education_level = ? WHERE id = ?",
            (bio or None, photo_filename, classe or None, user["id"]),
        )
        db.commit()
        flash("Profil mis à jour.", "success")
        return redirect(url_for("etudiant_profil"))

    return render_template("etudiant_profil.html", user=user, niveaux=NIVEAUX)


@app.route("/etudiant/mes-cours")
@login_required(role="etudiant")
def etudiant():
    db = get_db()
    reservations = db.execute(
        """
        SELECT sl.*, c.id AS course_id, c.title, c.subject, c.level, c.description,
               c.teacher_id, c.city AS course_city, c.price_cents, u.name AS teacher_name,
               p.id AS payment_id, p.status AS payment_status, p.amount_cents AS payment_amount_cents,
               p.release_at AS payment_release_at
        FROM slots sl
        JOIN courses c ON c.id = sl.course_id
        JOIN users u ON u.id = c.teacher_id
        LEFT JOIN payments p ON p.slot_id = sl.id
        WHERE sl.reserved_by = ?
        ORDER BY sl.slot_date, sl.slot_time
        """,
        (current_user()["id"],),
    ).fetchall()
    my_ratings = {
        r["teacher_id"]: r["rating"]
        for r in db.execute(
            "SELECT teacher_id, rating FROM ratings WHERE student_id = ?",
            (current_user()["id"],),
        ).fetchall()
    }
    now = datetime.now()
    course_over_map = {}
    for r in reservations:
        try:
            course_over_map[r["id"]] = slot_end_datetime(r) <= now
        except (TypeError, ValueError):
            course_over_map[r["id"]] = False
    return render_template(
        "etudiant.html",
        reservations=reservations,
        my_ratings=my_ratings,
        course_over_map=course_over_map,
    )


@app.route("/profs/<int:teacher_id>/noter", methods=["POST"])
@login_required(role="etudiant")
def noter_prof(teacher_id):
    user = current_user()
    db = get_db()
    teacher = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'prof'", (teacher_id,)
    ).fetchone()
    if not teacher:
        flash("Professeur introuvable.", "error")
        return redirect(url_for("etudiant"))

    # On ne peut noter qu'un prof avec qui un cours a déjà été réservé.
    if not is_allowed_contact(user, teacher_id):
        flash("Vous ne pouvez noter qu'un professeur avec qui vous avez réservé un cours.", "error")
        return redirect(url_for("etudiant"))

    try:
        rating = int(request.form.get("rating", ""))
    except (TypeError, ValueError):
        rating = 0
    if rating < 1 or rating > 5:
        flash("Merci de choisir une note entre 1 et 5 étoiles.", "error")
        return redirect(url_for("etudiant"))

    db.execute(
        """
        INSERT INTO ratings(student_id, teacher_id, rating)
        VALUES (?, ?, ?)
        ON CONFLICT(student_id, teacher_id)
        DO UPDATE SET rating = excluded.rating, created_at = CURRENT_TIMESTAMP
        """,
        (user["id"], teacher_id, rating),
    )
    db.commit()
    flash(f"Vous avez noté {teacher['name']} {rating}/5. Merci !", "success")
    return redirect(url_for("etudiant"))


# ---------------------------------------------------------------------------
# Routes — Calendrier
# ---------------------------------------------------------------------------
def resolve_month(args):
    today = date.today()
    try:
        year = int(args.get("year", today.year))
        month = int(args.get("month", today.month))
        if month < 1 or month > 12:
            raise ValueError
    except (TypeError, ValueError):
        year, month = today.year, today.month
    return year, month


def month_nav(year, month):
    prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)
    return (prev_year, prev_month), (next_year, next_month)


@app.route("/etudiant/calendrier")
@login_required(role="etudiant")
def etudiant_calendrier():
    db = get_db()
    year, month = resolve_month(request.args)
    (prev_year, prev_month), (next_year, next_month) = month_nav(year, month)

    rows = db.execute(
        """
        SELECT sl.*, c.title, c.subject, c.level, u.name AS teacher_name
        FROM slots sl
        JOIN courses c ON c.id = sl.course_id
        JOIN users u ON u.id = c.teacher_id
        WHERE sl.reserved_by = ?
        """,
        (current_user()["id"],),
    ).fetchall()

    events_by_date = {}
    for s in rows:
        events_by_date.setdefault(s["slot_date"], []).append(
            {
                "title": s["title"],
                "subtitle": f"{s['subject']} · {s['level']} · {s['teacher_name']}",
                "time": s["slot_time"],
                "duration": s["duration_minutes"],
                "status": "reserve",
            }
        )

    weeks = build_month_weeks(year, month, events_by_date)
    return render_template(
        "calendrier.html",
        weeks=weeks,
        month_label=f"{MOIS_FR[month]} {year}",
        jours=JOURS_FR,
        prev_link=url_for("etudiant_calendrier", year=prev_year, month=prev_month),
        next_link=url_for("etudiant_calendrier", year=next_year, month=next_month),
        today_link=url_for("etudiant_calendrier"),
        role_mode="etudiant",
    )


@app.route("/prof/calendrier")
@login_required(role="prof")
def prof_calendrier():
    db = get_db()
    year, month = resolve_month(request.args)
    (prev_year, prev_month), (next_year, next_month) = month_nav(year, month)

    rows = db.execute(
        """
        SELECT sl.*, c.title, c.subject, c.level, s.name AS student_name
        FROM slots sl
        JOIN courses c ON c.id = sl.course_id
        LEFT JOIN users s ON s.id = sl.reserved_by
        WHERE c.teacher_id = ?
        """,
        (current_user()["id"],),
    ).fetchall()

    events_by_date = {}
    for s in rows:
        is_reserved = s["reserved_by"] is not None
        events_by_date.setdefault(s["slot_date"], []).append(
            {
                "title": s["title"],
                "subtitle": f"{s['subject']} · {s['level']}"
                + (f" · {s['student_name']}" if is_reserved else " · Disponible"),
                "time": s["slot_time"],
                "duration": s["duration_minutes"],
                "status": "reserve" if is_reserved else "disponible",
            }
        )

    weeks = build_month_weeks(year, month, events_by_date)
    return render_template(
        "calendrier.html",
        weeks=weeks,
        month_label=f"{MOIS_FR[month]} {year}",
        jours=JOURS_FR,
        prev_link=url_for("prof_calendrier", year=prev_year, month=prev_month),
        next_link=url_for("prof_calendrier", year=next_year, month=next_month),
        today_link=url_for("prof_calendrier"),
        role_mode="prof",
    )


# ---------------------------------------------------------------------------
# Routes — Messagerie
# ---------------------------------------------------------------------------
def get_admin_id():
    """Renvoie l'id du compte administrateur unique."""
    row = get_db().execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    return row["id"] if row else None


def get_contact_preview(user_id, other_id):
    """Dernier message échangé et nombre de messages non lus entre deux comptes."""
    db = get_db()
    last = db.execute(
        """
        SELECT body, created_at, sender_id FROM messages
        WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, other_id, other_id, user_id),
    ).fetchone()
    unread = db.execute(
        "SELECT COUNT(*) FROM messages WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
        (other_id, user_id),
    ).fetchone()[0]
    return last, unread


def get_contacts(user):
    """Retourne la liste des correspondants autorisés : profs pour un élève,
    élèves pour un prof, tous les profs/élèves pour l'administrateur — avec
    dans tous les cas le nombre de messages non lus et un aperçu du dernier
    message échangé. L'administrateur est toujours proposé comme
    correspondant aux profs et aux élèves, même sans cours réservé ensemble."""
    db = get_db()
    if user["role"] == "admin":
        rows = db.execute(
            "SELECT id, name, role FROM users WHERE role IN ('prof', 'etudiant') ORDER BY role, name"
        ).fetchall()
        contacts = []
        for r in rows:
            last, unread = get_contact_preview(user["id"], r["id"])
            contacts.append({"id": r["id"], "name": r["name"], "role": r["role"], "last": last, "unread": unread})
        contacts.sort(key=lambda c: (c["last"]["created_at"] if c["last"] else ""), reverse=True)
        return contacts

    if user["role"] == "etudiant":
        rows = db.execute(
            """
            SELECT u.id, u.name, u.role
            FROM contacts ct
            JOIN users u ON u.id = ct.teacher_id
            WHERE ct.student_id = ?
            GROUP BY u.id
            ORDER BY u.name
            """,
            (user["id"],),
        ).fetchall()
    elif user["role"] == "prof":
        rows = db.execute(
            """
            SELECT u.id, u.name, u.role
            FROM contacts ct
            JOIN users u ON u.id = ct.student_id
            WHERE ct.teacher_id = ?
            GROUP BY u.id
            ORDER BY u.name
            """,
            (user["id"],),
        ).fetchall()
    else:
        return []

    contacts = []
    for r in rows:
        last, unread = get_contact_preview(user["id"], r["id"])
        contacts.append({"id": r["id"], "name": r["name"], "role": r["role"], "last": last, "unread": unread})

    # L'administrateur est toujours joignable par un prof ou un élève, même
    # sans mise en relation préalable via un cours.
    admin_id = get_admin_id()
    if admin_id:
        admin_row = db.execute("SELECT name FROM users WHERE id = ?", (admin_id,)).fetchone()
        last, unread = get_contact_preview(user["id"], admin_id)
        contacts.append({"id": admin_id, "name": admin_row["name"], "role": "admin", "last": last, "unread": unread})

    contacts.sort(key=lambda c: (c["last"]["created_at"] if c["last"] else ""), reverse=True)
    return contacts


def is_allowed_contact(user, other_id):
    db = get_db()
    if user["role"] == "admin":
        row = db.execute(
            "SELECT 1 FROM users WHERE id = ? AND role IN ('prof', 'etudiant')", (other_id,)
        ).fetchone()
        return row is not None
    if other_id == get_admin_id():
        return True
    if user["role"] == "etudiant":
        row = db.execute(
            "SELECT 1 FROM contacts WHERE student_id = ? AND teacher_id = ?",
            (user["id"], other_id),
        ).fetchone()
    elif user["role"] == "prof":
        row = db.execute(
            "SELECT 1 FROM contacts WHERE teacher_id = ? AND student_id = ?",
            (user["id"], other_id),
        ).fetchone()
    else:
        row = None
    return row is not None


@app.context_processor
def inject_unread_total():
    user = current_user()
    if user and user["role"] in ("prof", "etudiant", "admin"):
        total = get_db().execute(
            "SELECT COUNT(*) FROM messages WHERE recipient_id = ? AND read_at IS NULL",
            (user["id"],),
        ).fetchone()[0]
        return {"unread_messages_total": total}
    return {"unread_messages_total": 0}


@app.route("/messagerie")
@login_required()
def messagerie():
    user = current_user()
    contacts = get_contacts(user)
    return render_template("messagerie.html", contacts=contacts)


@app.route("/messagerie/diffusion", methods=["POST"])
@login_required(role="admin")
def messagerie_diffusion():
    """Envoie un même message à tous les professeurs et élèves inscrits."""
    user = current_user()
    body = request.form.get("body", "").strip()
    if not body:
        flash("Écrivez un message avant de l'envoyer.", "error")
        return redirect(url_for("messagerie"))

    db = get_db()
    destinataires = db.execute(
        "SELECT id FROM users WHERE role IN ('prof', 'etudiant')"
    ).fetchall()
    for d in destinataires:
        db.execute(
            "INSERT INTO messages(sender_id, recipient_id, body) VALUES (?, ?, ?)",
            (user["id"], d["id"], body),
        )
    db.commit()
    flash(f"Message envoyé à {len(destinataires)} utilisateur(s).", "success")
    return redirect(url_for("messagerie"))


@app.route("/messagerie/<int:contact_id>", methods=["GET", "POST"])
@login_required()
def messagerie_conversation(contact_id):
    user = current_user()

    db = get_db()
    contact = db.execute("SELECT * FROM users WHERE id = ?", (contact_id,)).fetchone()
    if not contact or not is_allowed_contact(user, contact_id):
        flash("Vous ne pouvez échanger qu'avec un professeur ou un élève avec qui un cours a été réservé.", "error")
        return redirect(url_for("messagerie"))

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if body:
            db.execute(
                "INSERT INTO messages(sender_id, recipient_id, body) VALUES (?, ?, ?)",
                (user["id"], contact_id, body),
            )
            db.commit()
        return redirect(url_for("messagerie_conversation", contact_id=contact_id))

    # Marque comme lus les messages reçus de ce correspondant.
    db.execute(
        "UPDATE messages SET read_at = CURRENT_TIMESTAMP WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
        (contact_id, user["id"]),
    )
    db.commit()

    thread = db.execute(
        """
        SELECT * FROM messages
        WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
        ORDER BY id ASC
        """,
        (user["id"], contact_id, contact_id, user["id"]),
    ).fetchall()

    contacts = get_contacts(user)
    return render_template(
        "messagerie.html", contacts=contacts, active_contact=contact, thread=thread
    )


# ---------------------------------------------------------------------------
# Routes — Visioconférence
# ---------------------------------------------------------------------------
def get_course_and_partner(course_id, slot_id, user):
    """Vérifie que l'utilisateur a le droit d'accéder à la visio de ce créneau
    (le prof qui a créé le cours, ou l'élève qui a réservé ce créneau précis)
    et renvoie (cours, créneau, id_du_correspondant), ou (None, None, None)
    si l'accès est refusé. La réservation étant désormais faite créneau par
    créneau, un même cours peut être partagé par plusieurs élèves différents :
    c'est le créneau, pas le cours, qui détermine le binôme prof/élève."""
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        return None, None, None
    slot = db.execute(
        "SELECT * FROM slots WHERE id = ? AND course_id = ?", (slot_id, course_id)
    ).fetchone()
    if not slot:
        return None, None, None
    if user["role"] == "prof" and course["teacher_id"] == user["id"]:
        return course, slot, slot["reserved_by"]
    if user["role"] == "etudiant" and slot["reserved_by"] == user["id"]:
        return course, slot, course["teacher_id"]
    return None, None, None


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio")
@login_required()
def visio(course_id, slot_id):
    user = current_user()
    if user["role"] not in ("prof", "etudiant"):
        flash("La visioconférence est réservée aux professeurs et aux élèves.", "error")
        return redirect(dashboard_url_for(user["role"]))

    course, slot, partner_id = get_course_and_partner(course_id, slot_id, user)
    if not course or not slot:
        flash("Vous n'avez pas accès à la visioconférence de ce créneau.", "error")
        return redirect(dashboard_url_for(user["role"]))
    if not partner_id:
        flash("Ce créneau doit d'abord être réservé par un élève pour démarrer la visioconférence.", "error")
        return redirect(dashboard_url_for(user["role"]))
    if slot["slot_mode"] != "en_ligne":
        flash("Ce créneau se déroule en présentiel : pas de visioconférence pour ce cours.", "error")
        return redirect(dashboard_url_for(user["role"]))

    db = get_db()
    partner = db.execute("SELECT * FROM users WHERE id = ?", (partner_id,)).fetchone()

    # Historique des messages échangés avec ce correspondant, affiché dans le
    # panneau de chat de la visio (mêmes messages que dans la messagerie).
    chat_messages = db.execute(
        """
        SELECT * FROM messages
        WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
        ORDER BY id ASC
        """,
        (user["id"], partner_id, partner_id, user["id"]),
    ).fetchall()
    db.execute(
        "UPDATE messages SET read_at = CURRENT_TIMESTAMP WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
        (partner_id, user["id"]),
    )
    db.commit()

    return render_template("visio.html", course=course, slot=slot, partner=partner, chat_messages=chat_messages)


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/envoyer", methods=["POST"])
@login_required()
def visio_envoyer(course_id, slot_id):
    user = current_user()
    course, slot, partner_id = get_course_and_partner(course_id, slot_id, user)
    if not course or not slot or not partner_id:
        return jsonify({"error": "accès refusé"}), 403

    data = request.get_json(silent=True) or {}
    signal_type = data.get("type")
    payload = data.get("payload")
    if signal_type not in ("offer", "answer", "candidate", "leave") or payload is None:
        return jsonify({"error": "signal invalide"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO signals(course_id, sender_id, recipient_id, type, payload) VALUES (?, ?, ?, ?, ?)",
        (course_id, user["id"], partner_id, signal_type, json.dumps(payload)),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/recevoir")
@login_required()
def visio_recevoir(course_id, slot_id):
    user = current_user()
    course, slot, partner_id = get_course_and_partner(course_id, slot_id, user)
    if not course or not slot or not partner_id:
        return jsonify({"error": "accès refusé"}), 403

    since = request.args.get("since", 0, type=int)
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM signals
        WHERE course_id = ? AND recipient_id = ? AND id > ?
        ORDER BY id ASC
        """,
        (course_id, user["id"], since),
    ).fetchall()
    return jsonify(
        {
            "signals": [
                {"id": r["id"], "type": r["type"], "payload": json.loads(r["payload"])}
                for r in rows
            ]
        }
    )


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/message/envoyer", methods=["POST"])
@login_required()
def visio_chat_envoyer(course_id, slot_id):
    """Envoie un message de chat pendant la visio. Les messages sont stockés
    dans la même table que la messagerie classique : ils apparaissent donc
    aussi dans les conversations habituelles."""
    user = current_user()
    course, slot, partner_id = get_course_and_partner(course_id, slot_id, user)
    if not course or not slot or not partner_id:
        return jsonify({"error": "accès refusé"}), 403

    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "message vide"}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO messages(sender_id, recipient_id, body) VALUES (?, ?, ?)",
        (user["id"], partner_id, body),
    )
    db.commit()
    row = db.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(
        {
            "message": {
                "id": row["id"],
                "body": row["body"],
                "created_at": row["created_at"],
                "sender_id": row["sender_id"],
            }
        }
    )


@app.route("/cours/<int:course_id>/slots/<int:slot_id>/visio/message/recevoir")
@login_required()
def visio_chat_recevoir(course_id, slot_id):
    """Poll des nouveaux messages de chat échangés avec le correspondant de
    cette visio, pour affichage en direct dans le panneau de messagerie."""
    user = current_user()
    course, slot, partner_id = get_course_and_partner(course_id, slot_id, user)
    if not course or not slot or not partner_id:
        return jsonify({"error": "accès refusé"}), 403

    since = request.args.get("since", 0, type=int)
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM messages
        WHERE ((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?))
          AND id > ?
        ORDER BY id ASC
        """,
        (user["id"], partner_id, partner_id, user["id"], since),
    ).fetchall()
    # Les messages reçus du correspondant pendant la visio sont marqués lus
    # puisque l'utilisateur est en train de regarder la conversation.
    db.execute(
        "UPDATE messages SET read_at = CURRENT_TIMESTAMP WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
        (partner_id, user["id"]),
    )
    db.commit()
    return jsonify(
        {
            "messages": [
                {
                    "id": r["id"],
                    "body": r["body"],
                    "created_at": r["created_at"],
                    "sender_id": r["sender_id"],
                }
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------------------
# Routes — Paramètres (changement de mot de passe)
# ---------------------------------------------------------------------------
@app.route("/parametres", methods=["GET", "POST"])
@login_required()
def parametres():
    user = current_user()
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(user["password_hash"], current_password):
            flash("Mot de passe actuel incorrect.", "error")
            return render_template("parametres.html", user=user)
        if len(new_password) < 4:
            flash("Le nouveau mot de passe doit contenir au moins 4 caractères.", "error")
            return render_template("parametres.html", user=user)
        if new_password != confirm_password:
            flash("Les deux mots de passe saisis ne correspondent pas.", "error")
            return render_template("parametres.html", user=user)

        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user["id"]),
        )
        db.commit()
        flash("Mot de passe mis à jour avec succès.", "success")
        return redirect(url_for("parametres"))

    return render_template("parametres.html", user=user)


@app.route("/admin/utilisateurs/<int:user_id>/reinitialiser-mot-de-passe", methods=["POST"])
@login_required(role="admin")
def admin_reinitialiser_mot_de_passe(user_id):
    """Permet à l'administrateur de réinitialiser le mot de passe d'un
    professeur ou d'un élève qui ne peut plus se connecter (mot de passe
    oublié). Un nouveau mot de passe temporaire est généré et affiché une
    seule fois à l'admin, à transmettre à l'utilisateur concerné."""
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("Utilisateur introuvable.", "error")
        return redirect(url_for("admin_dashboard"))
    if target["role"] == "admin":
        flash("Impossible de réinitialiser le mot de passe d'un compte administrateur.", "error")
        return redirect(url_for("admin_dashboard"))

    new_password = secrets.token_urlsafe(6)
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )
    db.commit()
    flash(
        f"Nouveau mot de passe temporaire pour {target['name']} ({target['email']}) : {new_password} "
        "— transmettez-le-lui, il pourra le changer ensuite dans ses Paramètres.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------------
init_db()
start_auto_release_background_thread()

if __name__ == "__main__":
    app.run(debug=True)
