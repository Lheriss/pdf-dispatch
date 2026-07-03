#!/bin/bash
PUID=${PUID:-1026}
PGID=${PGID:-100}

echo "Démarrage avec UID=${PUID} GID=${PGID}"

# Étape 1 (root) : créer tous les dossiers nécessaires
mkdir -p /data /data/input /data/output /data/output/error /data/output/processed /data/output/no_code

# Étape 2 (root) : permissions sur les dossiers (lecture/exécution groupe, pas d'écriture pour "others")
chmod -R 775 /data

# Étape 3 (root) : ajuster UID/GID de nobody
groupmod -o -g "${PGID}" users  2>/dev/null || true
usermod  -o -u "${PUID}" nobody 2>/dev/null || true

# Étape 4 (root) : donner la propriété à l'utilisateur cible
chown -R nobody:users /data

echo "Lancement de l'application sous UID=${PUID} GID=${PGID}"
exec gosu nobody python /app/app.py
