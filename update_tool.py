import sqlite3
import sys

with open('/tmp/tool.py', 'r') as f:
    content = f.read()

conn = sqlite3.connect('/tmp/webui.db')
conn.execute("UPDATE tool SET content = ? WHERE id = 'comfyui_model_switcher'", (content,))
conn.commit()
conn.close()
