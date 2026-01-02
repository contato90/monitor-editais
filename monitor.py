import os
import json
import hashlib
import re
from datetime import datetime, timezone, date

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_IDS = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",") if c.strip()]

SOURCES_FILE = "sources.json"
STATE_FILE = "state.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EditaisMonitor/1.0)"}

# Meses PT-BR para "31 de janeiro de 2026"
PT_MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "março": 3,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


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
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        # Se der erro de token/chat_id, vai aparecer nos logs do Actions:
        r.raise_for_status()


def clean_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_deadlines(text: str):
    """
    Retorna lista de datas encontradas no texto (date objects).
    Procura padrões comuns:
      - até 31/01/2026
      - prazo: 31-01-2026
      - inscrições até 31.01.2026
      - até 31 de janeiro de 2026
    """
    t = text.lower()

    found = []

    # 1) Padrão com separadores numéricos (dd/mm/aaaa, dd-mm-aaaa, dd.mm.aaaa)
    # Damos preferência quando aparece perto de palavras-chave.
    kw = r"(até|prazo|inscri|submiss|envio|entrega|encerr|data\s*limite|deadline)"
    rx_kw_num = re.compile(
        rf"{kw}[^0-9]{{0,60}}(\d{{1,2}})[\/\.-](\d{{1,2}})[\/\.-](\d{{2,4}})"
    )
    for m in rx_kw_num.finditer(t):
        d, mo, y = m.group(2), m.group(3), m.group(4)  # cuidado com grupos
        # Na regex, grupos: kw (1), dia (2), mês (3), ano (4)
        day_i = int(m.group(2))
        mon_i = int(m.group(3))
        year_i = int(m.group(4))
        if year_i < 100:
            year_i += 2000
        try:
            found.append(date(year_i, mon_i, day_i))
        except ValueError:
            pass

    # 2) Caso não tenha achado com keyword, tenta pegar qualquer data numérica no texto
    rx_any_num = re.compile(r"(\d{1,2})[\/\.-](\d{1,2})[\/\.-](\d{2,4})")
    for m in rx_any_num.finditer(t):
        day_i = int(m.group(1))
        mon_i = int(m.group(2))
        year_i = int(m.group(3))
        if year_i < 100:
            year_i += 2000
        try:
            found.append(date(year_i, mon_i, day_i))
        except ValueError:
            pass

    # 3) Formato por extenso: "até 31 de janeiro de 2026"
    rx_ext = re.compile(
        r"(até|prazo|inscri|submiss|envio|entrega|encerr|data\s*limite|deadline)"
        r"[^0-9]{0,60}(\d{1,2})\s+de\s+([a-zçãõéêíóôú]+)\s+de\s+(\d{4})"
    )
    for m in rx_ext.finditer(t):
        day_i = int(m.group(2))
        month_name = m.group(3).strip()
        year_i = int(m.group(4))
        mon_i = PT_MONTHS.get(month_name)
        if not mon_i:
            continue
        try:
            found.append(date(year_i, mon_i, day_i))
        except ValueError:
            pass

    return found


def pick_deadline(text: str):
    """
    Escolhe uma 'data-limite' provável:
    - Se existirem datas: usa a MAIS RECENTE (maior), porque em páginas pode ter datas de início e outras referências.
    - Se não achar nenhuma: retorna None
    """
    dates = parse_deadlines(text)
    if not dates:
        return None
    return max(dates)


def is_expired(deadline: date) -> bool:
    today = datetime.now().date()
    return deadline < today


def main():
    sources = load_sources()
    state = load_state()
    seen = state.get("seen", {})

    new_items = []
    skipped_expired = 0

    # Checa fontes (páginas principais)
    for s in sources:
        name = s["name"]
        url = s["url"]
        try:
            html = fetch_page(url)
            page_fingerprint = sha(html)
            last_fp = seen.get(url, {}).get("fingerprint")

            # Se página não mudou, pula
            if last_fp == page_fingerprint:
                continue

            links = extract_links(html, url)

            keywords = ["edital", "chamamento", "seleção", "oportunidade", "retificação", "prorrogação", "inscrição", "inscrições"]
            candidates = [
                (t, h) for (t, h) in links
                if any(k in t.lower() for k in keywords) or any(k in h.lower() for k in keywords)
            ]
            if not candidates:
                candidates = links[:10]

            candidates_sig = sha(json.dumps(candidates, ensure_ascii=False))
            last_sig = seen.get(url, {}).get("candidates_sig")

            # Se houve mudança nos candidatos, avaliamos os links
            if last_sig != candidates_sig:
                for (t, h) in candidates[:8]:
                    try:
                        detail_html = fetch_page(h)
                        detail_text = clean_text_from_html(detail_html)
                        deadline = pick_deadline(detail_text)

                        if deadline and is_expired(deadline):
                            skipped_expired += 1
                            continue

                        if deadline:
                            new_items.append((name, f"{t} (prazo: {deadline.strftime('%d/%m/%Y')})", h))
                        else:
                            new_items.append((name, f"{t} (⚠️ sem prazo detectado)", h))

                    except Exception as e:
                        # Se não conseguir abrir a página do item, ainda avisa (pra você decidir manualmente)
                        new_items.append((name, f"{t} (⚠️ erro ao ler detalhes)", f"{h} | {type(e).__name__}: {e}"))

            # Atualiza estado da fonte
            seen[url] = {
                "fingerprint": page_fingerprint,
                "candidates_sig": candidates_sig,
                "checked_at": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            new_items.append((name, "⚠️ Erro ao verificar fonte", f"{url} | {type(e).__name__}: {e}"))

    state["seen"] = seen
    save_state(state)

    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    if new_items:
        msg_lines = [f"🔎 Novidades detectadas ({now})", ""]
        for (src, title, link) in new_items[:20]:
            msg_lines.append(f"• {src}: {title}\n  {link}")

        if skipped_expired:
            msg_lines.append("")
            msg_lines.append(f"🧹 Filtrados por prazo vencido: {skipped_expired}")

        tg_send("\n".join(msg_lines))
    else:
        # Mensagem “prova de vida” (como você gosta)
        extra = f" | filtrados vencidos: {skipped_expired}" if skipped_expired else ""
        tg_send(f"✅ Monitor rodou ({now}) e não encontrou novidades nas fontes.{extra}")


if __name__ == "__main__":
    main()
