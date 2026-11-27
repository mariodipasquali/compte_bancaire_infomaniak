#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tenue de compte bancaire — web app locale (aucune dépendance).
Usage :  python3 compte_bancaire.py  puis ouvrir http://localhost:8000
         (sous Windows : py compte_bancaire.py ou python compte_bancaire.py)
Options : python3 compte_bancaire.py [port] [chemin_bdd]
Arrêt  : Ctrl+C, ou bouton ⏻ Quitter dans l'interface
Base par défaut : ~/pCloudDrive/compte_bancaire/compte.db (le dossier est
créé s'il n'existe pas ; pCloud Drive doit être monté).

Version corrigée :
- montants stockés en centimes (INTEGER) au lieu de REAL (float) ;
- réponse HTTP vidée avant l'arrêt brutal (os._exit) ;
- import calendar déplacé en tête de fichier ;
- noms de catégories/types vides refusés côté serveur ;
- jokers SQL (% et _) échappés dans la recherche ;
- PRAGMA foreign_keys activé sur chaque connexion.
- journal d'activité copié dans ~/pCloudDrive/compte_bancaire/compte_bancaire.log
  (la sortie console reste affichée normalement).

Nouveautés de cette version :
- sauvegarde automatique de la base (VACUUM INTO) au démarrage et à
  l'arrêt, les 10 dernières copies .bak sont conservées — indispensable
  quand la base vit dans un dossier synchronisé pCloud ;
- arrêt propre via le bouton ⏻ : shutdown du serveur + sauvegarde avant
  os._exit (le jeton anti-CSRF protège /api/arret) ;
- validation réelle des dates (date.fromisoformat : 2026-02-30 refusé) ;
- port validé au démarrage (message clair si invalide) ;
- schéma/migrations exécutés une seule fois au démarrage (init_db) au
  lieu de refaire tout le travail à chaque requête API ;
- journal_mode=DELETE forcé (le mode WAL est dangereux avec la synchro
  cloud : fichiers annexes non synchronisés).
- serveur multi-thread (ThreadingHTTPServer) : l'interface ne se fige
  plus pendant un export CSV ;
- pagination des transactions : 150 lignes par page, bouton
  « Afficher plus » (l'export CSV et les soldes restent globaux) ;
- suppression d'une catégorie : transactions ET échéances réaffectées à
  « Non catégorisé » (auparavant, supprimer une catégorie utilisée par
  une échéance échouait sur la clé étrangère) ;
- tiers obligatoire pour les échéances, comme pour les transactions ;
- « catégorie libre » créée dans la même transaction SQL que la ligne :
  plus de catégorie orpheline si la saisie échoue ensuite.
- cadre Catégories & types repensé : deux colonnes, compteur
  d'utilisation (transactions + échéances) par élément, renommage (✎,
  nouvelles actions renommer_categorie / renommer_type) et suppression
  avec confirmation contextuelle.
- interface modernisée : en-tête sticky avec soldes colorés (vert/rouge
  selon le signe), champs avec anneau de focus visible, filtres en
  segmented control, badges « pilule » pour catégories et types, dates
  affichées au format français, messages d'erreur en toast (plus
  d'alert() bloquant), états vides explicites dans les tableaux.
La migration des anciennes bases est automatique (colonne montant_cents
remplie depuis l'ancienne colonne montant).
"""
import calendar
import csv
import glob
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import webbrowser
from datetime import date
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qsl

# Sous Windows, la sortie redirigée est en cp1252 : les emoji du journal
# lèveraient UnicodeEncodeError. On remplace silencieusement les caractères
# non représentables au lieu de perdre les lignes de log.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

try:
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
except ValueError:
    sys.exit(f"✖ Port invalide : {sys.argv[1]!r} (exemples corrects : 8000, 8080)")
# Sous Linux/macOS, pCloud Drive est monté dans ~/pCloudDrive ; sous Windows,
# pCloud crée un dossier « pCloud Drive » dans le profil utilisateur (ou une
# lettre de lecteur). Le chemin reste surchargeable : variable d'environnement
# COMPTE_BANCAIRE_DIR, ou 2e argument de la ligne de commande.
if os.name == "nt":
    DOSSIER_BDD = os.environ.get("COMPTE_BANCAIRE_DIR") or os.path.expanduser(
        r"~\pCloud Drive\compte_bancaire")
else:
    DOSSIER_BDD = os.path.expanduser("~/pCloudDrive/compte_bancaire")
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else os.path.join(DOSSIER_BDD, "compte.db")
FICHIER_LOG = os.path.join(DOSSIER_BDD, "compte_bancaire.log")
TOKEN = secrets.token_hex(16)   # jeton anti-CSRF pour /api/arret

class Tee:
    """Duplique la sortie console vers le terminal ET le fichier de log."""
    def __init__(self, fichier):
        self.fichier = fichier
    def write(self, s):
        try:
            sys.__stdout__.write(s)
        except Exception:
            pass
        try:
            self.fichier.write(s)
        except Exception:
            # log momentanément indisponible (synchro pCloud, disque plein…) :
            # on continue en console plutôt que de faire planter les requêtes
            pass
    def flush(self):
        try:
            sys.__stdout__.flush()
        except Exception:
            pass
        try:
            self.fichier.flush()
        except Exception:
            pass

# ---------------------------------------------------------------- base de données
SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nom TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS types_paiement (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nom TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,              -- YYYY-MM-DD
  tiers TEXT,                      -- tiers / contrepartie (EDF, Carrefour…)
  libelle TEXT NOT NULL,
  montant REAL NOT NULL,           -- colonne historique (hors usage)
  montant_cents INTEGER NOT NULL,  -- montant en centimes : -4520 = -45,20 €
  categorie_id INTEGER REFERENCES categories(id),
  type_paiement_id INTEGER REFERENCES types_paiement(id),
  pointe INTEGER NOT NULL DEFAULT 0,
  echeance_id INTEGER              -- si générée par une échéance
);
CREATE TABLE IF NOT EXISTS echeances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tiers TEXT,
  libelle TEXT NOT NULL,
  montant REAL NOT NULL,           -- colonne historique (hors usage)
  montant_cents INTEGER NOT NULL,
  jour INTEGER NOT NULL,           -- jour du mois (1-28, ou 99 = fin de mois)
  categorie_id INTEGER REFERENCES categories(id),
  type_paiement_id INTEGER REFERENCES types_paiement(id),
  actif INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS meta (
  cle TEXT PRIMARY KEY,
  valeur TEXT
);
"""

def init_db():
    """Crée/migre le schéma UNE SEULE FOIS au démarrage (pas à chaque requête)."""
    db = sqlite3.connect(DB_PATH)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        # DELETE (défaut) est le mode le plus sûr pour une base synchronisée
        # dans le cloud : un seul fichier, pas de WAL/-shm désynchronisés.
        db.execute("PRAGMA journal_mode=DELETE")
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO categories(nom) VALUES ('Non catégorisé')")
        for t in ("CB", "Chèque", "Espèces", "Virement", "Prélèvement", "PayPal", "Autre"):
            db.execute("INSERT OR IGNORE INTO types_paiement(nom) VALUES (?)", (t,))
        # migration : anciennes bases sans les colonnes récentes
        cols = [r[1] for r in db.execute("PRAGMA table_info(transactions)")]
        if "type_paiement_id" not in cols:
            db.execute("ALTER TABLE transactions ADD COLUMN type_paiement_id INTEGER")
        if "tiers" not in cols:
            db.execute("ALTER TABLE transactions ADD COLUMN tiers TEXT")
        if "montant_cents" not in cols:
            db.execute("ALTER TABLE transactions ADD COLUMN montant_cents INTEGER")
            db.execute("UPDATE transactions SET montant_cents=CAST(ROUND(montant*100) AS INTEGER)")
        cols = [r[1] for r in db.execute("PRAGMA table_info(echeances)")]
        if "type_paiement_id" not in cols:
            db.execute("ALTER TABLE echeances ADD COLUMN type_paiement_id INTEGER")
        if "tiers" not in cols:
            db.execute("ALTER TABLE echeances ADD COLUMN tiers TEXT")
        if "montant_cents" not in cols:
            db.execute("ALTER TABLE echeances ADD COLUMN montant_cents INTEGER")
            db.execute("UPDATE echeances SET montant_cents=CAST(ROUND(montant*100) AS INTEGER)")
        db.commit()
    finally:
        db.close()

def get_db():
    """Connexion légère utilisée par les requêtes API."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

def backup_db():
    """Sauvegarde physique cohérente de la base (VACUUM INTO).
    Conserve les 10 dernières copies .bak. Indispensable quand la base vit
    dans un dossier synchronisé : une synchro pendant une écriture peut
    corrompre le fichier distants — ici on part toujours d'une copie saine."""
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            dest = f"{DB_PATH}.{date.today().isoformat()}.bak"
            if not os.path.exists(dest):
                db.execute("VACUUM INTO ?", (dest,))
        finally:
            db.close()
        baks = sorted(glob.glob(DB_PATH + ".*.bak"))
        for old in baks[:-10]:
            try:
                os.remove(old)
            except OSError:
                pass
        print(f"💾 Sauvegarde : {baks[-1] if baks else dest}" if baks else f"💾 Sauvegarde : {dest}")
    except Exception as e:
        print(f"⚠ Sauvegarde impossible ({e}) — la base reste utilisable.")

def last_day(annee, mois):
    return calendar.monthrange(annee, mois)[1]

def jour_date(annee, mois, jour):
    if jour == 99:
        return date(annee, mois, last_day(annee, mois))
    return date(annee, mois, min(jour, last_day(annee, mois)))

def mois_courant():
    d = date.today()
    return f"{d.year:04d}-{d.month:02d}"

def mois_suivant(ym):
    a, m = map(int, ym.split("-"))
    return f"{a+1:04d}-01" if m == 12 else f"{a:04d}-{m+1:02d}"

def generer_echeances():
    """Reporte les échéances mensuelles vers les transactions, le 1er jour de
    chaque mois, un mois après l'autre. Le mois du dernier report est mémorisé
    dans la table meta ; si l'application n'a pas été ouverte pendant plusieurs
    mois, les mois manqués sont rattrapés l'un après l'autre."""
    db = get_db()
    cur = mois_courant()
    row = db.execute("SELECT valeur FROM meta WHERE cle='dernier_mois'").fetchone()
    if row is None:
        # première utilisation : on mémorise le mois courant sans générer de report
        db.execute("INSERT INTO meta(cle,valeur) VALUES ('dernier_mois',?)", (cur,))
        mois = cur
    else:
        mois = row["valeur"]
    while mois < cur:
        mois = mois_suivant(mois)
        annee, nmois = map(int, mois.split("-"))
        for e in db.execute("SELECT * FROM echeances WHERE actif=1"):
            d = jour_date(annee, nmois, e["jour"])
            existe = db.execute(
                "SELECT 1 FROM transactions WHERE echeance_id=? AND date=?",
                (e["id"], d.isoformat())).fetchone()
            if not existe:
                cents = e["montant_cents"]
                db.execute(
                    "INSERT INTO transactions(date, tiers, libelle, montant, montant_cents, categorie_id, type_paiement_id, pointe, echeance_id) "
                    "VALUES (?,?,?,?,?,?,?,0,?)",
                    (d.isoformat(), e["tiers"], e["libelle"], cents / 100.0, cents,
                     e["categorie_id"], e["type_paiement_id"], e["id"]))
        db.execute("UPDATE meta SET valeur=? WHERE cle='dernier_mois'", (mois,))
    db.commit()
    db.close()

# ---------------------------------------------------------------- API
def build_where(q):
    """Filtres communs : pointage + recherche plein texte (libellé, catégorie, type)."""
    conds, params = [], []
    if q.get("pointe") in ("0", "1"):
        conds.append("t.pointe=?")
        params.append(int(q["pointe"]))
    recherche = (q.get("q") or "").strip()
    if recherche:
        # échappement des jokers SQL pour une recherche littérale
        like = "%" + recherche.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append("(t.libelle LIKE ? ESCAPE '\\' OR t.tiers LIKE ? ESCAPE '\\' "
                     "OR c.nom LIKE ? ESCAPE '\\' OR p.nom LIKE ? ESCAPE '\\')")
        params += [like, like, like, like]
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where, params

LIMITE_PAGE = 150

def api_get(db, q):
    where, params = build_where(q)
    try:
        offset = max(0, int(q.get("offset") or 0))
    except ValueError:
        offset = 0
    nb_total = db.execute(
        "SELECT COUNT(*) FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.categorie_id "
        "LEFT JOIN types_paiement p ON p.id=t.type_paiement_id" + where,
        params).fetchone()[0]
    txs = db.execute(
        "SELECT t.*, c.nom AS categorie, p.nom AS type_paiement FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.categorie_id "
        "LEFT JOIN types_paiement p ON p.id=t.type_paiement_id" + where +
        " ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?",
        params + [LIMITE_PAGE, offset]).fetchall()
    cats = [dict(c) for c in db.execute("SELECT * FROM categories ORDER BY nom").fetchall()]
    types = [dict(t) for t in db.execute("SELECT * FROM types_paiement ORDER BY id").fetchall()]
    # nombre d'utilisations (transactions + échéances), pour l'UI de gestion
    def _compte(colonne):
        n = {}
        for tbl in ("transactions", "echeances"):
            for r in db.execute(f"SELECT {colonne} AS cid, COUNT(*) AS n FROM {tbl} "
                                f"WHERE {colonne} IS NOT NULL GROUP BY {colonne}"):
                n[r["cid"]] = n.get(r["cid"], 0) + r["n"]
        return n
    nbc, nbt = _compte("categorie_id"), _compte("type_paiement_id")
    for c in cats:
        c["nb"] = nbc.get(c["id"], 0)
    for t in types:
        t["nb"] = nbt.get(t["id"], 0)
    echs = db.execute(
        "SELECT e.*, c.nom AS categorie, p.nom AS type_paiement FROM echeances e "
        "LEFT JOIN categories c ON c.id=e.categorie_id "
        "LEFT JOIN types_paiement p ON p.id=e.type_paiement_id ORDER BY e.jour").fetchall()
    solde = db.execute("SELECT COALESCE(SUM(montant_cents),0) FROM transactions").fetchone()[0]
    solde_pointe = db.execute("SELECT COALESCE(SUM(montant_cents),0) FROM transactions WHERE pointe=1").fetchone()[0]
    total_ech = db.execute("SELECT COALESCE(SUM(montant_cents),0) FROM echeances WHERE actif=1").fetchone()[0]
    return {"transactions": [dict(t) for t in txs],
            "categories": cats,
            "types_paiement": types,
            "echeances": [dict(e) for e in echs],
            "solde": solde,
            "solde_pointe": solde_pointe,
            "total_echeances": total_ech,
            "nb_total": nb_total,
            "offset": offset,
            "limite": LIMITE_PAGE}

def val_montant(s):
    """Convertit un montant saisi en centimes (int), sans jamais passer par un float.
    Tous les types d'espaces sont retirés (séparateurs de milliers collés,
    espace insécable ou fine insécable copiés-collés depuis l'affichage)."""
    s = re.sub(r"[\s\u00a0\u202f]", "", str(s).strip().replace(",", "."))
    m = re.fullmatch(r"(-?)(\d+)(?:\.(\d{1,2}))?", s)
    if not m:
        raise ValueError("montant invalide")
    signe, ent, dec = m.group(1), m.group(2), m.group(3) or ""
    cents = int(ent) * 100 + int(dec.ljust(2, "0"))
    return -cents if signe else cents

def fmt_cents(c):
    """Formate des centimes en chaîne 'x xxx,xx' (sans float, séparateur de milliers)."""
    c = int(c)
    ent = f"{abs(c) // 100:,}".replace(",", " ")
    return ("-" if c < 0 else "") + ent + ",%02d" % (abs(c) % 100)

def val_date(s):
    """Refuse les dates au bon format mais inexistantes (2026-02-30…)."""
    try:
        date.fromisoformat(s)
    except (ValueError, TypeError):
        raise ValueError("date invalide")
    return s

def val_ref(body, key):
    v = body.get(key)
    if v in (None, "", 0, "0"):
        return None
    return int(v)

def api_post(db, action, body):
    with db:
        if action == "transaction":
            cents = val_montant(body["montant"])
            tiers = (body.get("tiers") or "").strip()
            if not tiers:
                raise ValueError("le tiers est obligatoire")
            cat_id = val_ref(body, "categorie_id")
            # catégorie libre : créée dans la MÊME transaction SQL (atomique),
            # pas de catégorie orpheline si la suite échoue
            nouvelle = (body.get("nouvelle_categorie") or "").strip()
            if nouvelle:
                db.execute("INSERT OR IGNORE INTO categories(nom) VALUES (?)", (nouvelle,))
                row = db.execute("SELECT id FROM categories WHERE nom=?", (nouvelle,)).fetchone()
                cat_id = row["id"] if row else cat_id
            db.execute("INSERT INTO transactions(date, tiers, libelle, montant, montant_cents, categorie_id, type_paiement_id) VALUES (?,?,?,?,?,?,?)",
                       (val_date(body["date"]), tiers, (body.get("libelle") or "").strip(),
                        cents / 100.0, cents,
                        cat_id, val_ref(body, "type_paiement_id")))
        elif action == "modification":
            cents = val_montant(body["montant"])
            tiers = (body.get("tiers") or "").strip()
            if not tiers:
                raise ValueError("le tiers est obligatoire")
            db.execute("UPDATE transactions SET date=?, tiers=?, libelle=?, montant=?, montant_cents=?, categorie_id=?, type_paiement_id=? WHERE id=?",
                       (val_date(body["date"]), tiers, (body.get("libelle") or "").strip(),
                        cents / 100.0, cents,
                        val_ref(body, "categorie_id"), val_ref(body, "type_paiement_id"), int(body["id"])))
        elif action == "pointage":
            db.execute("UPDATE transactions SET pointe=? WHERE id=?",
                       (1 if body.get("pointe") else 0, int(body["id"])))
        elif action == "suppression":
            db.execute("DELETE FROM transactions WHERE id=?", (int(body["id"]),))
        elif action == "categorie":
            nom = body["nom"].strip()
            if not nom:
                raise ValueError("le nom est obligatoire")
            db.execute("INSERT OR IGNORE INTO categories(nom) VALUES (?)", (nom,))
        elif action == "type":
            nom = body["nom"].strip()
            if not nom:
                raise ValueError("le nom est obligatoire")
            db.execute("INSERT OR IGNORE INTO types_paiement(nom) VALUES (?)", (nom,))
        elif action == "renommer_categorie":
            cid = int(body["id"])
            nom = (body.get("nom") or "").strip()
            if not nom:
                raise ValueError("le nom est obligatoire")
            try:
                db.execute("UPDATE categories SET nom=? WHERE id=? AND nom<>'Non catégorisé'", (nom, cid))
            except sqlite3.IntegrityError:
                raise ValueError("ce nom de catégorie existe déjà")
        elif action == "renommer_type":
            tid = int(body["id"])
            nom = (body.get("nom") or "").strip()
            if not nom:
                raise ValueError("le nom est obligatoire")
            try:
                db.execute("UPDATE types_paiement SET nom=? WHERE id=?", (nom, tid))
            except sqlite3.IntegrityError:
                raise ValueError("ce nom de type existe déjà")
        elif action == "suppr_type":
            tid = int(body["id"])
            db.execute("UPDATE transactions SET type_paiement_id=NULL WHERE type_paiement_id=?", (tid,))
            db.execute("UPDATE echeances SET type_paiement_id=NULL WHERE type_paiement_id=?", (tid,))
            db.execute("DELETE FROM types_paiement WHERE id=?", (tid,))
        elif action == "suppr_categorie":
            cid = int(body["id"])
            # réaffectation à « Non catégorisé » (transactions ET échéances :
            # sans cela, la clé étrangère bloquait la suppression d'une
            # catégorie utilisée par une échéance)
            ref = db.execute("SELECT id FROM categories WHERE nom='Non catégorisé'").fetchone()
            ref_id = ref["id"] if ref else None
            if ref_id is not None and cid == ref_id:
                raise ValueError("impossible de supprimer la catégorie « Non catégorisé »")
            db.execute("UPDATE transactions SET categorie_id=? WHERE categorie_id=?", (ref_id, cid))
            db.execute("UPDATE echeances SET categorie_id=? WHERE categorie_id=?", (ref_id, cid))
            db.execute("DELETE FROM categories WHERE id=?", (cid,))
        elif action == "echeance":
            cents = val_montant(body["montant"])
            tiers = (body.get("tiers") or "").strip()
            if not tiers:
                raise ValueError("le tiers est obligatoire")
            jour = int(body["jour"])
            if not (1 <= jour <= 28 or jour == 99):
                raise ValueError("jour invalide (1-28 ou 99 = fin de mois)")
            db.execute("INSERT INTO echeances(tiers, libelle, montant, montant_cents, jour, categorie_id, type_paiement_id, actif) VALUES (?,?,?,?,?,?,?,1)",
                       (tiers, body["libelle"].strip(),
                        cents / 100.0, cents, jour,
                        val_ref(body, "categorie_id"), val_ref(body, "type_paiement_id")))
        elif action == "modification_echeance":
            cents = val_montant(body["montant"])
            tiers = (body.get("tiers") or "").strip()
            if not tiers:
                raise ValueError("le tiers est obligatoire")
            jour = int(body["jour"])
            if not (1 <= jour <= 28 or jour == 99):
                raise ValueError("jour invalide (1-28 ou 99 = fin de mois)")
            db.execute("UPDATE echeances SET tiers=?, libelle=?, montant=?, montant_cents=?, jour=?, categorie_id=?, type_paiement_id=? WHERE id=?",
                       (tiers, body["libelle"].strip(),
                        cents / 100.0, cents, jour,
                        val_ref(body, "categorie_id"), val_ref(body, "type_paiement_id"), int(body["id"])))
        elif action == "toggle_echeance":
            db.execute("UPDATE echeances SET actif = 1-actif WHERE id=?", (int(body["id"]),))
        elif action == "suppr_echeance":
            db.execute("DELETE FROM echeances WHERE id=?", (int(body["id"]),))
        else:
            raise ValueError("action inconnue")
    return {"ok": True}

# ---------------------------------------------------------------- HTML / JS
PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tenue de compte</title>
<style>
:root{--bleu:#4f46e5;--bleu-2:#4338ca;--fond:#f2f4f8;--carte:#fff;--texte:#1c2130;--texte2:#616a80;--bord:#e4e7ef;--ok:#16a34a;--ko:#dc2626;--ombre:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08);--rayon:14px}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;background:var(--fond);color:var(--texte);-webkit-font-smoothing:antialiased}
header{position:sticky;top:0;z-index:50;background:linear-gradient(120deg,#1e1b4b,#312e81 55%,#4338ca);color:#fff;padding:14px 24px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;box-shadow:0 2px 12px rgba(30,27,75,.25)}
header h1{margin:0;font-size:1.35rem;font-weight:700;white-space:nowrap}
.solde{background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.18);padding:8px 16px;border-radius:12px;font-variant-numeric:tabular-nums;font-size:.9rem;color:rgba(255,255,255,.85)}
.solde b{color:#fff;font-size:1.02rem;margin-left:4px}
header .solde b.pos{color:#4ade80}
header .solde b.neg{color:#f87171}
.solde:first-of-type{margin-left:32px}
header button.sec:first-of-type{margin-left:32px}
header button.danger{margin-left:auto}
main{max-width:1600px;margin:24px auto;padding:0 16px;display:grid;gap:20px}
section{background:var(--carte);border:1px solid var(--bord);border-radius:var(--rayon);padding:20px;box-shadow:var(--ombre);min-width:0;overflow-x:auto}
section.cadre{border-color:var(--bord)}
section.rouge{border-color:#fecaca}
h2{margin:0 0 14px;font-size:1.05rem;font-weight:700;color:var(--texte);display:flex;align-items:center;gap:9px}
h2::before{content:"";width:4px;height:18px;border-radius:2px;background:linear-gradient(180deg,var(--bleu),#818cf8);flex:none}
form{display:flex;flex-wrap:wrap;gap:10px;align-items:end}
label{display:flex;flex-direction:column;font-size:.74rem;color:var(--texte2);font-weight:600;gap:5px;letter-spacing:.03em;text-transform:uppercase}
label span::after{content:" *";visibility:hidden}
label:has(>input[required]) span::after{visibility:visible;color:var(--ko);font-weight:700}
input,select{padding:8px 10px;border:1.5px solid var(--bord);border-radius:9px;font-size:.9rem;background:#fff;color:var(--texte);max-width:100%;box-sizing:border-box;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus{outline:none;border-color:var(--bleu);box-shadow:0 0 0 3px rgba(79,70,229,.14)}
button{padding:8px 16px;border:1px solid transparent;border-radius:9px;background:linear-gradient(135deg,var(--bleu),var(--bleu-2));color:#fff;font-weight:600;font-size:.88rem;cursor:pointer;transition:filter .15s,transform .05s,box-shadow .15s;box-shadow:0 1px 2px rgba(79,70,229,.35)}
button:hover{filter:brightness(1.08)}
button:active{transform:translateY(1px)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(79,70,229,.35)}
button.sec{background:#fff;color:var(--texte);border-color:var(--bord);box-shadow:var(--ombre)}
button.sec:hover{filter:none;background:#f5f6fb}
header button.sec{background:rgba(255,255,255,.10);color:#fff;border-color:rgba(255,255,255,.25);box-shadow:none}
header button.sec:hover{background:rgba(255,255,255,.2)}
button.danger{background:#fee2e2;color:#b91c1c;box-shadow:none}
button.danger:hover{filter:none;background:#fecaca}
header button.danger{background:rgba(239,68,68,.85);color:#fff}
header button.danger:hover{background:#ef4444}
.on{background:var(--ok);color:#fff;border-color:transparent;box-shadow:0 1px 2px rgba(22,163,74,.4)}
.on:hover{filter:brightness(1.05)}
header button.on{background:rgba(255,255,255,.22);box-shadow:inset 0 0 0 1px rgba(255,255,255,.4)}
td button{padding:4px 10px;font-size:.82rem;border-radius:8px;box-shadow:none}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{padding:9px 10px;border-bottom:1px solid #eef0f5;text-align:left;overflow-wrap:break-word;min-width:0}
#ebody th,#ebody td{padding:6px 8px}
th{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--texte2)}
tbody tr{transition:background .12s}
tbody tr:hover td{background:#f7f8fd}
.montant{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;font-weight:600}
.pos{color:var(--ok)} .neg{color:var(--ko)}
tr.pointe td{color:#a3a9ba}
tr.edit td{background:#eef2ff}
.filtres{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.filtres>b{color:var(--texte2);font-size:.85rem}
.seg{display:inline-flex;background:#e8eaf2;border-radius:10px;padding:3px;gap:2px}
.seg button{background:transparent;color:var(--texte2);box-shadow:none;border-radius:8px;font-weight:600;padding:6px 14px}
.seg button:hover{filter:none;background:rgba(255,255,255,.6)}
.seg button.on{background:#fff;color:var(--bleu);box-shadow:var(--ombre)}
#f_search{flex:1;min-width:220px}
.pill{padding:4px 12px;border-radius:20px;font-size:.75rem;background:#eef2ff;color:var(--bleu);font-weight:600}
.ech-inactive{opacity:.45;text-decoration:line-through}
.cache{display:none}
.vide{text-align:center;color:var(--texte2);padding:28px 10px!important;font-style:italic}
.badge{background:#eef2ff;color:var(--bleu);padding:2px 8px;border-radius:10px;font-size:.68rem;font-weight:600}
.tag{display:inline-block;background:#f1f3f9;border:1px solid var(--bord);padding:2px 10px;border-radius:20px;font-size:.78rem;font-weight:600;color:var(--texte2);white-space:nowrap}
.layout{display:block}
.grid2{display:contents}
#voile{position:fixed;inset:0;background:rgba(15,18,34,.45);backdrop-filter:blur(3px);z-index:99;display:none}
section.modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:100;width:min(620px,95vw);max-height:90vh;overflow-y:auto;box-shadow:0 24px 64px rgba(15,18,34,.35);animation:pop .18s ease-out}
@keyframes pop{from{opacity:0;transform:translate(-50%,-48%) scale(.97)}to{opacity:1;transform:translate(-50%,-50%) scale(1)}}
section.modal h2{padding-right:40px}
#sec_ech{width:min(930px,95vw)}
.fermer{position:absolute;top:12px;right:12px;padding:4px 11px;font-size:.95rem}
#sec_cat{width:min(760px,95vw)}
.ctgrid{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media(max-width:700px){.ctgrid{grid-template-columns:1fr}}
h3.ct{margin:0;font-size:.75rem;color:var(--texte2);text-transform:uppercase;letter-spacing:.05em}
.ctlist{margin-top:10px;display:flex;flex-direction:column;gap:5px;max-height:46vh;overflow-y:auto;padding-right:2px}
.ctrow{display:flex;align-items:center;gap:8px;padding:7px 10px;border:1px solid var(--bord);border-radius:10px;font-size:.85rem;background:#fafbfe;transition:border-color .12s,background .12s}
.ctrow:hover{border-color:#c7cbe0;background:#fff}
.ctrow .nom{flex:1;overflow-wrap:anywhere;font-weight:600}
.ctrow .nb{color:var(--texte2);font-size:.72rem;white-space:nowrap}
.ctrow button{padding:2px 9px;font-size:.78rem}
#btn_plus{width:100%;margin-top:12px;background:#fff;color:var(--bleu);border:1.5px dashed #c7cbe0;box-shadow:none;font-weight:600}
#btn_plus:hover{filter:none;background:#eef2ff;border-color:var(--bleu)}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:200;background:#1c2130;color:#fff;padding:11px 20px;border-radius:12px;font-size:.9rem;box-shadow:0 8px 24px rgba(15,18,34,.35);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;max-width:90vw}
#toast.visible{opacity:1;transform:translateX(-50%) translateY(-4px)}
</style></head><body>
<header><h1>📒 Tenue De Compte Bancaire</h1>
<span class="solde">Solde : <b id="solde"></b></span>
<span class="solde">Pointé : <b id="soldeP"></b></span>
<button class="sec" onclick="exportCSV()">⬇ Export CSV</button>
<button class="sec" id="menu_ech" onclick="toggleCadre('ech')" title="Afficher/masquer le cadre Échéances">📅 Échéances</button>
<button class="sec" id="menu_cat" onclick="toggleCadre('cat')" title="Afficher/masquer le cadre Catégories">🏷️ Catégories</button>
</header>
<main>
<div id="voile" onclick="fermerTout()"></div>
<div id="toast" role="status" aria-live="polite"></div>
<div class="layout">
<div class="grid2">
<section class="rouge modal" id="sec_ech">
<button class="danger fermer" title="Fermer" onclick="fermerCadre('ech')">✕</button>
<h2>Échéances mensuelles</h2>
<div style="margin-bottom:10px">Total mensuel (échéances actives) : <b id="ech_total"></b></div>
<form onsubmit="addEch(event)">
<label><span>Tiers</span><input id="e_tiers" required placeholder="Bailleur, banque…"></label>
<label><span>Description</span><input id="e_lib" required placeholder="Loyer, crédit auto…"></label>
<label><span>Montant (€)</span><input id="e_montant" required placeholder="-850"></label>
<label><span>Jour (1-28, 99=fin)</span><input id="e_jour" required value="5"></label>
<label><span>Catégorie</span><select id="e_cat"></select></label>
<label><span>Type</span><select id="e_type"></select></label>
<button>Ajouter</button>
</form>
<p style="font-size:.75rem;color:#888">Les échéances actives sont reportées vers les transactions le 1ᵉʳ jour de chaque mois, un mois après l'autre. Modifier une échéance ne met pas à jour les transactions déjà reportées pour les mois à venir.</p>
<table><thead><tr><th>Jour</th><th>Tiers</th><th>Description</th><th>Catégorie</th><th>Type</th><th class="montant">Montant</th><th>Actif</th><th></th></tr></thead>
<tbody id="ebody"></tbody></table>
</section>
<section class="rouge modal" id="sec_cat">
<button class="danger fermer" title="Fermer" onclick="fermerCadre('cat')">✕</button>
<h2>Catégories & types de paiement</h2>
<div class="ctgrid">
<div>
<h3 class="ct">🏷️ Catégories</h3>
<form onsubmit="addCat(event)" style="margin-top:8px">
<label><span>Nom catégorie</span><input id="c_nom" required placeholder="Courses, Loisirs…"></label>
<button>Ajouter</button></form>
<div id="catlist" class="ctlist"></div>
</div>
<div>
<h3 class="ct">💳 Types de paiement</h3>
<form onsubmit="addType(event)" style="margin-top:8px">
<label><span>Nouveau type</span><input id="t_nom" required placeholder="CB, Chèque, PayPal…"></label>
<button>Ajouter</button></form>
<div id="typelist" class="ctlist"></div>
</div>
</div>
<p style="font-size:.75rem;color:#888;margin-bottom:0">✎ renomme · ✕ supprime (les lignes concernées passent en « Non catégorisé » / sans type). Le nombre d'opérations (transactions + échéances) est affiché à côté de chaque élément.</p>
</section>
</div>
<section class="cadre">
<h2>Transactions</h2>
<form onsubmit="addTx(event)">
<label><span>Date</span><input type="date" id="f_date" required></label>
<label><span>Tiers</span><input id="f_tiers" required placeholder="EDF, Carrefour…"></label>
<label><span>Description</span><input id="f_lib" placeholder="Facture, salaire… (option)"></label>
<label><span>Type de paiement</span><select id="f_type"></select></label>
<label><span>Catégorie</span><select id="f_cat"></select></label>
<label><span>Catégorie libre</span><input id="f_newcat" placeholder="nouvelle cat. (option)"></label>
<label><span>Montant (€)</span><input id="f_montant" required placeholder="-45,20 ou 2300"></label>
<button>Ajouter</button>
</form>
<div class="filtres" style="margin-top:14px">
<b>Afficher :</b>
<span class="seg">
<button id="fil_all" class="on" onclick="setFil('')">Toutes</button>
<button id="fil_np" onclick="setFil('0')">Non pointées</button>
<button id="fil_p" onclick="setFil('1')">Pointées</button>
</span>
<input id="f_search" placeholder="🔎 Rechercher (tiers, description, catégorie, type)…" style="flex:1;min-width:220px" oninput="rechercher()">
<span class="pill" id="cnt"></span>
</div>
<table><thead><tr><th>✔</th><th>Date</th><th>Tiers</th><th>Description</th><th>Catégorie</th><th>Type</th><th class="montant">Montant</th><th></th></tr></thead>
<tbody id="tbody"></tbody></table>
<button id="btn_plus" class="sec cache" onclick="plusTx()" style="margin-top:10px"><span id="plus_txt"></span></button>
</section>
</div>
</div>
</main>
<script>
let CATS=[], TYPES=[], FIL='', EDIT=null, EEDIT=null, OFFSET=0;
const TOKEN='%%TOKEN%%';
const eur=c=>(c<0?'-':'')+ (Math.abs(c)/100).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})+' €';
const montantInput=c=>(c<0?'-':'')+ (Math.abs(c)/100).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2});
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtDate=s=>{const[a,m,j]=s.split('-');return j+'/'+m+'/'+a};
let TOAST_T=null;
function toast(m,duree=3500){const t=document.getElementById('toast');t.textContent=m;t.classList.add('visible');clearTimeout(TOAST_T);TOAST_T=setTimeout(()=>t.classList.remove('visible'),duree)}
async function api(action,body){
  const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  if(!r.ok){let m='erreur '+r.status;try{m=(await r.json()).error||m}catch(e){}toast('⚠ '+m);throw new Error(m)}
  return r.json();
}
function fillCats(sel,withEmpty){
  sel.innerHTML=(withEmpty?'<option value="">—</option>':'')+
    CATS.map(c=>`<option value="${c.id}">${esc(c.nom)}</option>`).join('');
}
function fillTypes(sel){
  sel.innerHTML='<option value="">—</option>'+
    TYPES.map(t=>`<option value="${t.id}">${esc(t.nom)}</option>`).join('');
}
let TO=null;
function rechercher(){clearTimeout(TO);OFFSET=0;TO=setTimeout(load,300)}
function plusTx(){OFFSET+=150;load(true)}
async function load(append){
  try{
  const d=await(await fetch('/api/data?pointe='+FIL+'&q='+encodeURIComponent(f_search.value.trim())+'&offset='+OFFSET)).json();
  CATS=d.categories; TYPES=d.types_paiement;
  fillCats(f_cat,true); fillCats(e_cat,true); fillTypes(f_type); fillTypes(e_type);
  solde.textContent=eur(d.solde); solde.className=d.solde<0?'neg':'pos';
  soldeP.textContent=eur(d.solde_pointe); soldeP.className=d.solde_pointe<0?'neg':'pos';
  ech_total.textContent=eur(d.total_echeances);
  cnt.textContent=d.transactions.length+' lignes';
  const rows=d.transactions.map(t=>t.id===EDIT?editRow(t):`<tr class="${t.pointe?'pointe':''}">
   <td><button class="${t.pointe?'on':''}" onclick="pointe(${t.id},${1-t.pointe})">${t.pointe?'✔':'&nbsp;'}</button></td>
   <td>${fmtDate(t.date)}</td><td>${esc(t.tiers||'')}</td><td>${esc(t.libelle)} ${t.echeance_id?'<span class="badge">éch.</span>':''}</td>
   <td>${t.categorie?`<span class="tag">${esc(t.categorie)}</span>`:''}</td><td>${t.type_paiement?`<span class="tag">${esc(t.type_paiement)}</span>`:''}</td>
   <td class="montant ${t.montant_cents<0?'neg':'pos'}">${eur(t.montant_cents)}</td>
   <td><button class="sec" title="Modifier" onclick="editTx(${t.id})">✎</button> <button class="danger" onclick="suppr(${t.id},'transaction')">✕</button></td></tr>`).join('');
  if(append){tbody.insertAdjacentHTML('beforeend',rows)}else{tbody.innerHTML=rows||'<tr><td colspan="8" class="vide">Aucune transaction pour ce filtre</td></tr>'}
  ebody.innerHTML=d.echeances.map(e=>e.id===EEDIT?editEchRow(e):`<tr class="${e.actif?'':'ech-inactive'}">
   <td>${e.jour==99?'fin':e.jour}</td><td>${esc(e.tiers||'')}</td><td>${esc(e.libelle)}</td><td>${e.categorie?`<span class="tag">${esc(e.categorie)}</span>`:''}</td><td>${e.type_paiement?`<span class="tag">${esc(e.type_paiement)}</span>`:''}</td>
   <td class="montant ${e.montant_cents<0?'neg':'pos'}">${eur(e.montant_cents)}</td>
   <td><button class="${e.actif?'on':''}" onclick="toggleEch(${e.id})">${e.actif?'actif':'off'}</button></td>
   <td><button class="sec" title="Modifier" onclick="editEch(${e.id})">✎</button> <button class="danger" onclick="suppr(${e.id},'echeance')">✕</button></td></tr>`).join('')||'<tr><td colspan="8" class="vide">Aucune échéance enregistrée</td></tr>';
  catlist.innerHTML=CATS.map(c=>`<div class="ctrow"><span class="nom">${esc(c.nom)}</span>
   <span class="nb">${c.nb?c.nb+' opér.':'inutilisée'}</span>
   ${c.nom!=='Non catégorisé'?`<button class="sec" title="Renommer" onclick="renommer(${c.id},'categorie')">✎</button>
   <button class="danger" title="Supprimer" onclick="supprRef(${c.id},'categorie')">✕</button>`:'<span class="nb">défaut</span>'}</div>`).join('');
  typelist.innerHTML=TYPES.map(t=>`<div class="ctrow"><span class="nom">${esc(t.nom)}</span>
   <span class="nb">${t.nb?t.nb+' opér.':'inutilisé'}</span>
   <button class="sec" title="Renommer" onclick="renommer(${t.id},'type')">✎</button>
   <button class="danger" title="Supprimer" onclick="supprRef(${t.id},'type')">✕</button></div>`).join('');
  const reste=d.nb_total-d.offset-d.transactions.length;
  btn_plus.classList.toggle('cache',reste<=0);
  if(reste>0)plus_txt.textContent='Afficher plus ('+reste+' restantes)';
  }catch(e){toast('Erreur de chargement : '+e,8000)}
}
async function pointe(id,v){await api('pointage',{id,pointe:v});load()}
function editTx(id){EDIT=id;load()}
function cancelEdit(){EDIT=null;load()}
function editRow(t){
  return `<tr class="edit"><td>✎</td>
   <td><input type="date" id="m_date" value="${t.date}"></td>
   <td><input id="m_tiers" value="${esc(t.tiers||'')}" placeholder="Tiers"></td>
   <td><input id="m_lib" value="${esc(t.libelle)}"></td>
   <td><select id="m_cat"><option value="">—</option>${CATS.map(c=>`<option value="${c.id}" ${c.id==t.categorie_id?'selected':''}>${esc(c.nom)}</option>`).join('')}</select></td>
   <td><select id="m_type"><option value="">—</option>${TYPES.map(x=>`<option value="${x.id}" ${x.id==t.type_paiement_id?'selected':''}>${esc(x.nom)}</option>`).join('')}</select></td>
   <td><input id="m_montant" value="${montantInput(t.montant_cents)}" style="text-align:right"></td>
   <td><button title="Enregistrer" onclick="saveEdit(${t.id})">💾</button> <button class="sec" title="Annuler" onclick="cancelEdit()">✕</button></td></tr>`;
}
async function saveEdit(id){
  try{await api('modification',{id:id,date:m_date.value,tiers:m_tiers.value,libelle:m_lib.value,montant:m_montant.value,categorie_id:m_cat.value||null,type_paiement_id:m_type.value||null});}
  catch(e){toast(e.message);return}
  EDIT=null;load()}
function editEch(id){EEDIT=id;load()}
function cancelEchEdit(){EEDIT=null;load()}
function editEchRow(e){
  return `<tr class="edit">
   <td><input id="x_jour" value="${e.jour}" style="width:52px"></td>
   <td><input id="x_tiers" value="${esc(e.tiers||'')}" placeholder="Tiers"></td>
   <td><input id="x_lib" value="${esc(e.libelle)}"></td>
   <td><select id="x_cat"><option value="">—</option>${CATS.map(c=>`<option value="${c.id}" ${c.id==e.categorie_id?'selected':''}>${esc(c.nom)}</option>`).join('')}</select></td>
   <td><select id="x_type"><option value="">—</option>${TYPES.map(t=>`<option value="${t.id}" ${t.id==e.type_paiement_id?'selected':''}>${esc(t.nom)}</option>`).join('')}</select></td>
   <td><input id="x_montant" value="${montantInput(e.montant_cents)}" style="text-align:right"></td>
   <td></td>
   <td><button title="Enregistrer" onclick="saveEchEdit(${e.id})">💾</button> <button class="sec" title="Annuler" onclick="cancelEchEdit()">✕</button></td></tr>`;
}
async function saveEchEdit(id){
  try{await api('modification_echeance',{id:id,tiers:x_tiers.value,libelle:x_lib.value,jour:x_jour.value,montant:x_montant.value,categorie_id:x_cat.value||null,type_paiement_id:x_type.value||null});}
  catch(e){toast(e.message);return}
  EEDIT=null;load()}
const SUPPR={transaction:'suppression',echeance:'suppr_echeance',categorie:'suppr_categorie',type:'suppr_type'};
async function suppr(id,what){if(!confirm('Supprimer ?'))return;
  await api(SUPPR[what],{id});load()}
async function toggleEch(id){await api('toggle_echeance',{id});load()}
async function renommer(id,kind){
  const it=(kind==='categorie'?CATS:TYPES).find(x=>x.id===id);if(!it)return;
  const nom=prompt('Nouveau nom :',it.nom);
  if(nom===null)return;if(!nom.trim())return toast('le nom est obligatoire');
  await api(kind==='categorie'?'renommer_categorie':'renommer_type',{id,nom});load()}
async function supprRef(id,kind){
  const it=(kind==='categorie'?CATS:TYPES).find(x=>x.id===id);if(!it)return;
  const cible=kind==='categorie'?'« Non catégorisé »':'« sans type »';
  const msg=it.nb?`« ${it.nom} » est utilisé(e) par ${it.nb} opération(s). Les lignes concernées passeront en ${cible}. Supprimer ?`:`Supprimer « ${it.nom} » ?`;
  if(!confirm(msg))return;
  await api(kind==='categorie'?'suppr_categorie':'suppr_type',{id});load()}
async function addTx(ev){ev.preventDefault();
  try{await api('transaction',{date:f_date.value,tiers:f_tiers.value,libelle:f_lib.value,montant:f_montant.value,categorie_id:f_cat.value||null,type_paiement_id:f_type.value||null,nouvelle_categorie:f_newcat.value.trim()||null});}
  catch(e){toast(e.message);return}
  f_lib.value='';f_tiers.value='';f_montant.value='';f_newcat.value='';OFFSET=0;load()}
async function addEch(ev){ev.preventDefault();
  try{await api('echeance',{tiers:e_tiers.value,libelle:e_lib.value,montant:e_montant.value,jour:e_jour.value,categorie_id:e_cat.value||null,type_paiement_id:e_type.value||null});}
  catch(e){toast(e.message);return}
  e_lib.value='';e_tiers.value='';e_montant.value='';load()}
async function addCat(ev){ev.preventDefault();await api('categorie',{nom:c_nom.value});c_nom.value='';load()}
async function addType(ev){ev.preventDefault();await api('type',{nom:t_nom.value});t_nom.value='';load()}
function setFil(f){FIL=f;OFFSET=0;[fil_all,fil_np,fil_p].forEach(b=>b.classList.remove('on'));
  ({'':fil_all,'0':fil_np,'1':fil_p})[f].classList.add('on');load()}
function exportCSV(){window.open('/api/export?pointe='+FIL+'&q='+encodeURIComponent(f_search.value.trim()))}
function toggleCadre(k){
  const visible = localStorage.getItem('cadre_'+k)==='1';
  localStorage.setItem('cadre_'+k, visible?'0':'1');
  appliquerCadres();
}
function fermerCadre(k){
  localStorage.setItem('cadre_'+k,'0');
  appliquerCadres();
}
function fermerTout(){
  localStorage.setItem('cadre_ech','0');
  localStorage.setItem('cadre_cat','0');
  appliquerCadres();
}
function appliquerCadres(){
  const ech = localStorage.getItem('cadre_ech')==='1';
  const cat = localStorage.getItem('cadre_cat')==='1';
  sec_ech.classList.toggle('cache',!ech);
  sec_cat.classList.toggle('cache',!cat);
  menu_ech.classList.toggle('on',ech);
  menu_cat.classList.toggle('on',cat);
  voile.style.display = (ech||cat)?'block':'none';
}
# Sur un serveur : le bouton ⏻ Quitter est retiré de l'interface et la route
# /api/arret est neutralisée — une application exposée au web ne doit pas
# pouvoir être arrêtée depuis un navigateur.
async function quitter(){
  toast('ℹ Arrêt désactivé sur le serveur : utilisez systemctl stop compte_bancaire.')}
f_date.value=new Date().toISOString().slice(0,10);
appliquerCadres();
load();
</script></body></html>"""

# ---------------------------------------------------------------- serveur
class Serveur(ThreadingHTTPServer):
    # Sous Windows, SO_REUSEADDR autorise le bind sur un port déjà pris :
    # deux instances pourraient tourner sur la même base. On le désactive.
    allow_reuse_address = os.name != "nt"

def arret_propre(server):
    """Arrêt complet : shutdown du serveur, sauvegarde de la base, arrêt brutal."""
    try:
        server.shutdown()
    except Exception:
        pass
    backup_db()
    try:
        sys.stdout.flush()
        if isinstance(sys.stdout, Tee):
            sys.stdout.fichier.flush()
            sys.stdout.fichier.close()
    except Exception:
        pass
    os._exit(0)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            data = PAGE.replace("%%TOKEN%%", TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/api/data":
            q = dict(parse_qsl(u.query))
            db = get_db()
            try:
                self._json(api_get(db, q))
            finally:
                db.close()
        elif u.path == "/api/export":
            q = dict(parse_qsl(u.query))
            where, params = build_where(q)
            db = get_db()
            try:
                buf = io.StringIO()
                w = csv.writer(buf, delimiter=";")
                w.writerow(["Date", "Tiers", "Description", "Catégorie", "Type", "Montant", "Pointé"])
                for t in db.execute(
                        "SELECT t.*, c.nom AS categorie, p.nom AS type_paiement FROM transactions t "
                        "LEFT JOIN categories c ON c.id=t.categorie_id "
                        "LEFT JOIN types_paiement p ON p.id=t.type_paiement_id" + where +
                        " ORDER BY t.date, t.id", params):
                    w.writerow([t["date"], t["tiers"] or "", t["libelle"], t["categorie"] or "",
                                t["type_paiement"] or "",
                                fmt_cents(t["montant_cents"]),
                                "OUI" if t["pointe"] else "NON"])
                # ligne de solde (toutes transactions, hors filtre)
                solde = db.execute("SELECT COALESCE(SUM(montant_cents),0) FROM transactions").fetchone()[0]
                w.writerow(["", "", "SOLDE TOTAL", "", "", fmt_cents(solde), ""])
                csv_data = "\ufeff" + buf.getvalue()  # BOM pour Excel/LibreOffice
            finally:
                db.close()
            data = csv_data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=compte.csv")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            self.send_error(404); return
        action = u.path[len("/api/"):]
        if action == "arret":
            # arrêt depuis le navigateur désactivé (déploiement serveur) :
            # on refuse systématiquement, même avec un jeton valide.
            self._json({"error": "arrêt désactivé sur le serveur"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            db = get_db()
            try:
                self._json(api_post(db, action, body))
            finally:
                db.close()
        except (ValueError, sqlite3.Error, KeyError, TypeError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": "Erreur interne : " + repr(e)}, 500)

if __name__ == "__main__":
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    except OSError as e:
        sys.exit(f"✖ Impossible de créer le dossier de la base ({e}). "
                 + ("pCloud Drive est-il bien lancé et monté ?"
                    if os.name != "nt" else
                    "pCloud Drive est-il bien démarré (lecteur pCloud visible dans l'Explorateur) ?"))
    # journal : copie de la sortie console dans le dossier de la base
    try:
        log = open(FICHIER_LOG, "a", encoding="utf-8", buffering=1)
    except OSError as e:
        print(f"⚠ Journal indisponible ({e}) — sortie console uniquement.")
        log = None
    if log:
        tee = Tee(log)
        sys.stdout = tee
        sys.stderr = tee
        print(f"--- Démarrage du {date.today().isoformat()} ---")
    # schéma + migrations UNE SEULE FOIS, puis sauvegarde du jour
    init_db()
    backup_db()
    generer_echeances()
    url = f"http://localhost:{PORT}"
    try:
        server = Serveur(("127.0.0.1", PORT), Handler)
    except OSError:
        # déjà lancée : on ouvre simplement le navigateur sur l'instance existante
        print("⚠ Déjà lancée — ouverture du navigateur.")
        webbrowser.open(url)
        sys.exit(0)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"✔ Base : {DB_PATH}")
    print(f"▶ Application : {url}  (Ctrl+C pour arrêter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # arrêt normal (Ctrl+C) : sauvegarde puis fermeture propre du journal
        backup_db()
        try:
            if isinstance(sys.stdout, Tee):
                sys.stdout.fichier.flush()
                sys.stdout.fichier.close()
        except Exception:
            pass