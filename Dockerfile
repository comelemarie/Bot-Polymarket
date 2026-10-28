FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Les secrets (.env) et l'etat local sont exclus par .dockerignore: ils ne doivent jamais etre dans l'image.
COPY . .

# Utilisateur non root
RUN useradd --create-home --uid 1000 bot && mkdir -p state logs data && chown -R bot:bot /app
USER bot

ENV PYTHONUNBUFFERED=1

# Base SQLite (positions, ordres, budget Claude), logs et enregistrements: a monter en volumes,
# sinon tout est perdu quand le conteneur est recree.
VOLUME ["/app/state", "/app/logs", "/app/data"]

CMD ["python", "-u", "main.py"]
