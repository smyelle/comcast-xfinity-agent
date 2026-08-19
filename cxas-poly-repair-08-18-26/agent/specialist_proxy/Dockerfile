FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Run as a non-root user. Nothing here needs root: the service binds $PORT (8080, not
# privileged), writes nothing to disk, and its job store is Firestore. Without this the
# container runs as UID 0, which is what the repository scan flags -- a compromise of the
# app would then start with everything needed to write over the image and reach the node.
#
# NOT `proxy`: python:3.12-slim already ships a system account by that name, and
# `useradd` exits 9 rather than doing anything, which fails the build.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER 10001

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
