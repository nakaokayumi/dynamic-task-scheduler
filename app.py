import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(24))
DATABASE = "scheduler.db" 

def get_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# --- PBKDF2-SHA256 SECURE SECURITY LAYER ---
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 600000)
    return f"pbkdf2:sha256:600000${salt}${key.hex()}"

def check_password(stored_hash: str, password: str) -> bool:
    try:
        parts = stored_hash.split('$')
        salt = parts[1]
        original_key = parts[2]
        new_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 600000)
        return secrets.compare_digest(original_key, new_key.hex())
    except Exception:
        return False

def init_db():
    with get_db() as conn:
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

        conn.execute('''
            CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL
            )
        ''')

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

def run_scheduling_engine(user_id):
    db = get_db()
    
    # 🔒 PRIVACY FIX 1: Isolate tasks by user_id and verify structural prerequisites
    raw_tasks = db.execute('''
        SELECT t.* FROM tasks t 
        WHERE t.user_id = ? 
        AND t.is_completed = 0 
        AND (t.parent_id IS NULL OR t.parent_id IN (SELECT id FROM tasks WHERE is_completed = 1 AND user_id = ?))
    ''', (user_id, user_id)).fetchall()
    
    tasks = [dict(t) for t in raw_tasks]
    
    # 🔒 PRIVACY FIX 2: Isolate commitments by user_id
    commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id = ? ORDER BY start_time ASC", 
        (user_id,)
    ).fetchall()
    
    # 🔒 PRIVACY FIX 3: Isolate energy metrics by user_id (assuming user_id column exists here)
    # If your user_energy table does not have a user_id column yet, use: "SELECT * FROM user_energy"
    raw_energy = db.execute("SELECT * FROM user_energy WHERE user_id = ?", (user_id,)).fetchall()
    energy_map = {row['hour']: row['energy_level'] for row in raw_energy}
    
    profile = db.execute("SELECT * FROM user_profile WHERE id = ?", (user_id,)).fetchone()
    wake_hour = 6
    if profile and profile['wake_time']:
        try:
            wake_hour = int(profile['wake_time'].split(':')[0])
        except Exception:
            pass
        
    today = datetime.today().date()
    current_timeline = datetime.combine(today, datetime.min.time()) + timedelta(hours=wake_hour)
    end_of_day = datetime.combine(today, datetime.min.time()) + timedelta(hours=24)
    
    # 🛡️ SAFETY GUARD RAIL: Exit early if database tables are empty to avoid loops crashing
    if not tasks and not commitments:
        return []
        
    free_slots = []
    for comm in commitments:
        try:
            c_start = datetime.strptime(comm['start_time'], "%Y-%m-%dT%H:%M")
            c_end = datetime.strptime(comm['end_time'], "%Y-%m-%dT%H:%M")
            if c_start > current_timeline:
                free_slots.append({"start": current_timeline, "end": c_start})
            current_timeline = max(current_timeline, c_end)
        except Exception:
            pass

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
        try:
            c_start = datetime.strptime(comm['start_time'], "%Y-%m-%dT%H:%M")
            c_end = datetime.strptime(comm['end_time'], "%Y-%m-%dT%H:%M")
            schedule_timeline.append({
                "title": comm['title'],
                "start": c_start.strftime("%H:%M"),
                "end": c_end.strftime("%H:%M"),
                "type": "commitment",
                "score": "N/A"
            })
        except Exception:
            pass
        
    schedule_timeline.sort(key=lambda x: x['start'])
    return schedule_timeline

def is_authenticated():
    return "user_id" in session

# --- ROUTING PATTERNS ---
@app.route('/')
def index():
    if not is_authenticated():
        return redirect(url_for('login'))
    db = get_db()
    current_user = session['user_id']
    profile = db.execute("SELECT * FROM user_profile WHERE id = ?", (session['user_id'],)).fetchone()
    
    if not profile:
        session.clear()
        return redirect(url_for('login'))
        
    timeline = run_scheduling_engine(session['user_id'])
    all_tasks = db.execute(
        "SELECT * FROM tasks WHERE is_completed = 0 AND user_id = ?", 
        (current_user,)
    ).fetchall()
    
    return render_template('scheduler.html', timeline=timeline, profile=profile, tasks=all_tasks)

@app.route('/calendar')
def calendar_view():
    if not is_authenticated():
        return redirect(url_for('login'))
    return render_template('calendar.html')

@app.route('/api/calendar-events')
def calendar_events():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    events = []
    
    commitments = db.execute("SELECT * FROM commitments").fetchall()
    for c in commitments:
        events.append({
            "title": f"🔒 {c['title']}",
            "start": c['start_time'],
            "end": c['end_time'],
            "backgroundColor": "#1e293b",
            "borderColor": "#334155"
        })
        
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
    if is_authenticated(): return redirect(url_for('index'))
    if request.method == 'POST':
        db = get_db()
        if db.execute("SELECT 1 FROM user_profile WHERE email = ?", (request.form['email'],)).fetchone():
            flash("Email registered.", "danger")
            return redirect(url_for('signup'))
        hashed_pw = hash_password(request.form['password'])
        
        cursor = db.cursor()
        cursor.execute("INSERT INTO user_profile (name, age, occupation, wake_time, email, password_hash) VALUES (?, ?, ?, ?, ?, ?)",
                       (request.form['name'], request.form['age'], request.form['occupation'], request.form['wake_time'], request.form['email'], hashed_pw))
        user_id = cursor.lastrowid
        db.commit()
        
        session['user_id'] = user_id
        return redirect(url_for('index'))
    return render_template('signup.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/profile')
def profile():
    if not is_authenticated():
        return redirect(url_for('login'))
        
    db = get_db()
    current_user = session['user_id']
    
    # Fetch the logged-in user's profile details from the database
    profile_data = db.execute("SELECT * FROM user_profile WHERE id = ?", (current_user,)).fetchone()
    
    # If no profile record exists yet, create an empty dictionary structure so the HTML doesn't crash
    if not profile_data:
        profile_data = {'name': 'Authenticated Profile', 'email': ''}
        
    return render_template('profile.html', profile=profile_data)

# ==========================================
# TASK ACTIONS: UPDATE, DELETE, COMPLETE
# ==========================================

@app.route('/tasks/delete/<int:task_id>', methods=['POST'])
def delete_task(task_id):
    if not is_authenticated():
        return redirect(url_for('login'))
    
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, session['user_id']))
    db.commit()
    return redirect(url_for('manage_tasks'))


# ==========================================
# COMMITMENT ACTIONS: DELETE/UPDATE
# ==========================================

@app.route('/commitments/delete/<int:commitment_id>', methods=['POST'])
def delete_commitment(commitment_id):
    if not is_authenticated():
        return redirect(url_for('login'))
    
    db = get_db()
    # Cascading cleanup: If deleting a main event, also drop its child travel block
    db.execute(
        """
        DELETE FROM commitments 
        WHERE (id = ? OR parent_commitment_id = ?) AND user_id = ?
        """, 
        (commitment_id, commitment_id, session['user_id'])
    )
    db.commit()
    return redirect(url_for('manage_commitments'))

@app.route('/tasks', methods=['GET', 'POST'])
def manage_tasks():
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    db = get_db()
    
    if request.method == 'POST':
        parent = request.form.get('parent_id')
        parent_val = int(parent) if parent and parent.strip() else None
        
        # 1. Grab the active user's session identifier
        current_user = session.get('user_id')
        
        # 2. Inject user_id into the constraint tracking schema layout
        db.execute(
            """
            INSERT INTO tasks (user_id, parent_id, title, priority, urgency, difficulty, duration, due_date) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_user, 
                parent_val, 
                request.form['title'], 
                int(request.form['priority']), 
                int(request.form['urgency']), 
                int(request.form['difficulty']), 
                int(request.form['duration']), 
                request.form['due_date']
            )
        )
        db.commit()
        return redirect(url_for('manage_tasks'))
        
    # GET Request logic remains clean
    all_tasks = db.execute("SELECT * FROM tasks WHERE is_completed = 0 AND user_id = ? ORDER BY id DESC", (session['user_id'],)).fetchall()
    return render_template('tasks.html', tasks=all_tasks)

@app.route('/commitments', methods=['GET', 'POST'])
def manage_commitments():
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    db = get_db()
    current_user = session.get('user_id')
    
    if request.method == 'POST':
        title = request.form['title']
        start_str = request.form['start_time']
        end_str = request.form['end_time']
        travel_duration = int(request.form.get('travel_time', 0)) # in minutes
        
        # 1. Insert the main event commitment block
        cursor = db.execute(
            """
            INSERT INTO commitments (user_id, title, start_time, end_time, is_travel_block) 
            VALUES (?, ?, ?, ?, 0)
            """,
            (current_user, title, start_str, end_str)
        )
        main_event_id = cursor.lastrowid
        
        # 2. Automatically account for travel time if requested
        if travel_duration > 0:
            event_start = datetime.strptime(start_str, "%Y-%m-%dT%H:%M")
            
            # Calculate commute window right before the event starts
            travel_start = event_start - timedelta(minutes=travel_duration)
            travel_end = event_start
            
            db.execute(
                """
                INSERT INTO commitments (user_id, title, start_time, end_time, parent_commitment_id, is_travel_block) 
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    current_user,
                    f"🚗 Commute: {title}",
                    travel_start.strftime("%Y-%m-%dT%H:%M"),
                    travel_end.strftime("%Y-%m-%dT%H:%M"),
                    main_event_id
                )
            )
            
        db.commit()
        return redirect(url_for('manage_commitments'))
        
    # GET Request logic remains exactly the same
    commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id = ? ORDER BY start_time ASC", 
        (current_user,)
    ).fetchall()
    
    return render_template('commitments.html', commitments=commitments)
        
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

@app.route('/tasks/complete/<int:task_id>', methods=['POST'])
def complete_task(task_id):
    if not is_authenticated():
        return redirect(url_for('login'))
    
    db = get_db()
    db.execute("UPDATE tasks SET is_completed = 1 WHERE id = ? AND user_id = ?", (task_id, session['user_id']))
    db.commit()
    return redirect(url_for('manage_tasks'))

@app.route('/clear-commitments')
def clear_commitments():
    if not is_authenticated(): return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM commitments")
    db.commit()
    return redirect(url_for('index'))

init_db()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))