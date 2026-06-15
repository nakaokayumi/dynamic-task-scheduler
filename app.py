import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(24))

DATABASE = "scheduler.db"


# ---------------- DB ----------------
def get_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:

        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            age INTEGER,
            occupation TEXT,
            wake_time TEXT,
            email TEXT UNIQUE,
            password_hash TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            priority INTEGER,
            urgency INTEGER,
            difficulty INTEGER,
            duration INTEGER,
            is_completed INTEGER DEFAULT 0
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            start_time TEXT,
            end_time TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_energy (
            hour INTEGER PRIMARY KEY,
            energy_level INTEGER
        )
        """)

        if not conn.execute("SELECT 1 FROM user_energy LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO user_energy VALUES (?, ?)",
                [(h, 3) for h in range(24)]
            )

        conn.commit()


# ---------------- SECURITY ----------------
def hash_password(password):
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode(),
        salt.encode(),
        600000
    )
    return f"{salt}${key.hex()}"


def check_password(stored, password):
    try:
        salt, key = stored.split("$")
        new_key = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode(),
            salt.encode(),
            600000
        )
        return secrets.compare_digest(key, new_key.hex())
    except:
        return False


# ---------------- AUTH ----------------
def is_logged_in():
    return session.get("user_id") is not None


# ---------------- SCHEDULER (SAFE VERSION) ----------------
def run_scheduler(user_id):
    db = get_db()

    tasks = db.execute(
        "SELECT * FROM tasks WHERE user_id=? AND is_completed=0",
        (user_id,)
    ).fetchall()

    commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id=?",
        (user_id,)
    ).fetchall()

    timeline = []

    for t in tasks:
        timeline.append({
            "title": t["title"],
            "start": "09:00",
            "end": "10:00",
            "type": "task"
        })

    for c in commitments:
        timeline.append({
            "title": c["title"],
            "start": c["start_time"],
            "end": c["end_time"],
            "type": "commitment"
        })

    return timeline


# ---------------- ROUTES ----------------
@app.route("/")
def home():
    if not is_logged_in():
        return redirect(url_for("login"))

    user_id = session.get("user_id")

    db = get_db()
    profile = db.execute(
        "SELECT * FROM user_profile WHERE id=?",
        (user_id,)
    ).fetchone()

    timeline = run_scheduler(user_id)

    return render_template("index.html", timeline=timeline, profile=profile)


# ---------- LOGIN ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        db = get_db()
        user = db.execute(
            "SELECT * FROM user_profile WHERE email=?",
            (email,)
        ).fetchone()

        if user and check_password(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("home"))

        flash("Invalid login")

    return render_template("login.html")


# ---------- SIGNUP ----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        db = get_db()

        db.execute("""
        INSERT INTO user_profile (name, age, occupation, wake_time, email, password_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            request.form["name"],
            request.form["age"],
            request.form["occupation"],
            request.form["wake_time"],
            request.form["email"],
            hash_password(request.form["password"])
        ))

        db.commit()

        return redirect(url_for("login"))

    return render_template("signup.html")


# ---------- LOGOUT ----------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- TASKS ----------
@app.route("/tasks", methods=["GET", "POST"])
def tasks():
    if not is_logged_in():
        return redirect(url_for("login"))

    db = get_db()
    user_id = session["user_id"]

    if request.method == "POST":
        db.execute("""
        INSERT INTO tasks (user_id, title, priority, urgency, difficulty, duration)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            request.form["title"],
            request.form["priority"],
            request.form["urgency"],
            request.form["difficulty"],
            request.form["duration"]
        ))
        db.commit()
        return redirect(url_for("tasks"))

    data = db.execute(
        "SELECT * FROM tasks WHERE user_id=?",
        (user_id,)
    ).fetchall()

    return render_template("tasks.html", tasks=data)


# ---------- START APP ----------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)