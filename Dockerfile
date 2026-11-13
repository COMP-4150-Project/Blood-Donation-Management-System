# Self-contained Vytal demo: PostgreSQL 16 + Flask GUI in one container. Listens on $PORT (default 7860).
FROM postgres:16-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 vytal
USER vytal
WORKDIR /home/vytal/app
COPY --chown=vytal requirements.txt .
RUN python3 -m venv /home/vytal/venv && /home/vytal/venv/bin/pip install --no-cache-dir -r requirements.txt gunicorn
COPY --chown=vytal . .
ENV PYTHON=/home/vytal/venv/bin/python PORT=7860
EXPOSE 7860
ENTRYPOINT ["./docker/entrypoint.sh"]
