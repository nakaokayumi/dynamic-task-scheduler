import sqlite3
from datetime import datetime
from app import run_scheduling_engine, hash_password

def setup_split_test():
    db_file = "scheduler.db"
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    print("🧹 Cleaning up old test data...")
    cursor.execute("DELETE FROM user_profile WHERE email = 'tester@split.com'")
    conn.commit()

    # 1. Create a mock test user
    print("👤 Creating test user profile...")
    pw_hash = hash_password("testpass123")
    cursor.execute("""
        INSERT INTO user_profile (name, age, occupation, wake_time, email, password_hash)
        VALUES ('Test User', 25, 'QA', '06:00', 'tester@split.com', ?)
    """, (pw_hash,))
    user_id = cursor.lastrowid
    
    # Initialize default energy for this test user
    default_profile = [
        (user_id, h, 5 if 8 <= h <= 12 else (2 if 13 <= h <= 16 else 3))
        for h in range(0, 24)
    ]
    cursor.executemany("INSERT INTO user_energy (user_id, hour, energy_level) VALUES (?, ?, ?)", default_profile)

    # 2. Add a Commitment that blocks the afternoon, creating a tight morning slot
    # Wake time is 06:00. Let's block everything from 08:00 onwards, leaving exactly a 2-hour gap (06:00 to 08:00)
    today_str = datetime.today().strftime("%Y-%m-%d")
    print(f"🔒 Inserting a bottleneck commitment for date: {today_str}")
    
    cursor.execute("""
        INSERT INTO commitments (user_id, title, start_time, end_time, travel_time)
        VALUES (?, 'Blocking Meeting', ?, ?, 0)
    """, (user_id, f"{today_str}T08:00", f"{today_str}T10:00"))

    # 3. Insert a giant 180-minute (3 hour) task. 
    # It cannot fit completely into the 2-hour morning window (06:00 - 08:00)!
    print("🎯 Inserting a 180-minute high-priority task...")
    cursor.execute("""
        INSERT INTO tasks (user_id, parent_id, title, priority, urgency, difficulty, duration, preferred_period, due_date, is_completed)
        VALUES (?, NULL, 'Massive Security Audit', 5, 5, 3, 180, 'morning', NULL, 0)
    """, (user_id,))
    
    conn.commit()
    conn.close()
    return user_id

def verify_results(user_id):
    print("\n⚙️ Running scheduling engine calculations...")
    timeline = run_scheduling_engine(user_id)
    
    print("\n--- 📅 GENERATED TIMELINE RESULTS ---")
    split_detected = False
    part1_found = False
    part2_found = False

    for item in timeline:
        print(f"[{item['start']} - {item['end']}] Type: {item['type'].upper()} | Title: {item['title']}")
        
        if "(Part 1)" in item['title']:
            part1_found = True
        if "(Part 2)" in item['title']:
            part2_found = True

    print("---------------------------------------")
    if part1_found and part2_found:
        print("✅ TEST PASSED: The engine successfully identified the bottleneck and split the task into sequential chunks!")
    else:
        print("❌ TEST FAILED: The task was either omitted, skipped entirely, or did not split gracefully.")

if __name__ == "__main__":
    test_user_id = setup_split_test()
    verify_results(test_user_id)