#!/usr/bin/env python3
"""
Bot de surveillance des logements CROUS (phase complementaire).

Principe :
- Recupere la page de recherche (URL avec le parametre bounds = ta zone).
- Extrait les logements actuellement disponibles.
- Compare avec l'etat de la derniere execution (state.json).
- Envoie une notification Telegram UNIQUEMENT s'il y a du nouveau.
- Un logement qui disparait puis revient (desistement) redeclenche une alerte.

Configuration via variables d'environnement :
- SEARCH_URL          : URL de recherche CROUS avec bounds (a coller le jour J)
- TELEGRAM_BOT_TOKEN  : token du bot (via @BotFather):
- TELEGRAM_CHAT_ID    : ton chat id Telegram :
- HEARTBEAT           : "1" (defaut) = 1 message "bot actif" par jour vers 8h, "0" = jamais
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SEARCH_URL = os.environ.get("SEARCH_URL", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
HEARTBEAT = os.environ.get("HEARTBEAT", "1").strip() != "0"

BASE = "https://trouverunlogement.lescrous.fr"
STATE_FILE = Path(__file__).parent / "state.json"
MAX_DETAILED_ALERTS = 8  # au-dela, un seul message recapitulatif

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}


# ---------------------------------------------------------------- Telegram

def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("[info] Telegram non configure, message non envoye :")
        print(text)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[erreur] Telegram a repondu {r.status_code} : {r.text[:200]}")
    except requests.RequestException as exc:
        print(f"[erreur] Envoi Telegram impossible : {exc}")


# ------------------------------------------------------------------- Etat

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[avertissement] state.json illisible, on repart de zero.")
    return {"available": [], "errors": 0, "last_heartbeat": ""}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ------------------------------------------------------------------ Parsing

def parse_listings(html_text: str):
    """Retourne (liste de logements, compteur affiche par la page ou None)."""
    soup = BeautifulSoup(html_text, "html.parser")

    page_text = " ".join(soup.get_text(" ", strip=True).split())
    count = None
    m = re.search(r"(\d+)\s+logements?\b", page_text, re.IGNORECASE)
    if m:
        count = int(m.group(1))

    listings = {}
    for link in soup.find_all("a", href=True):
        m = re.search(r"/accommodations/(\d+)", link["href"])
        if not m:
            continue
        lid = m.group(1)

        title = (link.get_text(" ", strip=True) or "Logement CROUS")[:150]

        # Prix : on remonte progressivement les parents et on s'arrete au
        # premier niveau qui contient un montant en euros, sans dependre
        # des noms de classes CSS. Garde-fou : si le texte devient trop
        # long, on a quitte la carte, on abandonne.
        price = ""
        node = link
        for _ in range(4):
            if node.parent is None:
                break
            node = node.parent
            node_text = " ".join(node.get_text(" ", strip=True).split())
            if len(node_text) > 500:
                break
            pm = re.search(
                r"(?<![A-Za-z0-9])\d{1,4}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?"
                r"[\s\u00a0]*\u20ac",
                node_text,
            )
            if pm:
                price = pm.group(0).replace("\u00a0", " ").strip()
                break

        href = link["href"]
        url = BASE + href if href.startswith("/") else href

        if lid in listings:
            # Plusieurs liens vers le meme logement : on garde le titre le
            # plus informatif et on complete le prix s'il manquait.
            if len(title) > len(listings[lid]["title"]):
                listings[lid]["title"] = title
            if price and not listings[lid]["price"]:
                listings[lid]["price"] = price
            continue

        listings[lid] = {"id": lid, "title": title, "price": price, "url": url}

    return list(listings.values()), count


def fetch_listings():
    r = requests.get(SEARCH_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    listings, count = parse_listings(r.text)
    # La page annonce des resultats mais on n'a rien extrait :
    # la structure du site a probablement change.
    if count and count > 0 and not listings:
        raise RuntimeError(
            f"La page annonce {count} logement(s) mais le parsing n'a rien extrait "
            "(structure HTML modifiee ?)."
        )
    return listings


# -------------------------------------------------------------------- Main

def notify_new(new_items, total_visible):
    if len(new_items) <= MAX_DETAILED_ALERTS:
        for item in new_items:
            lines = ["NOUVEAU logement CROUS dans ta zone", html.escape(item["title"])]
            if item["price"]:
                lines.append(f"Loyer : {html.escape(item['price'])}")
            lines.append(item["url"])
            send_telegram("\n".join(lines))
    else:
        lines = [
            f"{len(new_items)} NOUVEAUX logements CROUS dans ta zone "
            f"({total_visible} visibles au total) :"
        ]
        for item in new_items[:MAX_DETAILED_ALERTS]:
            price = f" - {item['price']}" if item["price"] else ""
            lines.append(f"- {html.escape(item['title'])}{price}")
        lines.append("...")
        lines.append(f"Voir tout : {SEARCH_URL}")
        send_telegram("\n".join(lines))


def maybe_heartbeat(state: dict, total_visible: int) -> None:
    if not HEARTBEAT:
        return
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Paris"))
    except Exception:
        now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if state.get("last_heartbeat") != today and now.hour >= 8:
        send_telegram(
            f"Bot actif. {total_visible} logement(s) actuellement visible(s) "
            "dans ta zone. (Message quotidien de controle, HEARTBEAT=0 pour le couper.)"
        )
        state["last_heartbeat"] = today


def main() -> int:
    if not SEARCH_URL:
        print("[info] SEARCH_URL absent : la phase n'est pas encore ouverte. "
              "Ajoute la variable SEARCH_URL sur GitHub puis relance.")
        return 0

    state = load_state()

    try:
        listings = fetch_listings()
        state["errors"] = 0
    except Exception as exc:
        state["errors"] = int(state.get("errors", 0)) + 1
        print(f"[erreur] Lecture du site impossible ({state['errors']}e fois) : {exc}")
        if state["errors"] == 3:
            send_telegram(
                "ATTENTION : le bot n'arrive plus a lire le site du CROUS "
                "(3 echecs de suite). Verifie l'URL ou va voir le site a la main."
            )
        save_state(state)
        return 0  # on ne fait pas echouer le workflow, on reessaiera

    previous = set(state.get("available", []))
    current_ids = [item["id"] for item in listings]
    new_items = [item for item in listings if item["id"] not in previous]

    print(f"[info] {len(listings)} logement(s) visibles, {len(new_items)} nouveau(x).")

    if new_items:
        notify_new(new_items, len(listings))

    maybe_heartbeat(state, len(listings))

    state["available"] = current_ids
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())