import sqlite3
import os

db_path = 'data/portfolio.db'
if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
c = conn.cursor()

print("--- Tables ---")
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = c.fetchall()
print(tables)

for table in tables:
    t_name = table[0]
    print(f"\n--- Columns in {t_name} ---")
    c.execute(f"PRAGMA table_info({t_name})")
    print(c.fetchall())
    
    print(f"\n--- Sample data from {t_name} ---")
    c.execute(f"SELECT * FROM {t_name} LIMIT 5")
    print(c.fetchall())

conn.close()
