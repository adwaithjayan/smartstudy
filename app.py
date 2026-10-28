"""SmartStudy AI: notes -> learning kit -> quiz -> coach -> focused practice -> study plan."""
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
import click
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()
import ai  # noqa: E402  (after load_dotenv so env vars are visible)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("SMARTSTUDY_DB", str(BASE_DIR / "smartstudy.db"))
MAX_NOTES = 15000          # matches the amount Gemini actually sees
MAX_PDF_BYTES = 10 * 1024 * 1024
DECAY = 0.85               # weight of older attempts when judging weak topics
WEAK_BELOW = 80            # accuracy % under which a topic is "weak"
GENERIC_TOPICS = {"study", "review", "computers", "computer", "question", "questions", "general",
                  "basics", "topic", "lesson", "unknown", "miscellaneous"}

DEFAULT_TOPIC = "Introduction to Computer Science"
DEFAULT_NOTES = ("Computer science is the study of computers and computational systems. An algorithm is a "
                 "step-by-step set of instructions used to solve a problem. Programming languages allow people "
                 "to write instructions that computers can execute. Data structures help organize information "
                 "efficiently.")
UNIT_SCOPE = ("Create a QUICK-REVISION, college-level kit ONLY for the selected unit. Do not teach other units. "
              "Keep it concise: 3-5 core concepts, short explanations, examples only where useful, 4-6 key terms, "
              "a short recap. Do not claim details are in the syllabus unless they appear in it.")

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_PDF_BYTES, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",  # set true behind HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(days=14))


def load_secret_key():
    """Use FLASK_SECRET_KEY if set, otherwise create and reuse a random key (never a public default)."""
    env = os.getenv("FLASK_SECRET_KEY", "").strip()
    if env:
        return env
    path = BASE_DIR / ".secret_key"
    try:
        if path.exists():
            return path.read_text().strip()
        key = secrets.token_hex(32)
        path.write_text(key)
        return key
    except OSError:
        return secrets.token_hex(32)


app.secret_key = load_secret_key()


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT,
            password_hash TEXT NOT NULL, created_at TEXT);
        CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, notes TEXT,
            summary TEXT, questions TEXT, kit TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, user_id INTEGER, lesson_id INTEGER, score INTEGER,
            total INTEGER, topics TEXT, created_at TEXT, answers TEXT, questions_snapshot TEXT,
            FOREIGN KEY(lesson_id) REFERENCES lessons(id));
        CREATE TABLE IF NOT EXISTS syllabi(id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, source_name TEXT,
            extracted_text TEXT, outline TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS topic_practice_sets(id INTEGER PRIMARY KEY, user_id INTEGER, topic TEXT,
            lesson_id INTEGER, questions TEXT, created_at TEXT);
    """)
    # Migrations for databases created by earlier versions.
    for table, column, kind in (("lessons", "kit", "TEXT"), ("attempts", "answers", "TEXT"),
                                ("attempts", "questions_snapshot", "TEXT"), ("lessons", "user_id", "INTEGER"),
                                ("attempts", "user_id", "INTEGER"), ("syllabi", "user_id", "INTEGER"),
                                ("topic_practice_sets", "user_id", "INTEGER")):
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    for table in ("lessons", "attempts", "syllabi", "topic_practice_sets"):
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_user ON {table}(user_id)")
    db.commit()
    db.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(raw, default):
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def save_lesson(db, user_id, title, notes, kit):
    """Persist the whole kit (not just summary + questions) so a saved lesson is complete."""
    cur = db.execute(
        "INSERT INTO lessons(user_id,title,notes,summary,questions,kit,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, title, notes, kit["summary"], json.dumps(kit["questions"], ensure_ascii=False),
         json.dumps(kit, ensure_ascii=False), now()))
    db.commit()
    return cur.lastrowid


def record_attempt(db, user_id, lesson_id, score, topics, submitted, questions):
    db.execute(
        "INSERT INTO attempts(user_id,lesson_id,score,total,topics,created_at,answers,questions_snapshot) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, lesson_id, score, len(questions), json.dumps(topics), now(), json.dumps(submitted),
         json.dumps(questions, ensure_ascii=False)))
    db.commit()


# ---------------------------------------------------------------- security

def _csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.context_processor
def inject_helpers():
    return {"csrf_field": lambda: Markup(f'<input type="hidden" name="csrf_token" value="{_csrf_token()}">')}


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not hmac.compare_digest(session.get("_csrf", ""), sent):
            abort(400)


@app.errorhandler(400)
def bad_request(_err):
    flash("Your session expired or the form was invalid. Please try again.", "error")
    return redirect(url_for("dashboard"))


@app.errorhandler(413)
def too_large(_err):
    flash("PDF is too large. Maximum size is 10 MB.", "error")
    return redirect(url_for("syllabus_upload"))


@app.template_filter("when")
def fmt_when(value):
    return str(value or "")[:16].replace("T", " ")


# ---------------------------------------------------------------- accounts

PUBLIC_ENDPOINTS = {"login", "register", "health", "static"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FAILED_LOGINS = {}   # "ip|email" -> timestamps (in-memory brute-force throttle)


@app.before_request
def load_user():
    """Identify who is making the request; everything except login/register needs an account."""
    g.user = None
    user_id = session.get("uid")
    if user_id:
        g.user = get_db().execute("SELECT id,email,name FROM users WHERE id=?", (user_id,)).fetchone()
        if g.user is None:
            session.pop("uid", None)
    if g.user is None and request.endpoint and request.endpoint not in PUBLIC_ENDPOINTS:
        nxt = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("login", next=nxt))


def uid():
    return g.user["id"]


def safe_next(target):
    """Only follow same-site relative redirects (blocks open-redirect tricks)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("dashboard")


def _throttled(key):
    recent = [t for t in _FAILED_LOGINS.get(key, []) if time.time() - t < 600]
    _FAILED_LOGINS[key] = recent
    return len(recent) >= 5


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    name, email = clean_spaces(request.form.get("name", ""))[:60], request.form.get("email", "").strip().lower()
    if request.method == "POST":
        password, confirm = request.form.get("password", ""), request.form.get("confirm", "")
        if not EMAIL_RE.match(email) or len(email) > 254:
            flash("Enter a valid email address.", "error")
        elif len(password) < 8:
            flash("Choose a password of at least 8 characters.", "error")
        elif password != confirm:
            flash("The two passwords do not match.", "error")
        else:
            db = get_db()
            first_user = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            try:
                cur = db.execute("INSERT INTO users(email,name,password_hash,created_at) VALUES(?,?,?,?)",
                                 (email, name or email.split("@")[0], generate_password_hash(password), now()))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists. Try logging in.", "error")
                return render_template("register.html", name=name, email=email)
            if first_user:  # data created before accounts existed goes to the first account
                for table in ("lessons", "attempts", "syllabi", "topic_practice_sets"):
                    db.execute(f"UPDATE {table} SET user_id=? WHERE user_id IS NULL", (cur.lastrowid,))
            db.commit()
            session.clear()
            session["uid"] = cur.lastrowid
            session.permanent = True
            flash("Welcome to SmartStudy AI! Your account is ready.", "success")
            return redirect(url_for("dashboard"))
    return render_template("register.html", name=name, email=email)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    email = request.form.get("email", "").strip().lower()
    if request.method == "POST":
        key = f"{request.remote_addr}|{email}"
        if _throttled(key):
            flash("Too many failed attempts. Please wait 10 minutes and try again.", "error")
        else:
            row = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                _FAILED_LOGINS.pop(key, None)
                session.clear()                      # new session on login (prevents fixation)
                session["uid"] = row["id"]
                session.permanent = True
                return redirect(safe_next(request.args.get("next")))
            _FAILED_LOGINS.setdefault(key, []).append(time.time())
            flash("Incorrect email or password.", "error")
    return render_template("login.html", email=email)


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.post("/demo/load")
def load_demo():
    """Give the current user a pre-built lesson (works with no Gemini connection)."""
    from demo_kit import DEMO_KIT, DEMO_NOTES, DEMO_TITLE
    lid = save_lesson(get_db(), uid(), DEMO_TITLE, DEMO_NOTES, DEMO_KIT)
    flash("Demo lesson added to your library.", "success")
    return redirect(url_for("saved_lesson", lid=lid))


# ---------------------------------------------------------------- topic statistics

def clean_spaces(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_topic(value):
    return clean_spaces(value).casefold()


def _add(stats, topic, correct, total, weight=1.0):
    label = clean_spaces(topic)
    key = normalize_topic(label)
    if not key or not total:
        return
    s = stats.setdefault(key, {"topic": label, "correct": 0, "total": 0, "wc": 0.0, "wt": 0.0})
    s["correct"] += correct
    s["total"] += total
    s["wc"] += correct * weight
    s["wt"] += total * weight


def _weak_list(stats, limit=5):
    weak = []
    for key, s in stats.items():
        if key in GENERIC_TOPICS or len(key) < 3 or not s["wt"]:
            continue
        pct = round(100 * s["wc"] / s["wt"])
        if pct < WEAK_BELOW:
            weak.append({"topic": s["topic"], "accuracy": pct, "correct": s["correct"], "total": s["total"]})
    weak.sort(key=lambda x: (x["accuracy"], -x["total"], x["topic"].casefold()))
    return weak[:limit]


def get_weak_topics(db, user_id):
    """Weak topics across recent attempts. Newer attempts at a topic count more than older ones,
    so a topic you have since mastered stops showing up as weak."""
    stats, seen = {}, {}
    for row in db.execute("SELECT topics FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 200", (user_id,)):
        for topic, value in load_json(row["topics"], {}).items():
            if not isinstance(value, dict):
                continue
            key = normalize_topic(topic)
            n = seen.get(key, 0)
            seen[key] = n + 1
            try:
                _add(stats, topic, int(value.get("correct", 0)), int(value.get("total", 0)), DECAY ** n)
            except (TypeError, ValueError):
                continue
    return _weak_list(stats)


def weak_from_attempt(topics):
    """Weak topics for one just-finished attempt."""
    stats = {}
    for topic, value in topics.items():
        _add(stats, topic, value["correct"], value["total"])
    return _weak_list(stats)


# ---------------------------------------------------------------- quiz helpers

def grade(questions, form, default_topic):
    score, topics, submitted = 0, {}, []
    for i, q in enumerate(questions):
        try:
            selected = int(form.get(f"q{i}", "-1"))
        except ValueError:
            selected = -1
        ok = selected == q["answer"]
        score += int(ok)
        stat = topics.setdefault(q.get("topic") or default_topic, {"correct": 0, "total": 0})
        stat["correct"] += int(ok)
        stat["total"] += 1
        submitted.append({"index": i, "selected": selected, "correct": ok})
    return score, topics, submitted


def build_review(questions, submitted):
    review = []
    for q, item in zip(questions, submitted):
        sel = item["selected"]
        review.append({"question": q["question"], "ok": item["correct"],
                       "your": q["options"][sel] if sel in range(len(q["options"])) else "Not answered",
                       "correct": q["options"][q["answer"]], "explanation": q.get("explanation", "")})
    return review


# ---------------------------------------------------------------- dashboard

@app.get("/")
def dashboard():
    db = get_db()
    lessons = db.execute("SELECT id,title,created_at FROM lessons WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid(),)).fetchall()
    total_lessons = db.execute("SELECT COUNT(*) FROM lessons WHERE user_id=?", (uid(),)).fetchone()[0]
    recent = db.execute("SELECT score,total FROM attempts WHERE user_id=? AND total>0 ORDER BY id DESC LIMIT 2", (uid(),)).fetchall()
    acc = round(100 * recent[0]["score"] / recent[0]["total"]) if recent else None
    if len(recent) == 2 and acc is not None:
        prev = round(100 * recent[1]["score"] / recent[1]["total"])
        trend = "Same as previous quiz" if prev == acc else f"{acc - prev:+d}% from previous quiz"
    else:
        trend = "Your latest completed quiz"
    return render_template("dashboard.html", lessons=lessons, total_lessons=total_lessons, acc=acc,
                           trend=trend, weak=get_weak_topics(db, uid()))


# ---------------------------------------------------------------- lesson lab

@app.route("/lesson", methods=["GET", "POST"])
def lesson_lab():
    topic = request.form.get("topic", DEFAULT_TOPIC).strip()[:160]
    notes = request.form.get("notes", DEFAULT_NOTES).strip()
    if request.method == "POST":
        if len(notes) < 40:
            flash("Please enter at least 40 characters of lesson notes.", "error")
        elif len(notes) > MAX_NOTES:
            flash(f"Notes are limited to {MAX_NOTES:,} characters. Split them into several lessons.", "error")
        else:
            try:
                title = topic or "Untitled lesson"
                kit = ai.make_kit(title, notes)
                lid = save_lesson(get_db(), uid(), title, notes, kit)
                flash("Learning kit created. Review it, then start the quiz when ready.", "success")
                return redirect(url_for("saved_lesson", lid=lid))  # redirect: a refresh can't duplicate it
            except ai.AIError as exc:
                flash(str(exc), "error")
    return render_template("lesson.html", topic=topic, notes=notes, max_notes=MAX_NOTES)


@app.route("/lesson/<int:lid>")
def saved_lesson(lid):
    lesson = get_db().execute("SELECT * FROM lessons WHERE id=? AND user_id=?", (lid, uid())).fetchone()
    if not lesson:
        flash("Saved lesson not found.", "error")
        return redirect(url_for("dashboard"))
    kit = load_json(lesson["kit"], {}) or None   # None for lessons saved by older versions
    questions = load_json(lesson["questions"], [])
    return render_template("saved_lesson.html", lesson=lesson, kit=kit, questions=questions)


# ---------------------------------------------------------------- quiz

@app.route("/quiz/<int:lid>", methods=["GET", "POST"])
def quiz(lid):
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id=? AND user_id=?", (lid, uid())).fetchone()
    if not lesson:
        flash("Lesson not found.", "error")
        return redirect(url_for("lesson_lab"))
    questions = load_json(lesson["questions"], [])
    if not questions:
        flash("This lesson has no saved questions. Generate a new kit.", "error")
        return redirect(url_for("saved_lesson", lid=lid))

    if request.method == "POST":
        score, topics, submitted = grade(questions, request.form, lesson["title"])
        record_attempt(db, uid(), lid, score, topics, submitted, questions)
        coach, coach_error = None, None
        try:
            coach = ai.make_coach_feedback(lesson["title"], lesson["notes"], questions, submitted, score)
        except ai.AIError as exc:
            app.logger.warning("Coach unavailable: %s", exc)
            coach_error = str(exc)
        return render_template("quiz_result.html", lesson=lesson, score=score, total=len(questions),
                               pct=round(100 * score / len(questions)), review=build_review(questions, submitted),
                               weak=weak_from_attempt(topics), coach=coach, coach_error=coach_error)
    return render_template("quiz.html", lesson=lesson, questions=questions)


# ---------------------------------------------------------------- planner

def build_plan(lesson, weak, minutes, exam_str, custom_subject):
    days_left = None
    if exam_str:
        try:
            days_left = (date.fromisoformat(exam_str) - date.today()).days
        except ValueError:
            days_left = None
        if days_left is not None and days_left < 0:
            days_left = None

    if lesson:
        title = lesson["title"] or "Untitled lesson"
        kit = load_json(lesson["kit"], {})
        questions = load_json(lesson["questions"], [])
        topics = []
        for q in questions:
            label = clean_spaces(q.get("topic")) if isinstance(q, dict) else ""
            if label and label.casefold() not in {t.casefold() for t in topics}:
                topics.append(label)
        concepts = len(kit.get("concepts", []))
        terms = len(kit.get("key_terms", []))
        if kit:
            complexity = (concepts >= 5) + (len(topics) >= 5) + (terms >= 8) + (len(questions) >= 8)
        else:  # lessons saved by an older version have no kit
            words = len(re.findall(r"\w+", lesson["notes"] or ""))
            complexity = (words >= 250) + (words >= 650)
        level = "detailed" if complexity >= 3 else "focused" if complexity == 0 else "balanced"
        ratios = {"detailed": (.42, .38, .20), "focused": (.32, .40, .28), "balanced": (.36, .40, .24)}[level]

        keys = {normalize_topic(t) for t in topics}
        priority = [w["topic"] for w in weak if normalize_topic(w["topic"]) in keys]
        focus = ", ".join((priority or topics)[:3])
        if focus:
            lead = f"starting with the topics your Study Coach flagged: {focus}" if priority else f"prioritizing {focus}"
            review = f"Review “{title}”, {lead}. Compare the explanations with your saved notes."
            practice = f"Practise the {len(questions)} saved questions, giving extra attention to {focus} and explaining why each answer is correct."
            recall = f"Without looking at “{title}”, explain {focus} from memory, then check gaps against your notes."
        else:
            review = f"Review “{title}” and mark the concepts that need another pass."
            practice = "Work through this lesson's saved questions; if there are none, write a few from its key ideas."
            recall = "Close the lesson and summarize its main ideas from memory, then check what you missed."
        subject = title
        source_note = f"Personalized from “{title}” ({concepts} concepts, {len(questions)} saved questions, {level} workload)."
    else:
        subject = custom_subject or "Your subject"
        level, ratios = "balanced", (.36, .40, .24)
        review = f"Start with the core ideas in {subject}. No saved lesson was selected, so this plan uses the topic name only."
        practice = f"Solve practice questions about {subject}: basic understanding first, then application."
        recall = f"Close your notes and explain the most important ideas in {subject} from memory."
        source_note = "Custom-topic plan: select a saved lesson to personalize timing from its notes and quiz topics."

    exam_note = ""
    if days_left is not None:
        if days_left <= 1:
            ratios = (.25, .50, .25)  # exam is imminent: lean on practice and recall, not new reading
            exam_note = ("Your exam is today. Keep this session light and focus on recall." if days_left == 0
                         else "Your exam is tomorrow. This plan leans toward practice and recall instead of new reading.")
        else:
            sessions = min(days_left, 7)
            exam_note = (f"{days_left} days until your exam. Repeat this {minutes}-minute plan on about {sessions} "
                         f"separate days (≈{sessions * minutes} minutes in total), weakest topics first. "
                         "Spaced sessions beat one long cram.")

    a, b = round(minutes * ratios[0]), round(minutes * ratios[1])
    items = [("Review key concepts", a, review), ("Practice questions", b, practice),
             ("Active recall", minutes - a - b, recall)]
    return {"subject": subject, "items": items, "total": minutes, "note": source_note, "exam_note": exam_note}


@app.route("/planner", methods=["GET", "POST"])
def planner():
    db = get_db()
    saved = db.execute("SELECT id,title,notes,summary,questions,kit,created_at FROM lessons WHERE user_id=? ORDER BY id DESC", (uid(),)).fetchall()
    weak = get_weak_topics(db, uid())
    selected = request.form.get("lesson_id", "")
    exam = request.form.get("exam", "")
    custom = request.form.get("custom_subject", "").strip()[:160]
    try:
        minutes = max(15, min(240, int(request.form.get("minutes", "60"))))
    except ValueError:
        minutes = 60

    plan = None
    if request.method == "POST":
        lesson = None
        if selected != "custom":
            lesson = next((l for l in saved if str(l["id"]) == selected), None)
            if not lesson:
                flash("Please choose one of your saved lessons.", "error")
        if lesson or selected == "custom":
            plan = build_plan(lesson, weak, minutes, exam, custom)

    return render_template("planner.html", saved=saved, plan=plan, selected=selected, minutes=minutes,
                           exam=exam, custom=custom, today=date.today().isoformat(),
                           show_custom=(selected == "custom" or not saved),
                           minute_options=[30, 45, 60, 90, 120, 180, 240])


# ---------------------------------------------------------------- syllabus

def extract_pdf_text(upload):
    if not upload or not upload.filename:
        raise ValueError("Choose a syllabus PDF to upload.")
    if not upload.filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are supported.")
    raw = upload.read(MAX_PDF_BYTES + 1)
    if len(raw) > MAX_PDF_BYTES:
        raise ValueError("PDF is too large. Maximum size is 10 MB.")
    if not raw.startswith(b"%PDF-"):
        raise ValueError("The uploaded file is not a valid PDF.")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError("This PDF is password-protected. Upload an unlocked copy.")
        if len(reader.pages) > 250:
            raise ValueError("PDF has too many pages. Maximum is 250 pages.")
        text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except ImportError as exc:
        raise RuntimeError("Install PDF support with: pip install pypdf") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Could not read this PDF. If it is scanned, OCR it first and upload the searchable PDF.") from exc
    if len(text) < 80:
        raise ValueError("Very little selectable text was found. This may be scanned; OCR it first.")
    return text[:35000], os.path.basename(upload.filename)[:180]


@app.route("/syllabus", methods=["GET", "POST"])
def syllabus_upload():
    if request.method == "POST":
        try:
            text, source = extract_pdf_text(request.files.get("syllabus_pdf"))
            outline = ai.extract_syllabus_outline(text)
            db = get_db()
            cur = db.execute("INSERT INTO syllabi(user_id,title,source_name,extracted_text,outline,created_at) VALUES(?,?,?,?,?,?)",
                             (uid(), outline["title"], source, text, json.dumps(outline, ensure_ascii=False), now()))
            db.commit()
            flash("Syllabus extracted. Choose a unit to generate a lesson.", "success")
            return redirect(url_for("syllabus_detail", sid=cur.lastrowid))
        except (ValueError, RuntimeError) as exc:   # includes ai.AIError
            flash(str(exc), "error")
        except Exception:
            app.logger.exception("Syllabus extraction failed")
            flash("Could not extract the syllabus. Please try again.", "error")
    return render_template("syllabus_upload.html")


@app.route("/syllabus/<int:sid>", methods=["GET", "POST"])
def syllabus_detail(sid):
    db = get_db()
    row = db.execute("SELECT * FROM syllabi WHERE id=? AND user_id=?", (sid, uid())).fetchone()
    if not row:
        flash("Syllabus not found. Upload it again.", "error")
        return redirect(url_for("syllabus_upload"))
    outline = load_json(row["outline"], {})
    units = outline.get("units", [])
    if request.method == "POST":
        try:
            idx = int(request.form.get("unit_index", "-1"))
            if not 0 <= idx < len(units):
                raise ValueError("Select a valid unit.")
            unit = units[idx]
            notes = (f"Syllabus: {row['title']}\nSelected unit: {unit['title']}\n"
                     f"Topics listed for this unit: {unit.get('topics', '')}\n\n"
                     "SOURCE SYLLABUS TEXT (use only material relevant to this unit):\n" + row["extracted_text"][:13000])
            kit = ai.make_kit(unit["title"], notes, scope=UNIT_SCOPE)   # scope is no longer truncated away
            lid = save_lesson(db, uid(), unit["title"], notes, kit)
            flash(f"Quick learning kit generated and saved for {unit['title']}.", "success")
            return redirect(url_for("saved_lesson", lid=lid))
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
    return render_template("syllabus_detail.html", row=row, outline=outline, units=units)


# ---------------------------------------------------------------- focused topic practice

def _start_practice_set(db, topic):
    matched, source = [], None
    for lesson in db.execute("SELECT id,title,notes,questions FROM lessons WHERE user_id=? ORDER BY id DESC", (uid(),)):
        found = [q for q in load_json(lesson["questions"], [])
                 if isinstance(q, dict) and normalize_topic(q.get("topic")) == normalize_topic(topic)
                 and isinstance(q.get("options"), list) and len(q["options"]) == 4 and q.get("answer") in range(4)]
        if found:
            matched.extend(found)
            source = source or lesson
    if not matched:
        flash("No saved lesson questions match this topic yet. Create a lesson kit with this topic first.", "error")
        return redirect(url_for("lesson_lab"))

    questions = matched[:10]
    if len(questions) < 5:   # top up with grounded extras so the session is a useful size
        try:
            questions += ai.generate_topic_practice(topic, source["notes"] or "", 5 - len(questions))
        except ai.AIError as exc:
            flash(f"Could not build five questions for this topic: {exc}", "error")
            return redirect(url_for("dashboard"))
    cur = db.execute("INSERT INTO topic_practice_sets(user_id,topic,lesson_id,questions,created_at) VALUES(?,?,?,?,?)",
                     (uid(), topic, source["id"], json.dumps(questions, ensure_ascii=False), now()))
    db.commit()
    return redirect(url_for("topic_practice", topic=topic, set_id=cur.lastrowid))


@app.route("/topic-practice", methods=["GET", "POST"])
def topic_practice():
    topic = clean_spaces(request.args.get("topic", ""))
    if not topic:
        flash("Choose a topic from Your Study Coach first.", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    set_id = request.args.get("set_id", "")
    if not set_id.isdigit():
        return _start_practice_set(db, topic)
    row = db.execute("SELECT * FROM topic_practice_sets WHERE id=? AND topic=? AND user_id=?", (int(set_id), topic, uid())).fetchone()
    questions = load_json(row["questions"], []) if row else []
    if not questions:
        flash("That practice set could not be found. Starting a new one.", "error")
        return redirect(url_for("topic_practice", topic=topic))

    if request.method == "POST":
        score, topics, submitted = grade(questions, request.form, topic)
        record_attempt(db, uid(), row["lesson_id"], score, topics, submitted, questions)
        return render_template("topic_result.html", topic=topic, score=score, total=len(questions),
                               pct=round(100 * score / len(questions)), review=build_review(questions, submitted))
    return render_template("topic_practice.html", topic=topic, questions=questions)


@app.get("/health")
def health():
    return {"status": "ok", "app": "SmartStudy AI"}


@app.cli.command("seed-demo")
@click.option("--email", required=True, help="Email of the account that should receive the demo lesson.")
def seed_demo(email):
    """Add a pre-built lesson to an account (also available as a button on the dashboard)."""
    from demo_kit import DEMO_KIT, DEMO_NOTES, DEMO_TITLE
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    user = db.execute("SELECT id FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not user:
        raise click.ClickException("No account with that email. Register first.")
    lid = save_lesson(db, user["id"], DEMO_TITLE, DEMO_NOTES, DEMO_KIT)
    db.close()
    click.echo(f"Demo lesson saved (id {lid}).")


init_db()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")),
            debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
