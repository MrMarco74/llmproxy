with open("llmproxy.py", "r") as f:
    content = f.read()
new_content = content.replace('body = await request.json()', 'body = await request.json()\n    print(f"REQUEST BODY: {body}", flush=True)')
with open("llmproxy.py", "w") as f:
    f.write(new_content)
