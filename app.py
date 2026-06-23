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
def log_event(user_id, action, details=None):
    """Utility to record application lifecycle security events."""
    try:
        # Changed 'db' to 'conn' to resolve the 'conn is not defined' reference error
        conn = get_db()
        
        # Extract IP addresses securely, accounting for potential reverse proxies
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        
        conn.execute(
            """
            INSERT INTO audit_logs (user_id, action, details, ip_address)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, action, details, ip_address)
        )
        conn.commit()
    except Exception as e:
        # Prevent database logging faults from crashing the main user action thread
        print(f"⚠️ Audit Log Failure: {e}")

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
        # User Profiles table
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

        # Tasks table (Strictly tied to user_id)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                parent_id INTEGER DEFAULT NULL,
                title TEXT NOT NULL,
                priority INTEGER CHECK(priority BETWEEN 1 AND 5),
                urgency INTEGER CHECK(urgency BETWEEN 1 AND 5),
                difficulty INTEGER CHECK(difficulty BETWEEN 1 AND 5),
                duration INTEGER NOT NULL,
                preferred_period TEXT CHECK(preferred_period IN ('morning', 'afternoon', 'evening')) NOT NULL,
                due_date TEXT,
                is_completed INTEGER DEFAULT 0,
                FOREIGN KEY(parent_id) REFERENCES tasks(id) ON DELETE SET NULL,
                FOREIGN KEY(user_id) REFERENCES user_profile(id) ON DELETE CASCADE
            )
        ''')

        # Commitments table (Strictly tied to user_id, includes travel_time)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                travel_time INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES user_profile(id) ON DELETE CASCADE
            )
        ''')

        # User Routines table (Strictly tied to user_id)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_routines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                duration INTEGER NOT NULL,
                preferred_period TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user_profile(id) ON DELETE CASCADE
            )
        ''')

        # User Energy Profiles table (Tied to user_id)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_energy (
                user_id INTEGER,
                hour INTEGER,
                energy_level INTEGER CHECK(energy_level BETWEEN 1 AND 5),
                PRIMARY KEY (user_id, hour),
                FOREIGN KEY(user_id) REFERENCES user_profile(id) ON DELETE CASCADE
            )
        ''')

        # 🪵 Audit Logs table (Strictly tied to user_id via Foreign Key)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                ip_address TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES user_profile(id) ON DELETE SET NULL
            )
        ''')
        
        conn.commit()

# --- HELPER TO INITIALIZE ENERGY FOR NEW USERS ---
def seed_default_energy(user_id):
    with get_db() as conn:
        # Only seed if no energy mapping exists for this user
        if not conn.execute("SELECT 1 FROM user_energy WHERE user_id = ? LIMIT 1", (user_id,)).fetchone():
            default_profile = [
                (user_id, h, 5 if 8 <= h <= 12 else (2 if 13 <= h <= 16 else 3))
                for h in range(0, 24)
            ]
            conn.executemany("INSERT INTO user_energy (user_id, hour, energy_level) VALUES (?, ?, ?)", default_profile)
            conn.commit()

def run_scheduling_engine(user_id):
    db = get_db()
    
    schedule_timeline = []
    free_slots = []
    
    # 🛠️ ISOLATION FIX: Query tasks specific to this logged-in user
    raw_tasks = db.execute(
        """
        SELECT id, parent_id, title, priority, urgency, difficulty, duration, preferred_period, due_date, is_completed
        FROM tasks
        WHERE is_completed = 0 AND user_id = ?
        """, (user_id,)
    ).fetchall()
        
    tasks = [dict(t) for t in raw_tasks]
    
    # 🛠️ ISOLATION FIX: Query commitments specific to this user
    commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id = ? ORDER BY start_time ASC", (user_id,)
    ).fetchall()
    
    # 🛠️ ISOLATION FIX: Query energy maps specific to this user
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
    
    if not tasks and not commitments:
        return []
        
    for comm in commitments:
        try:
            c_start = datetime.strptime(comm['start_time'], "%Y-%m-%dT%H:%M")
            c_end = datetime.strptime(comm['end_time'], "%Y-%m-%dT%H:%M")
            
            t_buffer = comm['travel_time'] if 'travel_time' in comm.keys() else 0
            
            arrival_buffer_start = c_start - timedelta(minutes=t_buffer)
            departure_buffer_end = c_end + timedelta(minutes=t_buffer)

            if arrival_buffer_start > current_timeline:
                free_slots.append({"start": current_timeline, "end": arrival_buffer_start})
                
            current_timeline = max(current_timeline, departure_buffer_end)
        except Exception:
            pass

    if current_timeline < end_of_day:
        free_slots.append({"start": current_timeline, "end": end_of_day})
        
    # =======================================================================
    # STEP 2: Automated Routine Injection Layer
    # =======================================================================
    period_bounds = {
        'morning': (wake_hour, 12),
        'afternoon': (12, 17),
        'evening': (17, 23)
    }

    adjusted_free_slots = []
    
    for slot in free_slots:
        slot_start = slot["start"]
        slot_end = slot["end"]
        
        for t in tasks[:]:
            if t.get('preferred_period') and ('Routine' in t['title'] or '🧼' in t['title'] or '🍳' in t['title']):
                start_h, end_h = period_bounds.get(t['preferred_period'], (wake_hour, 12))
                
                if start_h <= slot_start.hour < end_h:
                    slot_capacity = int((slot_end - slot_start).total_seconds() / 60)
                    
                    if slot_capacity >= t['duration']:
                        schedule_timeline.append({
                            "title": t['title'],
                            "start": slot_start.strftime("%H:%M"),
                            "end": (slot_start + timedelta(minutes=t['duration'])).strftime("%H:%M"),
                            "type": "routine",
                            "score": "Fixed Routine"
                        })
                        slot_start += timedelta(minutes=t['duration'])
                        tasks.remove(t)
                        
        if slot_start < slot_end:
            adjusted_free_slots.append({"start": slot_start, "end": slot_end})
            
    free_slots = adjusted_free_slots

    MIN_CHUNK = 30
        
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
    
    profile = db.execute("SELECT * FROM user_profile WHERE id = ?", (current_user,)).fetchone()
    if not profile:
        session.clear()
        return redirect(url_for('login'))
        
    # Active tasks displayed on the dashboard grid (incomplete only)
    all_tasks = db.execute(
        "SELECT * FROM tasks WHERE is_completed = 0 AND user_id = ?", (current_user,)
    ).fetchall()
    
    # 📊 CALCULATE COMPLETION RATE (Executed before returning the template)
    stats = db.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN is_completed = 1 THEN 1 ELSE 0 END) as completed
        FROM tasks 
        WHERE user_id = ?
    """, (current_user,)).fetchone()

    total_tasks = stats['total'] or 0
    completed_tasks = stats['completed'] or 0

    if total_tasks > 0:
        completion_rate = round((completed_tasks / total_tasks) * 100, 1)
    else:
        completion_rate = 0.0

    # Run the automated scheduler timeline matrix
    timeline = run_scheduling_engine(current_user)
    
    # ✅ Packaged completely into the response payload context
    return render_template(
        'index.html', 
        timeline=timeline, 
        profile=profile, 
        tasks=all_tasks, 
        completion_rate=completion_rate
    )

# 🛠️ ROUTE CONFLICT RESOLVED: Consolidated everything into one secure endpoint
@app.route('/tasks', methods=['GET', 'POST'])
def manage_tasks():
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    db = get_db()
    current_user = session['user_id']
    
    if request.method == 'POST':
        parent = request.form.get('parent_id')
        parent_val = int(parent) if parent and parent.strip() else None
        
        title = request.form['title']
        priority = int(request.form['priority'])
        urgency = int(request.form['urgency'])
        difficulty = int(request.form['difficulty'])
        duration = int(request.form['duration'])
        due_date = request.form.get('due_date') or None
        
        preferred_period = request.form.get('preferred_period') or "morning"

        db.execute(
            """
            INSERT INTO tasks (user_id, parent_id, title, priority, urgency, difficulty, duration, preferred_period, due_date, is_completed) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (current_user, parent_val, title, priority, urgency, difficulty, duration, preferred_period, due_date)
        )
        db.commit()
        return redirect(url_for('manage_tasks'))
        
    all_tasks = db.execute("SELECT * FROM tasks WHERE is_completed = 0 AND user_id = ? ORDER BY id DESC", (current_user,)).fetchall()
    return render_template('tasks.html', tasks=all_tasks)

@app.route('/calendar')
def calendar_view():
    if not is_authenticated():
        return redirect(url_for('login'))
    return render_template('calendar.html')

@app.route('/routines', methods=['GET', 'POST'])
def manage_routines():
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    db = get_db()
    current_user = session['user_id']
    
    if request.method == 'POST':
        title = request.form['title']
        duration = int(request.form['duration'])
        preferred_period = request.form['preferred_period']
        
        db.execute(
            "INSERT INTO user_routines (user_id, title, duration, preferred_period) VALUES (?, ?, ?, ?)",
            (current_user, title, duration, preferred_period)
        )
        db.commit()
        return redirect(url_for('manage_routines'))
        
    routines = db.execute("SELECT * FROM user_routines WHERE user_id = ?", (current_user,)).fetchall()
    return render_template('routines.html', routines=routines)

@app.route('/routines/delete/<int:routine_id>', methods=['POST'])
def delete_routine(routine_id):
    if not is_authenticated(): 
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM user_routines WHERE id = ? AND user_id = ?", (routine_id, session['user_id']))
    db.commit()
    return redirect(url_for('manage_routines'))

@app.route('/api/calendar-events')
def calendar_events():
    if not is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    db = get_db()
    current_user = session['user_id']
    events = []
    
    commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id = ? ORDER BY start_time ASC", (current_user,)
    ).fetchall()
    for c in commitments:
        events.append({
            "title": f"🔒 {c['title']}",
            "start": c['start_time'],
            "end": c['end_time'],
            "backgroundColor": "#1e293b",
            "borderColor": "#334155"
        })
        
    tasks = db.execute("SELECT * FROM tasks WHERE is_completed = 0 AND user_id = ?", (current_user,)).fetchall()
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
            
            # ✅ SUCCESS LOG INJECTION
            log_event(user['id'], "LOGIN_SUCCESS", "User authenticated successfully.")
            
            return redirect(url_for('index'))
            
        # ❌ FAILURE LOG INJECTION (Pass None since user authentication failed)
        log_event(None, "LOGIN_FAILED", f"Failed attempt for email: {request.form['email']}")
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
        
        seed_default_energy(user_id)
        session['user_id'] = user_id
        
        # ✅ ACCOUNT CREATION LOG INJECTION
        log_event(user_id, "ACCOUNT_CREATION", "New user registered and default energy maps seeded.")
        
        return redirect(url_for('index'))
    return render_template('signup.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if not is_authenticated():
        return redirect(url_for('login'))
        
    db = get_db()
    current_user = session['user_id']
    
    # ... keep your existing profile POST/UPDATE code here ...

    profile_data = db.execute("SELECT * FROM user_profile WHERE id = ?", (current_user,)).fetchone()
    
    # 🔎 Fetch the 5 most recent activities for this specific user
    user_logs = db.execute(
        "SELECT action, details, ip_address, timestamp FROM audit_logs WHERE user_id = ? ORDER BY id DESC LIMIT 5", 
        (current_user,)
    ).fetchall()
        
    return render_template('profile.html', profile=profile_data, logs=user_logs)

@app.route('/tasks/delete/<int:task_id>', methods=['POST'])
def delete_task(task_id):
    if not is_authenticated():
        return redirect(url_for('login'))
    
    db = get_db()
    current_user = session['user_id']
    
    db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, current_user))
    db.commit()
    
    # ✅ METRIC DELETION LOG INJECTION
    log_event(current_user, "TASK_DELETION", f"Task ID {task_id} permanently dropped from database tracking.")
    
    return redirect(url_for('manage_tasks'))

@app.route('/commitments/delete/<int:commitment_id>', methods=['POST'])
def delete_commitment(commitment_id):
    if not is_authenticated():
        return redirect(url_for('login'))
    
    db = get_db()
    db.execute("DELETE FROM commitments WHERE id = ? AND user_id = ?", (commitment_id, session['user_id']))
    db.commit()
    return redirect(url_for('manage_commitments'))

@app.route('/commitments', methods=['GET', 'POST'])
def manage_commitments():
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    db = get_db()
    current_user = session['user_id']
    
    if request.method == 'POST':
        title = request.form['title']
        start_str = request.form['start_time']
        end_str = request.form['end_time']
        travel_time = request.form.get('travel_time', 0)
        travel_time = int(travel_time) if travel_time else 0
        
        db.execute(
            """
            INSERT INTO commitments (user_id, title, start_time, end_time, travel_time) 
            VALUES (?, ?, ?, ?, ?)
            """,
            (current_user, title, start_str, end_str, travel_time)
        )
        db.commit()
        return redirect(url_for('manage_commitments'))
        
    commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id = ? ORDER BY start_time ASC", (current_user,)
    ).fetchall()
    return render_template('commitments.html', commitments=commitments)
        
@app.route('/energy', methods=['GET', 'POST'])
def manage_energy():
    if not is_authenticated(): return redirect(url_for('login'))
    db = get_db()
    current_user = session['user_id']
    
    if request.method == 'POST':
        for hour in range(0, 24):
            field_name = f"energy_{hour}"
            if field_name in request.form:
                db.execute(
                    """
                    UPDATE user_energy SET energy_level = ? 
                    WHERE hour = ? AND user_id = ?
                    """, 
                    (request.form[field_name], hour, current_user)
                )
        db.commit()
        return redirect(url_for('index'))
        
    energy_levels = db.execute("SELECT * FROM user_energy WHERE user_id = ? ORDER BY hour ASC", (current_user,)).fetchall()
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
    db.execute("DELETE FROM commitments WHERE user_id = ?", (session['user_id'],))
    db.commit()
    return redirect(url_for('index'))

init_db()


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))