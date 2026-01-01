import os
import json
import hashlib
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_IDS = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",") if c.strip()]

SOURCES_FILE = "sources.json"
STATE_FILE = "state.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EditaisMonitor/1.0)"}

def load_sources():
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["sources"]

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def fetch_page(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

def extract_links(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = " ".join(a.get_text(" ").split())
        if not text:
            continue
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)
        if href.startswith("http"):
            links.append((text[:140], href))
    return links

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True
            },
            timeout=30
        )
        print("TELEGRAM SEND -> chat_id:", chat_id, "status:", r.status_code, "resp:", r.text)
        r.raise_for_status()

def main():
    sources = load_sources()
    state = load_state()
    seen = state.get("seen", {})

    new_items = []

    for s in sources:
        name = s["name"]
        url = s["url"]
        try:
            html = fetch_page(url)
            page_fingerprint = sha(html)
            last_fp = seen.get(url, {}).get("fingerprint")

            if last_fp == page_fingerprint:
                continue

            links = extract_links(html, url)
            keywords = ["edital", "chamamento", "seleção", "oportunidade", "retificação", "prorrogação"]
            candidates = [(t, h) for (t, h) in links if any(k in t.lower() for k in keywords) or any(k in h.lower() for k in keywords)]
            if not candidates:
                candidates = links[:10]

            candidates_sig = sha(json.dumps(candidates, ensure_ascii=False))
            last_sig = seen.get(url, {}).get("candidates_sig")

            if last_sig != candidates_sig:
                for (t, h) in candidates[:8]:
                    new_items.append((name, t, h))

            seen[url] = {
                "fingerprint": page_fingerprint,
                "candidates_sig": candidates_sig,
                "checked_at": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            new_items.append((name, "⚠️ Erro ao verificar fonte", f"{url} | {type(e).__name__}: {e}"))

    state["seen"] = seen
    save_state(state)

    if new_items:
        now = datetime.now().strftime("%d/%m/%Y %H:%M")
        msg_lines = [f"🔎 Novidades detectadas ({now})", ""]
        for (src, title, link) in new_items[:15]:
            msg_lines.append(f"• {src}: {title}\n  {link}")
        tg_send("\n".join(msg_lines))

    if new_items:
        now = datetime.now().strftime("%d/%m/%Y %H:%M")
        msg_lines = [f"🔎 Novidades detectadas ({now})", ""]
        for (src, title, link) in new_items[:15]:
            msg_lines.append(f"• {src}: {title}\n  {link}")
        tg_send("\n".join(msg_lines))
    else:
        now = datetime.now().strftime("%d/%m/%Y %H:%M")
        tg_send(f"✅ Monitor rodou ({now}) e não encontrou novidades nas fontes.")

