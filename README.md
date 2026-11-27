# Tenue de compte bancaire — déploiement VPS (option B)

Application accessible depuis n'importe quel navigateur (PC, smartphone,
tablette) via HTTPS, protégée par un Basic Auth, avec :

- **tout regroupé dans un seul dossier** `/opt/compte_bancaire` : script,
  base SQLite (`compte.db`), journal, et les 10 sauvegardes `.bak` ;
- **démarrage automatique au reboot** et relance en cas de crash (systemd) ;
- **arrêt depuis le navigateur désactivé** (bouton ⏻ retiré, route
  `/api/arret` bloquée dans l'app **et** dans nginx) ;
- **affichage mobile amélioré** (balise viewport ajoutée).

## Contenu du paquet

| Fichier | Rôle |
|---|---|
| `compte_bancaire.py` | l'application, adaptée au serveur |
| `compte_bancaire.service` | service systemd |
| `nginx-compte_bancaire.conf` | reverse proxy HTTPS + Basic Auth |
| `install.sh` | installation automatisée |

## Installation sur le VPS

```bash
# 1. envoyer le paquet sur le VPS
scp -r compte_bancaire/ root@IP-DU-VPS:/root/

# 2. sur le VPS
ssh root@IP-DU-VPS
apt install -y python3        # si absent
bash /root/compte_bancaire/install.sh
```

Le script demande un nom d'utilisateur et un mot de passe (ceux que vous
taperez dans la fenêtre de connexion du navigateur).

### HTTPS : deux cas

- **Vous possédez un domaine** pointant (enregistrement A) vers l'IP du VPS :
  `bash install.sh votre-domaine.tld` → certificat Let's Encrypt gratuit,
  renouvelé automatiquement, aucun avertissement navigateur.
- **IP seule** : certificat auto-signé. Le navigateur affichera un
  avertissement à la première visite (par machine/outil) : cliquer
  « Avancé → Continuer ». C'est le seul inconvénient ; le chiffrement est
  réel. *NB : Let's Encrypt ne délivre pas de certificats pour une IP simple.*

### Ports

Ouvrir les ports **80 et 443** (pare-feu local et/ou manager Infomaniak).
Le port 8000 reste interne : l'application Python n'est jamais exposée.

## Utilisation

Ouvrir `https://IP-DU-VPS/` (ou le domaine) depuis n'importe quel appareil,
saisir le couple login/mot de passe, et l'application s'affiche.

## Commandes utiles (sur le VPS)

```bash
systemctl status compte_bancaire      # état du service
systemctl restart compte_bancaire     # redémarrer
journalctl -u compte_bancaire -f      # logs systemd
tail -f /opt/compte_bancaire/compte_bancaire.log   # journal applicatif
htpasswd -B /etc/nginx/.htpasswd_compte utilisateur  # changer le mot de passe
```

## Modifications apportées au script d'origine

1. `<meta name="viewport">` ajoutée (rendu correct sur smartphone) ;
2. bouton ⏻ Quitter retiré de l'interface ;
3. `/api/arret` répond désormais `403` systématiquement (impossible
   d'arrêter l'application depuis un navigateur) ;
4. arrêt propre uniquement par `systemctl stop/restart` (la sauvegarde
   `VACUUM INTO` du démarrage couvre l'intégrité des données).

## Sauvegardes

Le script conserve automatiquement 10 copies `compte.db.*.bak` dans
`/opt/compte_bancaire`. Recommandé : copie hebdomadaire hors du VPS :

```bash
scp root@IP-DU-VPS:/opt/compte_bancaire/compte.db.$(date +%F).bak ~/sauvegardes/
```

## Remarque sur l'IP fournie

L'IP `200.234.71.74` semble hors des plages habituellement attribuées par
Infomaniak (plages suisses). Vérifiez l'IP publique exacte dans le manager
Infomaniak avant l'installation (elle est nécessaire pour le DNS et
l'accès).
