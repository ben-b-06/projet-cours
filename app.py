"""
CoursConnect — plateforme de cours en ligne (version Python / Flask + SQLite)

Lancer :
    pip install -r requirements.txt
    python app.py

Puis ouvrir http://127.0.0.1:5000
"""

import os
import calendar as calendar_mod
from datetime import date
from functools import wraps
import sqlite3

from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "coursconnect.db")

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"  # à remplacer par une vraie valeur secrète en production

# Choix fermés pour la matière et le niveau scolaire (du collège au lycée)
MATIERES = ["Mathématiques", "Physique-Chimie", "Français", "SVT", "SES", "Histoire-Géographie"]
NIVEAUX = ["6e", "5e", "4e", "3e", "2nde", "1re", "Terminale"]

# Identifiants du compte administrateur unique — aucun autre admin ne peut être créé.
ADMIN_EMAIL = "admin@cours.fr"
ADMIN_PASSWORD = "nezufnze48746è_ç"


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
            role TEXT NOT NULL CHECK(role IN ('admin', 'prof', 'etudiant'))
        );

        CREATE TABLE IF NOT EXISTS courses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            level TEXT NOT NULL,
            description TEXT NOT NULL,
            teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reserved_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS slots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            slot_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL
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
        """
    )

    # Migration : ajoute la colonne des sujets à travailler si elle n'existe pas déjà
    # (les bases créées avant cette fonctionnalité n'ont pas encore cette colonne).
    existing_columns = [row[1] for row in db.execute("PRAGMA table_info(courses)").fetchall()]
    if "student_notes" not in existing_columns:
        db.execute("ALTER TABLE courses ADD COLUMN student_notes TEXT")

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
        ("Camille Dubois", "camille@coursconnect.fr", "prof123", "prof"),
        ("Karim Haddad", "karim@coursconnect.fr", "prof123", "prof"),
        ("Léa Marchand", "lea@coursconnect.fr", "etu123", "etudiant"),
    ]
    ids = {}
    for name, email, password, role in demo_users:
        cur = db.execute(
            "INSERT INTO users(name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash(password), role),
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
        for slot_date, slot_time, duration in slots:
            db.execute(
                "INSERT INTO slots(course_id, slot_date, slot_time, duration_minutes) VALUES (?, ?, ?, ?)",
                (course_id, slot_date, slot_time, duration),
            )
        if title.startswith("Préparer le brevet") or title.startswith("Révisions SES"):
            db.execute(
                "UPDATE courses SET reserved_by = ? WHERE id = ?",
                (ids["lea@coursconnect.fr"], course_id),
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
    """Retourne {course_id: [slot, ...]} triés par date/heure pour une liste d'ids de cours."""
    if not course_ids:
        return {}
    placeholders = ",".join("?" for _ in course_ids)
    rows = db.execute(
        f"SELECT * FROM slots WHERE course_id IN ({placeholders}) ORDER BY slot_date, slot_time",
        list(course_ids),
    ).fetchall()
    slots_map = {cid: [] for cid in course_ids}
    for r in rows:
        slots_map[r["course_id"]].append(r)
    return slots_map


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


@app.route("/cours")
def cours():
    db = get_db()
    user = current_user()

    q = request.args.get("q", "").strip()
    subject = request.args.get("subject", "").strip()
    level = request.args.get("level", "").strip()
    teacher_id = request.args.get("teacher_id", "").strip()

    conditions = []
    params = []

    if user and user["role"] == "etudiant":
        conditions.append("(c.reserved_by IS NULL OR c.reserved_by = ?)")
        params.append(user["id"])
    else:
        conditions.append("c.reserved_by IS NULL")

    if q:
        conditions.append("(c.title LIKE ? OR c.description LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])

    if subject in MATIERES:
        conditions.append("c.subject = ?")
        params.append(subject)

    if level in NIVEAUX:
        conditions.append("c.level = ?")
        params.append(level)

    if teacher_id.isdigit():
        conditions.append("c.teacher_id = ?")
        params.append(int(teacher_id))

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    rows = db.execute(
        f"""
        SELECT c.*, u.name AS teacher_name
        FROM courses c
        JOIN users u ON u.id = c.teacher_id
        WHERE {where_clause}
        ORDER BY c.id DESC
        """,
        params,
    ).fetchall()

    teachers = db.execute(
        "SELECT id, name FROM users WHERE role = 'prof' ORDER BY name"
    ).fetchall()

    slots_map = get_slots_map(db, [r["id"] for r in rows])
    return render_template(
        "cours.html",
        courses=rows,
        slots_map=slots_map,
        matieres=MATIERES,
        niveaux=NIVEAUX,
        teachers=teachers,
        filters={"q": q, "subject": subject, "level": level, "teacher_id": teacher_id},
    )


@app.route("/cours/<int:course_id>/inscription", methods=["POST"])
@login_required(role="etudiant")
def inscription_cours(course_id):
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    notes = request.form.get("notes", "").strip()
    if not course:
        flash("Cours introuvable.", "error")
    elif course["reserved_by"] is not None:
        flash("Ce cours vient d'être réservé par un autre élève.", "error")
    else:
        db.execute(
            "UPDATE courses SET reserved_by = ?, student_notes = ? WHERE id = ? AND reserved_by IS NULL",
            (current_user()["id"], notes or None, course_id),
        )
        # On garde une trace durable de la mise en relation élève/prof pour la
        # messagerie, même si l'élève se désinscrit plus tard.
        db.execute(
            "INSERT OR IGNORE INTO contacts(student_id, teacher_id, course_id) VALUES (?, ?, ?)",
            (current_user()["id"], course["teacher_id"], course_id),
        )
        db.commit()
        flash("Cours réservé ! Retrouvez-le dans « Mes cours ».", "success")
    return redirect(url_for("cours"))


@app.route("/cours/<int:course_id>/desinscription", methods=["POST"])
@login_required(role="etudiant")
def desinscription_cours(course_id):
    db = get_db()
    db.execute(
        "UPDATE courses SET reserved_by = NULL WHERE id = ? AND reserved_by = ?",
        (course_id, current_user()["id"]),
    )
    db.commit()
    flash("Vous avez annulé ce cours.", "success")
    return redirect(request.referrer or url_for("cours"))


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


@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role")

        if role not in ("prof", "etudiant"):
            flash("Merci de choisir un rôle valide.", "error")
            return render_template("inscription.html")

        if not name or not email or len(password) < 4:
            flash("Merci de renseigner un nom, un e-mail et un mot de passe d'au moins 4 caractères.", "error")
            return render_template("inscription.html")

        db = get_db()
        if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            flash("Un compte existe déjà avec cet e-mail.", "error")
            return render_template("inscription.html")

        cur = db.execute(
            "INSERT INTO users(name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash(password), role),
        )
        db.commit()
        session["user_id"] = cur.lastrowid
        flash(f"Compte créé. Bienvenue, {name} !", "success")
        return redirect(dashboard_url_for(role))
    return render_template("inscription.html")


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
        """
        SELECT c.*, s.name AS student_name
        FROM courses c
        LEFT JOIN users s ON s.id = c.reserved_by
        WHERE c.teacher_id = ?
        ORDER BY c.id DESC
        """,
        (user["id"],),
    ).fetchall()
    reserved_count = sum(1 for r in rows if r["reserved_by"] is not None)
    slots_map = get_slots_map(db, [r["id"] for r in rows])
    return render_template(
        "prof_dashboard.html", courses=rows, reserved_count=reserved_count, slots_map=slots_map
    )


@app.route("/prof/creer", methods=["GET", "POST"])
@login_required(role="prof")
def prof_creer():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        subject = request.form.get("subject", "")
        level = request.form.get("level", "")
        description = request.form.get("description", "").strip()

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
                form=request.form,
            )

        db = get_db()
        cur = db.execute(
            "INSERT INTO courses(title, subject, level, description, teacher_id) VALUES (?, ?, ?, ?, ?)",
            (title, subject, level, description, current_user()["id"]),
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

    return render_template("prof_creer.html", matieres=MATIERES, niveaux=NIVEAUX, form={})


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
        FROM users u WHERE u.role = 'prof' ORDER BY u.name
        """
    ).fetchall()
    etudiants = db.execute(
        """
        SELECT u.*, (SELECT COUNT(*) FROM courses c WHERE c.reserved_by = u.id) AS course_count
        FROM users u WHERE u.role = 'etudiant' ORDER BY u.name
        """
    ).fetchall()
    courses_rows = db.execute(
        """
        SELECT c.*, u.name AS teacher_name, s.name AS student_name
        FROM courses c
        JOIN users u ON u.id = c.teacher_id
        LEFT JOIN users s ON s.id = c.reserved_by
        ORDER BY c.id DESC
        """
    ).fetchall()
    return render_template(
        "admin_dashboard.html",
        profs=profs,
        etudiants=etudiants,
        courses=courses_rows,
        slots_map=get_slots_map(db, [c["id"] for c in courses_rows]),
    )


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


# ---------------------------------------------------------------------------
# Routes — Étudiant
# ---------------------------------------------------------------------------
@app.route("/etudiant/mes-cours")
@login_required(role="etudiant")
def etudiant():
    db = get_db()
    rows = db.execute(
        """
        SELECT c.*, u.name AS teacher_name
        FROM courses c
        JOIN users u ON u.id = c.teacher_id
        WHERE c.reserved_by = ?
        ORDER BY c.id DESC
        """,
        (current_user()["id"],),
    ).fetchall()
    slots_map = get_slots_map(db, [r["id"] for r in rows])
    return render_template("etudiant.html", courses=rows, slots_map=slots_map)


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

    courses = db.execute(
        """
        SELECT c.*, u.name AS teacher_name
        FROM courses c JOIN users u ON u.id = c.teacher_id
        WHERE c.reserved_by = ?
        """,
        (current_user()["id"],),
    ).fetchall()
    slots_map = get_slots_map(db, [c["id"] for c in courses])
    course_by_id = {c["id"]: c for c in courses}

    events_by_date = {}
    for course_id, slots in slots_map.items():
        c = course_by_id[course_id]
        for s in slots:
            events_by_date.setdefault(s["slot_date"], []).append(
                {
                    "title": c["title"],
                    "subtitle": f"{c['subject']} · {c['level']} · {c['teacher_name']}",
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

    courses = db.execute(
        """
        SELECT c.*, s.name AS student_name
        FROM courses c LEFT JOIN users s ON s.id = c.reserved_by
        WHERE c.teacher_id = ?
        """,
        (current_user()["id"],),
    ).fetchall()
    slots_map = get_slots_map(db, [c["id"] for c in courses])
    course_by_id = {c["id"]: c for c in courses}

    events_by_date = {}
    for course_id, slots in slots_map.items():
        c = course_by_id[course_id]
        is_reserved = c["reserved_by"] is not None
        for s in slots:
            events_by_date.setdefault(s["slot_date"], []).append(
                {
                    "title": c["title"],
                    "subtitle": f"{c['subject']} · {c['level']}"
                    + (f" · {c['student_name']}" if is_reserved else " · Disponible"),
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
def get_contacts(user):
    """Retourne la liste des correspondants autorisés (profs pour un élève,
    élèves pour un prof), avec le nombre de messages non lus et un aperçu
    du dernier message échangé."""
    db = get_db()
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
        last = db.execute(
            """
            SELECT body, created_at, sender_id FROM messages
            WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
            ORDER BY id DESC LIMIT 1
            """,
            (user["id"], r["id"], r["id"], user["id"]),
        ).fetchone()
        unread = db.execute(
            "SELECT COUNT(*) FROM messages WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
            (r["id"], user["id"]),
        ).fetchone()[0]
        contacts.append({"id": r["id"], "name": r["name"], "role": r["role"], "last": last, "unread": unread})
    contacts.sort(key=lambda c: (c["last"]["created_at"] if c["last"] else ""), reverse=True)
    return contacts


def is_allowed_contact(user, other_id):
    db = get_db()
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
    if user and user["role"] in ("prof", "etudiant"):
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
    if user["role"] not in ("prof", "etudiant"):
        flash("La messagerie est réservée aux professeurs et aux élèves.", "error")
        return redirect(dashboard_url_for(user["role"]))
    contacts = get_contacts(user)
    return render_template("messagerie.html", contacts=contacts)


@app.route("/messagerie/<int:contact_id>", methods=["GET", "POST"])
@login_required()
def messagerie_conversation(contact_id):
    user = current_user()
    if user["role"] not in ("prof", "etudiant"):
        flash("La messagerie est réservée aux professeurs et aux élèves.", "error")
        return redirect(dashboard_url_for(user["role"]))

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
init_db()

if __name__ == "__main__":
    app.run(debug=True)
