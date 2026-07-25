with open("llmproxy.py", "r") as f:
    content = f.read()
new_content = content.replace('async with _client.stream("POST", f"{upstream_url}{path}", json=body) as upstream_conn:', 'async with _client.stream("POST", f"{upstream_url}{path}", json=body) as upstream_conn:\n                print(f"UPSTREAM STATUS: {upstream_conn.status_code}", flush=True)\n                if upstream_conn.status_code != 200:\n                    err = await upstream_conn.aread()\n                    print(f"UPSTREAM ERROR: {err}", flush=True)')
with open("llmproxy.py", "w") as f:
    f.write(new_content)
