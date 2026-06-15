import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "secure_dynamic_scheduler_key"
DATABASE = "scheduler.db"

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Tasks Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER,
                title TEXT NOT NULL,
                priority INTEGER CHECK(priority BETWEEN 1 AND 5),
                urgency INTEGER CHECK(urgency BETWEEN 1 AND 5),
                difficulty INTEGER CHECK(difficulty BETWEEN 1 AND 5),
                duration INTEGER NOT NULL,
                is_completed INTEGER DEFAULT 0,
                FOREIGN KEY(parent_id) REFERENCES tasks(id)
            )''')
        
        # Fixed Commitments Blocker Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL
            )''')
        
        # User Energy Levels Matrix Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_energy (
                hour INTEGER PRIMARY KEY,
                energy_level INTEGER CHECK(energy_level BETWEEN 1 AND 5)
            )''')
        
        # User Profile Information Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                age INTEGER,
                occupation TEXT,
                wake_time TEXT NOT NULL
            )''')
        
        # Seed default energy profiles if completely empty
        if not conn.execute("SELECT 1 FROM user_energy").fetchone():
            default_profile = [(h, 5 if 8 <= h <= 12 else (2 if 13 <= h <= 16 else 3)) for h in range(0, 24)]
            conn.executemany("INSERT INTO user_energy (hour, energy_level) VALUES (?, ?)", default_profile)
        conn.commit()

def run_scheduling_engine():
    db = get_db()
    
    raw_tasks = db.execute("SELECT * FROM tasks WHERE is_completed = 0").fetchall()
    tasks = [dict(t) for t in raw_tasks]
    commitments = db.execute("SELECT * FROM commitments ORDER BY start_time ASC").fetchall()
    
    raw_energy = db.execute("SELECT * FROM user_energy").fetchall()
    energy_map = {row['hour']: row['energy_level'] for row in raw_energy}
    
    # Fetch user profile to extract dynamic wake time multiplier
    profile = db.execute("SELECT * FROM user_profile LIMIT 1").fetchone()
    if profile and profile['wake_time']:
        try:
            wake_hour = int(profile['wake_time'].split(':')[0])
        except Exception:
            wake_hour = 6 
    else:
        wake_hour = 6
        
    # Schedule Timeline Scope Boundary Configuration
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
                # Core Engine Logic Formulation
                p_global = (0.6 * t['priority']) + (0.4 * t['urgency'])
                dur_norm = t['duration'] / 480.0
                s_fit = p_global - (0.1 * dur_norm)
                
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

@app.route('/')
def index():
    db = get_db()
    profile = db.execute("SELECT * FROM user_profile LIMIT 1").fetchone()
    if not profile:
        return redirect(url_for('signup'))
        
    timeline = run_scheduling_engine()
    return render_template('index.html', timeline=timeline, profile=profile)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    db = get_db()
    if request.method == 'POST':
        db.execute("DELETE FROM user_profile")
        db.execute("INSERT INTO user_profile (name, age, occupation, wake_time) VALUES (?, ?, ?, ?)",
                   (request.form['name'], request.form['age'], request.form['occupation'], request.form['wake_time']))
        db.commit()
        return redirect(url_for('index'))
        
    profile = db.execute("SELECT * FROM user_profile LIMIT 1").fetchone()
    return render_template('signup.html', profile=profile)

@app.route('/tasks', methods=['GET', 'POST'])
def manage_tasks():
    db = get_db()
    if not db.execute("SELECT 1 FROM user_profile LIMIT 1").fetchone():
        return redirect(url_for('signup'))
        
    if request.method == 'POST':
        db.execute("INSERT INTO tasks (title, priority, urgency, difficulty, duration) VALUES (?, ?, ?, ?, ?)",
                   (request.form['title'], request.form['priority'], request.form['urgency'], request.form['difficulty'], request.form['duration']))
        db.commit()
        return redirect(url_for('manage_tasks'))
    all_tasks = db.execute("SELECT * FROM tasks WHERE is_completed = 0").fetchall()
    return render_template('tasks.html', tasks=all_tasks)

@app.route('/commitments', methods=['GET', 'POST'])
def manage_commitments():
    db = get_db()
    if not db.execute("SELECT 1 FROM user_profile LIMIT 1").fetchone():
        return redirect(url_for('signup'))
        
    if request.method == 'POST':
        db.execute("INSERT INTO commitments (title, start_time, end_time) VALUES (?, ?, ?)",
                   (request.form['title'], request.form['start_time'], request.form['end_time']))
        db.commit()
        return redirect(url_for('manage_commitments'))
    all_comm = db.execute("SELECT * FROM commitments").fetchall()
    return render_template('commitments.html', commitments=all_comm)

@app.route('/energy', methods=['GET', 'POST'])
def manage_energy():
    db = get_db()
    if not db.execute("SELECT 1 FROM user_profile LIMIT 1").fetchone():
        return redirect(url_for('signup'))
        
    if request.method == 'POST':
        for hour in range(0, 24):
            field_name = f"energy_{hour}"
            if field_name in request.form:
                level = request.form[field_name]
                db.execute("UPDATE user_energy SET energy_level = ? WHERE hour = ?", (level, hour))
        db.commit()
        return redirect(url_for('index'))
    energy_levels = db.execute("SELECT * FROM user_energy ORDER BY hour ASC").fetchall()
    return render_template('energy.html', energy_levels=energy_levels)

@app.route('/complete-task/<int:task_id>')
def complete_task(task_id):
    db = get_db()
    db.execute("UPDATE tasks SET is_completed = 1 WHERE id = ?", (task_id,))
    db.commit()
    return redirect(url_for('manage_tasks'))

@app.route('/clear-commitments')
def clear_commitments():
    db = get_db()
    db.execute("DELETE FROM commitments")
    db.commit()
    return redirect(url_for('manage_commitments'))

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)