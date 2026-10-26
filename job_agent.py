#!/usr/bin/env python3
"""Job Agent – hlídání pracovních příležitostí v Praze přes RSS + e-mail přes Resend."""

import html
import imaplib
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import requests

# ----------------------------- KONFIGURACE -----------------------------

FEEDS = [
    # "https://www.startupjobs.cz/nabidky.rss",
]

# Nabídky starší než tento počet dní se přeskočí (pokud feed uvádí datum).
MAX_AGE_DAYS = 3

# Volitelný filtr lokality, např. ("praha", "prague", "remote"). None = bez filtru.
LOCATION_KEYWORDS = None

ROLE_KEYWORDS = [
    "it", "administrator", "správce", "webmaster", "administrativ", "junior",
    "podpora", "marketing", "e-shop", "eshop", "copywriter", "asistent",
    "home office", "homeoffice", "home-office", "remote", "z domova", "z domu",
]

CONTRACT_KEYWORDS = [
    "dpp", "dpč", "dohoda", "ičo", "ico", "živnost", "osvč", "poloviční",
    "part-time", "brigáda", "částečný",
]

# Krátká klíčová slova se hledají jako celá slova (aby "it" nesedло na "city").
WHOLE_WORD = {"it", "dpp", "dpč", "ico", "ičo"}

# Nabídky s těmito výrazy se vždy vyřadí (regulární výrazy, malá písmena).
EXCLUDE_PATTERNS = [
    r"ukl[ií]d\w*", r"úklid\w*", r"ukl[ií]z\w*",
    r"(?<!\w)bar(?:u|em|e|ech|y|ů)?(?!\w)", r"barman\w*", r"bartend\w*",
    r"číšn\w*", r"cisn\w*",
]

# Nabídky s těmito výrazy se řadí nahoru a dostanou označení "Doporučeno".
PRIORITY_KEYWORDS = [
    "web", "wordpress", "e-shop", "eshop", "webmaster", "it", "it podpora", "helpdesk",
    "administrativ", "asistent", "marketing", "copywrit", "social media", "sociální sítě",
    "home office", "homeoffice", "home-office", "remote", "z domova", "z domu", "vzdálen",
]

# Skupiny pro motivační text.
WEB_WORDS = ["web", "wordpress", "e-shop", "eshop", "webmaster", "administrator", "administrátor",
             "správce", "it", "helpdesk"]
MARKETING_WORDS = ["marketing", "copywrit", "obsah", "social media", "sociální sítě", "newsletter"]
ADMIN_WORDS = ["administrativ", "asistent", "kancelář", "zadávání dat", "fakturac", "účetn"]

# E-mailové alerty z Prace.cz / Jobs.cz (čtou se z Gmailu přes IMAP).
ALERT_SENDERS = ["prace.cz", "jobs.cz", "almacareer"]
ALERT_DAYS = 2          # Kolik dní zpět se alerty čtou
MAX_ALERT_ITEMS = 60    # Pojistka proti záplavě

# Odkazy v alertech, které nejsou nabídky (patička, odhlášení, sociální sítě...).
SKIP_LINK_WORDS = [
    "odhlás", "odhlas", "unsubscribe", "nastaven", "zrušit", "zrusit", "facebook",
    "linkedin", "instagram", "youtube", "ochrana", "podmínky", "podminky",
    "v prohlížeči", "v prohlizeci", "zobrazit všechny", "zobrazit vsechny",
    "více nabídek", "vice nabidek", "aplikac", "google play", "app store",
    "kontakt", "nápověd", "napoved", "gdpr", "cookies",
    "pohovor", "životopis", "zivotopis", "motivační dopis", "motivacni dopis",
    "práce a brigády", "prace a brigady", "ukázat všechny", "ukazat vsechny",
    "kritéria", "kriteria", "poradna", "rady a tipy",
    "potvrdit", "potvrďte", "potvrdte", "potvrzujeme", "zrušte", "zrusite",
    "ověřit", "overit", "přihlásit", "prihlasit", "heslo",
]

# E-maily s těmito slovy v předmětu se přeskočí (potvrzení vlastních odpovědí apod.).
SKIP_SUBJECT_WORDS = [
    "hlásíte se", "hlasite se", "odeslali jste", "potvrzení", "potvrzeni",
    "potvrďte", "potvrdte", "potvrzujeme", "ověřte", "overte", "vítejte", "vitejte", "heslo",
]

NAME = "Jáchym Singh"
PHONE = "773 941 841"

HEADERS = {"User-Agent": "Mozilla/5.0 (JobAgent)"}

# ----------------------------- POMOCNÉ FUNKCE -----------------------------


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def find_keywords(text: str, keywords: list) -> list:
    text = text.lower()
    found = []
    for kw in keywords:
        if kw in WHOLE_WORD:
            pattern = rf"(?<!\w){re.escape(kw)}(?!\w)"
        else:
            pattern = re.escape(kw)
        if re.search(pattern, text):
            found.append(kw)
    return found


def fetch_feed(url: str) -> list:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as exc:
        print(f"[WARN] Feed {url} se nepodařilo načíst: {exc}", file=sys.stderr)
        return []

    items = []
    for item in root.iter("item"):
        items.append({
            "title": strip_html(item.findtext("title", "")),
            "link": (item.findtext("link", "") or "").strip(),
            "description": strip_html(item.findtext("description", "")),
            "pubdate": (item.findtext("pubDate", "") or "").strip(),
            "source": url,
        })
    return items


def is_recent(pubdate: str) -> bool:
    if not pubdate:
        return True
    try:
        dt = parsedate_to_datetime(pubdate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)


def is_excluded(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in EXCLUDE_PATTERNS)


def filter_jobs(items: list) -> list:
    result, seen = [], set()
    for it in items:
        if not it["link"] or it["link"] in seen:
            continue
        trusted = bool(it.get("trusted"))
        text = it["title"] if trusted else f'{it["title"]} {it["description"]}'

        if is_excluded(text):
            continue

        if trusted:
            roles, contracts = [], []
        else:
            roles = find_keywords(text, ROLE_KEYWORDS)
            contracts = find_keywords(text, CONTRACT_KEYWORDS)
            if not roles or not contracts:
                continue
            if LOCATION_KEYWORDS and not find_keywords(text, list(LOCATION_KEYWORDS)):
                continue
        if not is_recent(it["pubdate"]):
            continue

        priority = find_keywords(text, PRIORITY_KEYWORDS)
        seen.add(it["link"])
        it["roles"], it["contracts"], it["priority"] = roles, contracts, priority
        it["tags"] = list(dict.fromkeys(roles + contracts + priority))
        result.append(it)

    result.sort(key=lambda j: -len(j["priority"]))
    return result


# ----------------------------- ALERTY Z GMAILU -----------------------------


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None


def extract_links(body: str, is_html: bool) -> list:
    if is_html:
        parser = LinkParser()
        parser.feed(body)
        return parser.links
    links, prev = [], ""
    for line in body.splitlines():
        line = line.strip()
        match = re.search(r"https?://\S+", line)
        if match:
            links.append((match.group(0), prev))
        elif line:
            prev = line
    return links


def find_all_mail_folder(imap) -> str:
    try:
        status, folders = imap.list()
        for raw in folders or []:
            line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
            if "\\All" in line:
                match = re.search(r'"([^"]+)"\s*$', line)
                if match:
                    return match.group(1)
    except (imaplib.IMAP4.error, OSError):
        pass
    return "INBOX"


def fetch_alerts() -> list:
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_PASSWORD"]
    query = f'from:({" OR ".join(ALERT_SENDERS)}) newer_than:{ALERT_DAYS}d'
    items, seen_titles = [], set()

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(sender, password)
            folder = find_all_mail_folder(imap)
            status, _ = imap.select(f'"{folder}"', readonly=True)
            if status != "OK":
                folder = "INBOX"
                imap.select("INBOX", readonly=True)
            print(f"Čtená složka Gmailu: {folder}")
            status, data = imap.search(None, "X-GM-RAW", f'"{query}"')
            if status != "OK":
                print("[WARN] Hledání alertů v Gmailu selhalo.", file=sys.stderr)
                return []
            ids = data[0].split()
            print(f"Nalezeno alert e-mailů: {len(ids)}")

            for num in ids:
                status, msg_data = imap.fetch(num, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = message_from_bytes(msg_data[0][1], policy=policy.default)
                part = msg.get_body(preferencelist=("html", "plain"))
                if part is None:
                    continue
                content = part.get_content()
                is_html = part.get_content_type() == "text/html"
                subject = strip_html(str(msg.get("Subject", "")))
                if any(w in subject.lower() for w in SKIP_SUBJECT_WORDS):
                    print(f"[SKIP] e-mail přeskočen (předmět): {subject[:80]}")
                    continue
                date = str(msg.get("Date", ""))

                for href, text in extract_links(content, is_html):
                    low = f"{text} {href}".lower()
                    if not href.startswith("http") or len(text) < 15:
                        continue
                    if any(w in low for w in SKIP_LINK_WORDS):
                        continue
                    if text.lower() in seen_titles:
                        continue
                    seen_titles.add(text.lower())
                    print(f"[ALERT] {text[:80]} -> {href[:90]}")
                    items.append({
                        "title": text,
                        "link": href,
                        "description": f"Z e-mailového upozornění: {subject}" if subject else "",
                        "pubdate": date,
                        "source": "alert",
                        "trusted": True,
                    })
                    if len(items) >= MAX_ALERT_ITEMS:
                        return items
    except (imaplib.IMAP4.error, OSError) as exc:
        print(f"[WARN] Alerty z Gmailu se nepodařilo načíst: {exc}", file=sys.stderr)
    return items


# ----------------------------- MOTIVAČNÍ TEXT -----------------------------


def clean_title(title: str) -> str:
    t = title.split(" | ")[0]
    t = re.sub(r"^\s*PROJEKT:\s*", "", t, flags=re.I)
    t = re.sub(r"\s*\([^)]*\)", "", t)
    t = re.sub(r"\s+za\s+\d+\s*Kč/h", "", t)
    t = t.strip(" !-–:")
    if t.isupper():
        t = t.capitalize()
        t = re.sub(r"\b(prah\w*|praz\w*|brn\w*)", lambda m: m.group(1).capitalize(), t)
    return t[:80].strip() or title


def join_parts(parts: list) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " a " + parts[-1]


def build_reply(job: dict) -> str:
    text = job["title"] if job.get("trusted") else f'{job["title"]} {job["description"]}'
    parts = []
    if find_keywords(text, WEB_WORDS):
        parts.append("s tvorbou a správou webů, prací ve WordPressu a obsluhou e-shopů")
    if find_keywords(text, MARKETING_WORDS):
        parts.append("s tvorbou obsahu a podporou online marketingu")
    if find_keywords(text, ADMIN_WORDS):
        parts.append("s administrativou a účetním programem Pohoda")

    if parts:
        body = (
            f"Mám praktické zkušenosti {join_parts(parts)}. "
            "Jsem zodpovědný, rychle se učím a rád pracuji samostatně."
        )
    else:
        body = (
            "Jsem spolehlivý, zodpovědný a flexibilní, rychle se učím a nové úkoly zvládám samostatně. "
            "Rozvrh školy umím skloubit s prací a na domluvených termínech si zakládám. "
            "Mám také zkušenosti s prací na počítači a s administrativou."
        )

    return (
        "Dobrý den,\n\n"
        f"na základě Vašeho inzerátu „{clean_title(job['title'])}“ bych se rád ucházel o tuto pozici. "
        "Jsem student Obchodní akademie Praha – Vinohrady a hledám brigádu nebo "
        "částečný úvazek, který bych zvládal skloubit se školou.\n\n"
        f"{body}\n\n"
        "Rád se s Vámi sejdu osobně nebo online a upřesním, jak bych Vám mohl pomoci.\n\n"
        "S pozdravem\n"
        f"{NAME}\n"
        f"tel.: {PHONE}"
    )


# ----------------------------- HTML E-MAIL (ŠIRŠÍ A RESPONSIVNÍ DESIGN) -----------------------------


def build_html(jobs: list) -> str:
    e = html.escape
    cards = []
    for job in jobs:
        desc = job["description"]
        if len(desc) > 400:
            desc = desc[:400].rsplit(" ", 1)[0] + "…"
        tags = "".join(
            f'<span style="display:inline-block;background:#eef2ff;color:#3730a3;'
            f'border-radius:12px;padding:3px 10px;margin:2px 4px 2px 0;font-size:12px;font-weight:500;">{e(t)}</span>'
            for t in job["tags"]
        )
        date_line = (
            f'<div style="color:#64748b;font-size:12px;margin-bottom:8px;">{e(job["pubdate"])}</div>'
            if job["pubdate"] else ""
        )
        star = (
            '<div style="display:inline-block;background:#fef3c7;color:#92400e;border-radius:6px;'
            'padding:3px 8px;font-size:12px;font-weight:bold;margin-bottom:8px;">⭐ Doporučeno</div><br>'
            if job["priority"] else ""
        )
        border_color = "#f59e0b" if job["priority"] else "#2563eb"
        cards.append(f"""
        <tr><td style="padding:0 0 16px 0;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;border:1px solid #e2e8f0;border-left:5px solid {border_color};box-shadow:0 1px 3px rgba(0,0,0,0.05);">
            <tr><td style="padding:22px;">
              {star}
              <h2 style="margin:0 0 6px 0;font-size:18px;color:#0f172a;font-weight:700;">{e(job['title'])}</h2>
              {date_line}
              <div style="margin-bottom:12px;">{tags}</div>
              <p style="margin:0 0 16px 0;color:#334155;font-size:14px;line-height:1.6;">{e(desc)}</p>
              <a href="{e(job['link'])}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:14px;font-weight:600;">Otevřít inzerát</a>
              <div style="margin-top:16px;background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;padding:14px;">
                <div style="font-weight:bold;color:#065f46;margin-bottom:8px;font-size:13px;">
                  Připravený text k zkopírování
                </div>
                <div style="white-space:pre-wrap;color:#064e3b;font-size:13px;line-height:1.5;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;">{e(build_reply(job))}</div>
              </div>
            </td></tr>
          </table>
        </td></tr>""")

    top_count = sum(1 for j in jobs if j['priority'])
    return f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:20px 8px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" style="max-width:780px;width:100%;">
        <tr><td style="background:#0f172a;border-radius:14px 14px 0 0;padding:24px;">
          <div style="font-size:24px;font-weight:800;color:#ffffff;">Job Agent</div>
          <div style="font-size:14px;color:#94a3b8;margin-top:4px;">
            Nalezeno nabídek: {len(jobs)} · doporučených: {top_count} · {datetime.now().strftime('%d. %m. %Y')}
          </div>
        </td></tr>
        <tr><td style="background:#e2e8f0;padding:16px;border-radius:0 0 14px 14px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            {''.join(cards)}
          </table>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_email(subject: str, html_body: str) -> None:
    recipient = os.environ["RECIPIENT_EMAIL"]
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={
            "from": "Job Agent <onboarding@resend.dev>",
            "to": [recipient],
            "subject": subject,
            "html": html_body,
            "text": "Pro zobrazení nabídek použij e-mailového klienta s podporou HTML.",
        },
        timeout=30,
    )
    if not r.ok:
        print(r.text, file=sys.stderr)
    r.raise_for_status()


# ----------------------------- MAIN -----------------------------


def main() -> int:
    only_hour = os.environ.get("ONLY_AT_HOUR")
    if only_hour and datetime.now(ZoneInfo("Europe/Prague")).hour != int(only_hour):
        print("Mimo cílovou hodinu (pražský čas), končím.")
        return 0

    missing = [v for v in ("SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL", "RESEND_API_KEY") if not os.environ.get(v)]
    if missing:
        print(f"Chybí proměnné prostředí: {', '.join(missing)}", file=sys.stderr)
        return 1

    items = []
    for url in FEEDS:
        items.extend(fetch_feed(url))
    items.extend(fetch_alerts())
    print(f"Staženo položek: {len(items)}")

    jobs = filter_jobs(items)
    print(f"Po filtrování: {len(jobs)}")

    if not jobs:
        print("Žádné odpovídající nabídky, e-mail se neodesílá.")
        return 0

    top = sum(1 for j in jobs if j["priority"])
    send_email(f"Job Agent: {len(jobs)} nabídek ({top} doporučených)", build_html(jobs))
    print("E-mail odeslán.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
