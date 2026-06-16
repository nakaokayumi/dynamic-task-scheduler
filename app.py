import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(24))
DATABASE = "scheduler.db"

def get_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# --- CYBERSECURITY LAYER: PBKDF2-SHA256 PASSWORD HASHING ---
def hash_password(password: str) -> str:
    """Generates a secure PBKDF2-SHA256 password hash using a unique salt."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 600000)
    return f"pbkdf2:sha256:600000${salt}${key.hex()}"

def check_password(stored_hash: str, password: str) -> bool:
    """Verifies a password against the stored PBKDF2 hash."""
    try:
        parts = stored_hash.split('$')
        salt = parts[1]
        original_key = parts[2]
        new_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 600000)
        return secrets.compare_digest(original_key, new_key.hex())
    except Exception:
        return False

def init_db():
    """Initializes the database schema cleanly on app startup."""
    with get_db() as conn:
        # USER TABLE
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                age INTEGER,
                occupation TEXT,
                wake_time TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        ''')

        # RESET PATCH: Clean wipe to handle due_date and parent_id schemas perfectly
        conn.execute('DROP TABLE IF EXISTS tasks')

        # TASKS TABLE (Equipped with parent_id for dependencies)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER DEFAULT NULL,
                title TEXT NOT NULL,
                priority INTEGER CHECK(priority BETWEEN 1 AND 5),
                urgency INTEGER CHECK(urgency BETWEEN 1 AND 5),
                difficulty INTEGER CHECK(difficulty BETWEEN 1 AND 5),
                duration INTEGER NOT NULL,
                due_date TEXT,
                is_completed INTEGER DEFAULT 0,
                FOREIGN KEY(parent_id) REFERENCES tasks(id) ON DELETE SET NULL
            )
        ''')

        # COMMITMENTS TABLE
        conn.execute('''
            CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL
            )
        ''')

        # ENERGY TABLE
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_energy (
                hour INTEGER PRIMARY KEY,
                energy_level INTEGER CHECK(energy_level BETWEEN 1 AND 5)
            )
        ''')

        if not conn.execute("SELECT 1 FROM user_energy LIMIT 1").fetchone():
            default_profile = [
                (h, 5 if 8 <= h <= 12 else (2 if 13 <= h <= 16 else 3))
                for h in range(0, 24)
            ]
            conn.executemany("INSERT INTO user_energy (hour, energy_level) VALUES (?, ?)", default_profile)
        conn.commit()

def run_scheduling_engine():
    db = get_db()
    
    # ADVANCED FEATURE: Only pull tasks that either have no parent, or whose parent is already completed!
    raw_tasks = db.execute('''
        SELECT t.* FROM tasks t 
        WHERE t.is_completed = 0 
        AND (t.parent_id IS NULL OR t.parent_id IN (SELECT id FROM tasks WHERE is_completed = 1))
    ''').fetchall()
    
    tasks = [dict(t) for t in raw_tasks]
    commitments = db.execute("SELECT * FROM commitments ORDER BY start_time ASC").fetchall()
    
    raw_energy = db.execute("SELECT * FROM user_energy").fetchall()
    energy_map = {row['hour']: row['energy_level'] for row in raw_energy}
    
    profile = db.execute("SELECT * FROM user_profile LIMIT 1").fetchone()
    wake_hour = 6
    if profile and profile['wake_time']:
        try:
            wake_hour = int(profile['wake_time'].split(':')[0])
        except Exception:
            pass
        
    today = datetime.today().date()
    current_timeline = datetime.combine(today, datetime.min.time()) + timedelta(hours=wake_hour)
    end_of_day = datetime.combine(today, datetime.min.time()) + timedelta(hours=24)
    
    free_slots = []
    for comm in commitments:
        c_start = datetime.strptime(comm['start_time'], "%Y-%m-%dT%H:%M")
        c_end = datetime.strptime(comm['end_time'], "%Y-%m-%dT%H:%M")
        if c_start > current_timeline:
            free_slots.append({"start": current_timeline, "end": c_start})
        current_timeline = max(current_timeline, c_end)
    if current_timeline < end_of_day:
        free_slots.append({"start": current_timeline, "end": end_of_day})
        
    MIN_CHUNK = 30
    schedule_timeline = []
    
    for slot in free_slots:
        slot_start = slot["start"]
        slot_end = slot["end"]
        slot_capacity = int((slot_end - slot_start).total_seconds() / 60)
        
        while slot_capacity >= MIN_CHUNK and tasks:
            current_hour = slot_start.hour
            user_energy_input = energy_map.get(current_hour, 3)
            
            scored_tasks = []
            for t in tasks:
                days_left = 7
                if t['due_date']:
                    try:
                        due_dt = datetime.strptime(t['due_date'], "%Y-%m-%d").date()
                        days_left = (due_dt - today).days
                    except Exception:
                        pass

                if days_left <= 0: due_multiplier = 3.0
                elif days_left <= 1: due_multiplier = 2.0
                elif days_left <= 3: due_multiplier = 1.5
                elif days_left <= 7: due_multiplier = 1.2
                else: due_multiplier = 1.0

                p_global = (0.6 * t['priority']) + (0.4 * t['urgency'])
                dur_norm = t['duration'] / 480.0
                s_fit = (p_global * due_multiplier) - (0.1 * dur_norm)
                
                if t['difficulty'] > user_energy_input:
                    e_fit = 0
                else:
                    e_fit = 5 - (user_energy_input - t['difficulty'])
                    
                master_score = s_fit * e_fit
                if master_score > 0:
                    scored_tasks.append((master_score, t))
                    
            if not scored_tasks:
                slot_start += timedelta(minutes=30)
                slot_capacity -= 30
                continue
                
            scored_tasks.sort(key=lambda x: x[0], reverse=True)
            winner = scored_tasks[0][1]
            master_calculated_score = round(scored_tasks[0][0], 2)
            
            if winner['duration'] <= slot_capacity:
                schedule_timeline.append({
                    "title": winner['title'],
                    "start": slot_start.strftime("%H:%M"),
                    "end": (slot_start + timedelta(minutes=winner['duration'])).strftime("%H:%M"),
                    "type": "task",
                    "score": master_calculated_score
                })
                slot_start += timedelta(minutes=winner['duration'])
                slot_capacity -= winner['duration']
                tasks.remove(winner)
            else:
                allocated = slot_capacity
                schedule_timeline.append({
                    "title": f"{winner['title']} (Part 1)",
                    "start": slot_start.strftime("%H:%M"),
                    "end": (slot_start + timedelta(minutes=allocated)).strftime("%H:%M"),
                    "type": "task",
                    "score": master_calculated_score
                })
                winner['duration'] -= allocated
                winner['title'] = f"{winner['title']} (Part 2)"
                slot_capacity = 0
                
    for comm in commitments:
        c_start = datetime.strptime(comm['start_time'], "%Y-%m-%dT%H:%M")
        c_end = datetime.strptime(comm['end_time'], "%Y-%m-%dT%H:%M")
        schedule_timeline.append({
            "title": comm['title'],
            "start": c_start.strftime("%H:%M"),
            "end": c_end.strftime("%H:%M"),
            "type": "commitment",
            "score": "N/A"
        })
        
    schedule_timeline.sort(key=lambda x: x['start'])
    return schedule_timeline

def is_authenticated():
    return "user_id" in session

# --- ROUTING SYSTEM ---
@app.route('/')
def index():
    if not is_authenticated():
        return redirect(url_for('signup'))
    db = get_db()
    profile = db.execute("SELECT * FROM user_profile LIMIT 1").fetchone()
    timeline = run_scheduling_engine()
    return render_template('index.html', timeline=timeline, profile=profile)

# CALENDAR DASHBOARD ROUTE VIEW
@app.route('/calendar')
def calendar_view():
    if not is_authenticated():
        return redirect(url_for('login'))
    return render_template('calendar.html')

# --- API ENDPOINT FOR FULLCALENDAR.JS FEED ---
@app.route('/api/calendar-events')
def calendar_events():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    events = []
    
    # Fetch commitments
    commitments = db.execute("SELECT * FROM commitments").fetchall()
    for c in commitments:
        events.append({
            "title": f"🔒 {c['title']}",
            "start": c['start_time'],
            "end": c['end_time'],
            "backgroundColor": "#1e293b",
            "borderColor": "#334155"
        })
        
    # Fetch pending tasks mapped onto their targeted due dates
    tasks = db.execute("SELECT * FROM tasks WHERE is_completed = 0").fetchall()
    for t in tasks:
        if t['due_date']:
            events.append({
                "title": f"🎯 {t['title']}",
                "start": t['due_date'],
                "allDay": True,
                "backgroundColor": "#7c3aed",
                "borderColor": "#6d28d9"
            })
    return jsonify(events)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_authenticated(): return redirect(url_for('index'))
    if request.method == 'POST':
        db = get_db()
        user = db.execute("SELECT * FROM user_profile WHERE email = ?", (request.form['email'],)).fetchone()
        if user and check_password(user['password_hash'], request.form['password']):
            session['user_id'] = user['id']
            return redirect(url_for('index'))
        flash("Invalid credentials.", "danger")
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        db = get_db()
        if db.execute("SELECT 1 FROM user_profile WHERE email = ?", (request.form['email'],)).fetchone():
            flash("Email registered.", "danger")
            return redirect(url_for('signup'))
        hashed_pw = hash_password(request.form['password'])
        db.execute("INSERT INTO user_profile (name, age, occupation, wake_time, email, password_hash) VALUES (?, ?, ?, ?, ?, ?)",
                   (request.form['name'], request.form['age'], request.form['occupation'], request.form['wake_time'], request.form['email'], hashed_pw))
        db.commit()
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/tasks', methods=['GET', 'POST'])
def manage_tasks():
    if not is_authenticated(): return redirect(url_for('login'))
    db = get_db()
    if request.method == 'POST':
        parent = request.form.get('parent_id')
        parent_val = int(parent) if parent and parent.strip() else None
        db.execute(
            "INSERT INTO tasks (title, priority, urgency, difficulty, duration, due_date, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request.form['title'], request.form['priority'], request.form['urgency'], request.form['difficulty'], request.form['duration'], request.form['due_date'], parent_val)
        )
        db.commit()
        return redirect(url_for('manage_tasks'))
        
    # Grab all tasks so the user can select dependencies from the list
    all_tasks = db.execute("SELECT * FROM tasks WHERE is_completed = 0").fetchall()
    return render_template('tasks.html', tasks=all_tasks)

@app.route('/commitments', methods=['GET', 'POST'])
def manage_commitments():
    if not is_authenticated(): return redirect(url_for('login'))
    db = get_db()
    if request.method == 'POST':
        db.execute("INSERT INTO commitments (title, start_time, end_time) VALUES (?, ?, ?)",
                   (request.form['title'], request.form['start_time'], request.form['end_time']))
        db.commit()
        return redirect(url_for('manage_commitments'))
    all_comm = db.execute("SELECT * FROM commitments").fetchall()
    return render_template('commitments.html', commitments=all_comm)

@app.route('/energy', methods=['GET', 'POST'])
def manage_energy():
    if not is_authenticated(): return redirect(url_for('login'))
    db = get_db()
    if request.method == 'POST':
        for hour in range(0, 24):
            field_name = f"energy_{hour}"
            if field_name in request.form:
                db.execute("UPDATE user_energy SET energy_level = ? WHERE hour = ?", (request.form[field_name], hour))
        db.commit()
        return redirect(url_for('index'))
    energy_levels = db.execute("SELECT * FROM user_energy ORDER BY hour ASC").fetchall()
    return render_template('energy.html', energy_levels=energy_levels)

@app.route('/complete-task/<int:task_id>')
def complete_task(task_id):
    if not is_authenticated(): return redirect(url_for('login'))
    db = get_db()
    db.execute("UPDATE tasks SET is_completed = 1 WHERE id = ?", (task_id,))
    db.commit()
    return redirect(url_for('manage_tasks'))

@app.route('/clear-commitments')
def clear_commitments():
    if not is_authenticated(): return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM commitments")
    db.commit()
    return redirect(url_for('manage_commitments'))

init_db()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))