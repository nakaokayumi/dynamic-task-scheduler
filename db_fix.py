import sqlite3

try:
    # Connect directly to your existing database file
    conn = sqlite3.connect('scheduler.db')
    cursor = conn.cursor()
    
    # Force add the missing column to your existing table layout
    cursor.execute("ALTER TABLE commitments ADD COLUMN travel_time INTEGER DEFAULT 0;")
    
    conn.commit()
    print("🚀 Success! The travel_time column has been injected into your existing database structure.")
    
except sqlite3.OperationalError as e:
    if "duplicate column name" in str(e):
        print("✅ The column travel_time already exists in your table layout!")
    else:
        print(f"❌ Operational Error: {e}")
except Exception as e:
    print(f"❌ Unexpected Error: {e}")
finally:
    conn.close()