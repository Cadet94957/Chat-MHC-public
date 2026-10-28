FROM python:3.12-slim

WORKDIR /srv/chatmhc

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY ingest ./ingest
COPY static ./static
# Bake the manual forms library into the image. This public copy ships no kb/
# snapshot or form PDFs (MHCO content is licensed), so the app boots on seed data.
COPY forms_library ./forms_library

# Run as an unprivileged user; the data dir is the only writable path needed.
RUN useradd --create-home --uid 10001 chatmhc \
    && mkdir -p /srv/chatmhc/data \
    && chown -R chatmhc:chatmhc /srv/chatmhc/data
USER chatmhc

ENV DATA_DIR=/srv/chatmhc/data
VOLUME /srv/chatmhc/data

EXPOSE 8000
# --proxy-headers trusts X-Forwarded-* from the fronting proxy (Render/nginx)
# so per-client rate limiting sees real client IPs. If you expose this port
# directly to the internet with no proxy, remove the two proxy flags.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
