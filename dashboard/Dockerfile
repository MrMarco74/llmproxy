FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "fastapi" "uvicorn[standard]" httpx "jinja2" python-multipart "starlette<1.0.0" itsdangerous

# Download JS/CSS libraries so the dashboard works fully offline
RUN apt-get update -qq && apt-get install -y -qq curl && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /app/static/vendor && \
    curl -fsSL https://cdn.tailwindcss.com/3.4.16 -o /app/static/vendor/tailwind.js && \
    curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js -o /app/static/vendor/chart.umd.min.js

COPY dashboard/app.py .
COPY dashboard/templates/ templates/
COPY assets/favicon.ico static/favicon.ico
COPY assets/logo.png    static/logo.png

# Bundle client-app install package
COPY monitor/llm_monitor.py       pkg/llm_monitor.py
COPY scripts/install_monitor.sh   pkg/install_monitor.sh
COPY scripts/uninstall_monitor.sh pkg/uninstall_monitor.sh
COPY monitor/llm_monitor.desktop  pkg/llm_monitor.desktop
COPY monitor/assets/              pkg/assets/
RUN cd /app/pkg && tar czf ../static/llm-monitor-setup.tar.gz . && cd /app && rm -rf pkg

ENV DB_PATH=/data/llmproxy.db
ENV LLMPROXY_STATUS_URL=http://host.docker.internal:11435

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
