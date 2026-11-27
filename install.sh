#!/bin/bash
# =============================================================================
# Installation de l'application "Tenue de compte bancaire" sur un VPS
# Debian/Ubuntu (Infomaniak VPS Lite convient).
#
# Ce que fait ce script :
#   1. installe python3, nginx, openssl, apache2-utils (htpasswd)
#   2. copie tout dans /opt/compte_bancaire (script, base, journal, .bak)
#   3. cree un service systemd (demarrage auto au reboot, relance si crash)
#   4. configure nginx : HTTPS + Basic Auth + blocage de /api/arret
#   5. genere un certificat : Let's Encrypt si un domaine est fourni,
#      sinon certificat auto-signe (avertissement navigateur a accepter 1 fois)
#
# Usage :  sudo bash install.sh  [domaine.optionnel.tld]
# =============================================================================
set -euo pipefail

APPLI_DIR=/opt/compte_bancaire
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="${1:-}"
NGINX_SSL=/etc/nginx/ssl

[ "$(id -u)" -eq 0 ] || { echo "A lancer en root : sudo bash install.sh"; exit 1; }
[ -f "$SRC_DIR/compte_bancaire.py" ] || { echo "compte_bancaire.py introuvable a cote de install.sh"; exit 1; }

echo "== 1/6  Paquets =="
apt-get update -qq
apt-get install -y -qq python3 nginx openssl apache2-utils curl > /dev/null
if [ -n "$DOMAIN" ]; then
    apt-get install -y -qq certbot python3-certbot-nginx > /dev/null
fi

echo "== 2/6  Dossier $APPLI_DIR =="
mkdir -p "$APPLI_DIR"
cp "$SRC_DIR/compte_bancaire.py" "$APPLI_DIR/"
chown -R www-data:www-data "$APPLI_DIR"
chmod 750 "$APPLI_DIR"

echo "== 3/6  Service systemd =="
cp "$SRC_DIR/compte_bancaire.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now compte_bancaire.service
sleep 1
curl -fsS http://127.0.0.1:8000/ > /dev/null || { echo "!! l'application ne repond pas sur 127.0.0.1:8000"; exit 1; }
echo "   application demarree sur 127.0.0.1:8000 (invisible d'Internet)"

echo "== 4/6  Mot de passe (Basic Auth) =="
read -rp "Nom d'utilisateur : " UTILISATEUR
htpasswd -B -c /etc/nginx/.htpasswd_compte "$UTILISATEUR"
chmod 640 /etc/nginx/.htpasswd_compte

echo "== 5/6  nginx =="
mkdir -p "$NGINX_SSL"
cp "$SRC_DIR/nginx-compte_bancaire.conf" /etc/nginx/sites-available/compte_bancaire
ln -sf /etc/nginx/sites-available/compte_bancaire /etc/nginx/sites-enabled/compte_bancaire
rm -f /etc/nginx/sites-enabled/default

if [ -n "$DOMAIN" ]; then
    # --- HTTPS gratuit via Let's Encrypt : necessite que $DOMAIN pointe
    #     (enregistrement A) vers l'IP publique de ce VPS ---
    sed -i "s/server_name _;/server_name $DOMAIN;/" /etc/nginx/sites-available/compte_bancaire
    # certificat provisoire : certbot le remplace et recharge nginx
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "$NGINX_SSL/compte_bancaire.key" -out "$NGINX_SSL/compte_bancaire.crt" \
        -subj "/CN=$DOMAIN" > /dev/null 2>&1
    nginx -t
    systemctl reload nginx
    echo "   obtention du certificat Let's Encrypt pour $DOMAIN ..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" \
        || certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
else
    # --- pas de domaine : certificat auto-signe (validite 5 ans).
    #     Le navigateur affichera un avertissement a la premiere visite :
    #     cliquer "Avance" / "Continuer vers le site". ---
    echo "   pas de domaine fourni : certificat auto-signe"
    if [ ! -f "$NGINX_SSL/compte_bancaire.crt" ]; then
        openssl req -x509 -nodes -newkey rsa:2048 -days 1825 \
            -keyout "$NGINX_SSL/compte_bancaire.key" -out "$NGINX_SSL/compte_bancaire.crt" \
            -subj "/CN=compte_bancaire" > /dev/null 2>&1
    fi
    nginx -t
fi

echo "== 6/6  Redemarrage de nginx =="
systemctl enable --now nginx
systemctl restart nginx

echo
echo "============================================================"
echo " Installation terminee."
echo "   Application : https://$( [ -n "$DOMAIN" ] && echo "$DOMAIN" || echo '<IP-du-VPS>' )/"
echo "   Service      : systemctl status compte_bancaire"
echo "   Journal app  : $APPLI_DIR/compte_bancaire.log"
echo "   Donnees      : $APPLI_DIR/compte.db (+ .bak automatiques)"
echo " Pensez a ouvrir les ports 80 et 443 (firewall / manager Infomaniak)."
echo "============================================================"
