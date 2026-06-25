import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
def is_authenticated():
    return 'user_id' in session

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
        # 1. User Profiles table (wake_time completely removed from here)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                age INTEGER,
                occupation TEXT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        ''')

        # 2. NEW: Daily Variable Wake Times table (1 entry per day of week per user)
        # 2. NEW: Daily Variable Wake Times table (1 entry per day of week per user)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_wake_times (
                user_id INTEGER NOT NULL,
                day_of_week TEXT NOT NULL, -- 'Monday', 'Tuesday', etc.
                wake_time TEXT NOT NULL DEFAULT '06:00',
                PRIMARY KEY (user_id, day_of_week), -- Explicit composite primary key
                FOREIGN KEY (user_id) REFERENCES user_profile (id) ON DELETE CASCADE
            )
        ''')

        # 3. Tasks table (Strictly tied to user_id)
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

        # 4. Commitments table (Updated with recurrence fields: Type, Interval, and End Conditions)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                travel_time INTEGER DEFAULT 0,
                commitment_date TEXT NOT NULL,        -- Used as starting date for recurring events
                recurrence_type TEXT NOT NULL DEFAULT 'none', -- 'none', 'daily', 'weekly', 'monthly', 'custom'
                recurrence_interval INTEGER DEFAULT NULL,     -- Used if 'custom' (e.g., every X days)
                ends_type TEXT DEFAULT 'never',               -- 'never', 'date', 'occurrences'
                ends_date TEXT DEFAULT NULL,                  -- 'YYYY-MM-DD'
                ends_occurrences INTEGER DEFAULT NULL,        -- Counter value
                FOREIGN KEY(user_id) REFERENCES user_profile(id) ON DELETE CASCADE
            )
        ''')

        # 5. User Routines table (Strictly tied to user_id)
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

        # 6. User Energy Profiles table (Tied to user_id)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_energy (
                user_id INTEGER,
                hour INTEGER,
                energy_level INTEGER CHECK(energy_level BETWEEN 1 AND 5),
                PRIMARY KEY (user_id, hour),
                FOREIGN KEY(user_id) REFERENCES user_profile(id) ON DELETE CASCADE
            )
        ''')

        # 7. 🪵 Audit Logs table (Strictly tied to user_id via Foreign Key)
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
    
    # 1. Query tasks specific to this logged-in user
    raw_tasks = db.execute(
        """
        SELECT id, parent_id, title, priority, urgency, difficulty, duration, preferred_period, due_date, is_completed
        FROM tasks
        WHERE is_completed = 0 AND user_id = ?
        """, (user_id,)
    ).fetchall()
    tasks = [dict(t) for t in raw_tasks]
    
    # 2. Query commitments specific to this user (Converted to clean dicts)
    raw_commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id = ? ORDER BY start_time ASC", (user_id,)
    ).fetchall()
    commitments = [dict(c) for c in raw_commitments]
    
    # 3. Query standalone user routines specific to this user
    raw_routines = db.execute(
        "SELECT title, duration, preferred_period FROM user_routines WHERE user_id = ?", (user_id,)
    ).fetchall()
    routines = [dict(r) for r in raw_routines]
    
    # 4. Query energy maps specific to this user
    raw_energy = db.execute("SELECT * FROM user_energy WHERE user_id = ?", (user_id,)).fetchall()
    energy_map = {row['hour']: row['energy_level'] for row in raw_energy}
    
    # Fetch today's wake configuration bounds
    todays_wake = get_todays_wake_time(user_id)
    try:
        wake_hour = int(todays_wake.split(':')[0])
        wake_minute = int(todays_wake.split(':')[1])
    except (ValueError, IndexError, AttributeError):
        wake_hour = 6
        wake_minute = 0
        
    today = datetime.today().date()
    current_timeline = datetime.combine(today, datetime.min.time()) + timedelta(hours=wake_hour, minutes=wake_minute)
    end_of_day = datetime.combine(today, datetime.min.time()) + timedelta(hours=24)

    # 🚨 TEMPORARY DEBUG LOGS
    print(f"--- DEBUG ENGINE LOADING ---")
    print(f"Tasks found: {len(tasks)}")
    print(f"Commitments found: {len(commitments)}")
    print(f"Routines found: {len(routines)}")

    if not tasks and not commitments and not routines:
        return []
        
    # Helper utility to handle variable date strings coming from database frames cleanly
    def parse_datetime_flexible(dt_str, default_time_str="00:00"):
        if not dt_str:
            return None
        if len(dt_str) == 5 and ":" in dt_str:
            today_str = datetime.today().strftime("%Y-%m-%d")
            return datetime.strptime(f"{today_str} {dt_str}", "%Y-%m-%d %H:%M")
        if "T" in dt_str:
            return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")
        if len(dt_str) > 10:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        return datetime.strptime(f"{dt_str} {default_time_str}", "%Y-%m-%d %H:%M")

    # =======================================================================
    # STEP 1.5: Immediate Wake-Up Routine Placement
    # =======================================================================
    for r in routines[:]:
        if r['preferred_period'].lower() == 'morning':
            schedule_timeline.append({
                "title": r['title'],
                "start": current_timeline.strftime("%H:%M"),
                "end": (current_timeline + timedelta(minutes=r['duration'])).strftime("%H:%M"),
                "type": "routine",
                "score": "Fixed Routine"
            })
            current_timeline += timedelta(minutes=r['duration'])
            routines.remove(r)

    # =======================================================================
    # STEP 1: Map Fixed Commitment Boundaries & Remaining Free Slots
    # =======================================================================
    for comm in commitments:
        try:
            c_start = parse_datetime_flexible(comm['start_time'], "09:00")
            c_end = parse_datetime_flexible(comm['end_time'], "17:00")
            
            if not c_start or not c_end:
                continue
                
            # Safely check dict keys for travel time
            t_buffer = comm.get('travel_time', 0) or 0
            arrival_buffer_start = c_start - timedelta(minutes=t_buffer)
            departure_buffer_end = c_end + timedelta(minutes=t_buffer)

            if arrival_buffer_start > current_timeline:
                free_slots.append({"start": current_timeline, "end": arrival_buffer_start})
                
            current_timeline = max(current_timeline, departure_buffer_end)
        except Exception as e:
            print(f"Commitment mapping exception skipped: {e}")

    if current_timeline < end_of_day:
        free_slots.append({"start": current_timeline, "end": end_of_day})
        
    # =======================================================================
    # STEP 2: Non-Morning Routine Allocation Layer
    # =======================================================================
    period_bounds = {
        'afternoon': (12, 17),
        'evening': (17, 23)
    }

    adjusted_free_slots = []
    for slot in free_slots:
        slot_start = slot["start"]
        slot_end = slot["end"]
        
        for r in routines[:]:
            start_h, end_h = period_bounds.get(r['preferred_period'].lower(), (12, 17))
            if start_h <= slot_start.hour < end_h:
                slot_capacity = int((slot_end - slot_start).total_seconds() / 60)
                if slot_capacity >= r['duration']:
                    schedule_timeline.append({
                        "title": r['title'],
                        "start": slot_start.strftime("%H:%M"),
                        "end": (slot_start + timedelta(minutes=r['duration'])).strftime("%H:%M"),
                        "type": "routine",
                        "score": "Fixed Routine"
                    })
                    slot_start += timedelta(minutes=r['duration'])
                    routines.remove(r)
                    
        if slot_start < slot_end:
            adjusted_free_slots.append({"start": slot_start, "end": slot_end})
            
    free_slots = adjusted_free_slots

    # =======================================================================
    # STEP 3: Scored Fluid Task Allocation Matrix Layer
    # =======================================================================
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
                        due_dt = datetime.strptime(t['due_date'].split()[0], "%Y-%m-%d").date()
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

    # =======================================================================
    # STEP 4: Fixed Commitments Assembly Layer
    # =======================================================================
    for comm in commitments:
        try:
            c_start = parse_datetime_flexible(comm['start_time'], "09:00")
            c_end = parse_datetime_flexible(comm['end_time'], "17:00")
            if c_start and c_end:
                t_buffer = comm.get('travel_time', 0) or 0
                visual_start = c_start - timedelta(minutes=t_buffer)
                visual_end = c_end + timedelta(minutes=t_buffer)
                
                display_title = comm['title']
                if t_buffer > 0:
                    display_title += f" (Inc. {t_buffer}m Travel Time)"

                schedule_timeline.append({
                    "title": display_title,
                    "start": visual_start.strftime("%H:%M"),
                    "end": visual_end.strftime("%H:%M"),
                    "type": "commitment",
                    "score": "N/A"
                })
        except Exception as e:
            print(f"Error drawing commitment to timeline array: {e}")
            
    # Sort everything chronologically across all layers
    schedule_timeline.sort(key=lambda x: x['start'])
    return schedule_timeline

def get_todays_wake_time(user_id):
    """Helper to fetch the specific wake time for whatever day today is."""
    # Get today's name (e.g., 'Monday', 'Tuesday')
    today_name = datetime.now().strftime('%A')
    
    db = get_db()
    row = db.execute(
        "SELECT wake_time FROM user_wake_times WHERE user_id = ? AND day_of_week = ?", 
        (user_id, today_name)
    ).fetchone()
    
    # Fallback to standard 06:00 if not configured yet
    return row['wake_time'] if row else "06:00"
# --- ROUTING PATTERNS ---
@app.route('/')
def index():
    # 🌟 REPLACE THE NAMERROR LINE WITH THIS DIRECT SESSION CHECK:
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
        
    db = get_db()
    current_user = session['user_id']
    # ... rest of your index code remains exactly the same
    
    profile = db.execute("SELECT * FROM user_profile WHERE id = ?", (current_user,)).fetchone()
    if not profile:
        session.clear()
        return redirect(url_for('login'))
        
    # 🔗 LINK STEP 2 HERE: Convert the row to a dictionary and inject today's wake time
    profile_dict = dict(profile)
    profile_dict['wake_time'] = get_todays_wake_time(current_user)
        
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
    
    # ✅ Pass the updated profile_dict (instead of the old profile object) to the template
    return render_template(
        'index.html', 
        timeline=timeline, 
        profile=profile_dict, 
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

@app.route('/profile', methods=['GET', 'POST'])
def manage_settings():
    if not is_authenticated():
        return redirect(url_for('login'))
        
    current_user = session['user_id']
    db = get_db()
    days_of_week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    
    if request.method == 'POST':
        print("--- POST REQUEST RECEIVED ---")
        print("Form Data:", request.form)
        # 1. Grab personal details from form
        name = request.form.get('name')
        age = request.form.get('age')
        email = request.form.get('email')
        occupation = request.form.get('occupation')
        
        # 2. Update the user_profile table
        db.execute("""
            UPDATE user_profile 
            SET name = ?, age = ?, email = ?, occupation = ? 
            WHERE id = ?
        """, (name, age, email, occupation, current_user))
        
        # 3. Process and update weekly wake times
        for day in days_of_week:
            form_name = f"wake_{day.lower()}"
            wake_time = request.form.get(form_name, '06:00')
            
            db.execute("""
                INSERT INTO user_wake_times (user_id, day_of_week, wake_time)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, day_of_week) 
                DO UPDATE SET wake_time = excluded.wake_time
            """, (current_user, day, wake_time))
            
        db.commit()
        flash("Settings updated successfully!", "success")
        return redirect(url_for('manage_settings'))

    # --- GET REQUEST: Load existing data to display ---
    profile = db.execute("SELECT * FROM user_profile WHERE id = ?", (current_user,)).fetchone()
    
    wake_rows = db.execute(
        "SELECT day_of_week, wake_time FROM user_wake_times WHERE user_id = ?", 
        (current_user,)
    ).fetchall()
    wake_dict = {row['day_of_week']: row['wake_time'] for row in wake_rows}
    
    return render_template('settings.html', profile=profile, wake_dict=wake_dict)

@app.route('/settings/wake-times', methods=['GET', 'POST'])
def manage_wake_times():
    if not is_authenticated():
        return redirect(url_for('login'))
        
    current_user = session['user_id']
    db = get_db()
    days_of_week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    
    if request.method == 'POST':
        for day in days_of_week:
            # Look for form names like 'wake_monday', 'wake_tuesday', etc.
            form_name = f"wake_{day.lower()}"
            wake_time = request.form.get(form_name, '06:00') # Fallback to 06:00 if missing
            
            # Upsert logic: Update if exists, insert if it doesn't
            db.execute("""
                INSERT INTO user_wake_times (user_id, day_of_week, wake_time)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, day_of_week) 
                DO UPDATE SET wake_time = excluded.wake_time
            """, (current_user, day, wake_time))
            
        db.commit()
        flash("Wake times updated successfully!", "success")
        return redirect(url_for('manage_wake_times'))

    # --- GET REQUEST: Fetch data to display inside the HTML form inputs ---
    rows = db.execute(
        "SELECT day_of_week, wake_time FROM user_wake_times WHERE user_id = ?", 
        (current_user,)
    ).fetchall()
    
    # Map database records into a clean dictionary: {'Monday': '07:00', 'Tuesday': '06:30'}
    wake_dict = {row['day_of_week']: row['wake_time'] for row in rows}
    
    # Pass wake_dict directly to your template!
    return render_template('wake_settings.html', wake_dict=wake_dict)

@app.route('/calendar')
def calendar_view():
    if not is_authenticated():
        return redirect(url_for('login'))
    return render_template('calendar.html')


@app.route('/routines/delete/<int:routine_id>', methods=['POST'])
def delete_routine(routine_id):
    # 1. Secure authentication check
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    user_id = session.get('user_id')
    
    # 2. Safe context-managed database transaction
    with get_db() as conn:
        # Secure delete: ensure the routine strictly belongs to this specific logged-in user
        conn.execute(
            "DELETE FROM user_routines WHERE id = ? AND user_id = ?", 
            (routine_id, user_id)
        )
        conn.commit()

    flash("Routine deleted successfully!", "success")
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
        cursor.execute(
            "INSERT INTO user_profile (name, age, occupation, email, password_hash) VALUES (?, ?, ?, ?, ?)",
            (request.form['name'], request.form['age'], request.form['occupation'], request.form['email'], hashed_pw)
        )
        user_id = cursor.lastrowid
        db.commit()
        
        seed_default_energy(user_id)
        session['user_id'] = user_id
        
        # ✅ ACCOUNT CREATION LOG INJECTION
        log_event(user_id, "ACCOUNT_CREATION", "New user registered and default energy maps seeded.")
        
        return redirect(url_for('index'))
    return render_template('signup.html')

@app.route('/routines', methods=['GET', 'POST'])
def manage_routines():
    # 1. Ensure user is authenticated safely
    user_id = session.get('user_id')
    if not user_id or not is_authenticated():
        return redirect(url_for('login'))

    db = get_db()

    # 2. Handle adding a new routine via POST
    if request.method == 'POST':
        title = request.form.get('title')
        duration = request.form.get('duration')
        preferred_period = request.form.get('preferred_period')

        if title and duration and preferred_period:
            db.execute(
                "INSERT INTO user_routines (user_id, title, duration, preferred_period) VALUES (?, ?, ?, ?)",
                (user_id, title, int(duration), preferred_period)
            )
            db.commit()
            flash("Routine added successfully!", "success")
        return redirect(url_for('manage_routines'))

    # 3. Handle viewing the page via GET (Fetches user-specific routines)
    routines = db.execute(
        "SELECT * FROM user_routines WHERE user_id = ?", (user_id,)
    ).fetchall()
    
    return render_template('routines.html', routines=routines)

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
        start_str = request.form['start_time']  # Usually just 'HH:MM'
        end_str = request.form['end_time']      # Usually just 'HH:MM'
        
        # Pull standard travel time
        travel_time = request.form.get('travel_time', 0)
        travel_time = int(travel_time) if travel_time else 0
        
        # New Recurrence Parameters from form inputs
        commitment_date = request.form.get('commitment_date') # 'YYYY-MM-DD'
        recurrence_type = request.form.get('recurrence_type', 'none')
        recurrence_interval = request.form.get('recurrence_interval')
        ends_type = request.form.get('ends_type', 'never')
        ends_date = request.form.get('ends_date')
        ends_occurrences = request.form.get('ends_occurrences')

        # Clean integer strings and empty HTML form strings to None/Integer safely
        interval_val = int(recurrence_interval) if (recurrence_interval and recurrence_type == 'custom') else None
        occurrences_val = int(ends_occurrences) if (ends_occurrences and ends_type == 'occurrences') else None
        if not ends_date or ends_type != 'date': 
            ends_date = None

        db.execute(
            """
            INSERT INTO commitments (
                user_id, title, start_time, end_time, travel_time, 
                commitment_date, recurrence_type, recurrence_interval, ends_type, ends_date, ends_occurrences
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_user, title, start_str, end_str, travel_time,
                commitment_date, recurrence_type, interval_val, ends_type, ends_date, occurrences_val
            )
        )
        db.commit()
        return redirect(url_for('manage_commitments'))
        
    commitments = db.execute(
        "SELECT * FROM commitments WHERE user_id = ? ORDER BY commitment_date ASC, start_time ASC", 
        (current_user,)
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