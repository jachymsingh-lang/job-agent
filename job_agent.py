#!/usr/bin/env python3
"""Job Agent – hlídání pracovních příležitostí v Praze přes RSS + e-mail přes Gmail SMTP."""

import html
import imaplib
import os
import re
import smtplib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import requests

# ----------------------------- KONFIGURACE -----------------------------

FEEDS = [
    "https://www.startupjobs.cz/nabidky.rss",
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

# Krátká klíčová slova se hledají jako celá slova (aby "it" nesedlo na "city").
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
# Odesílatelé se hledají v Gmailu jako from:(...). Případně sem dopiš další doménu.
ALERT_SENDERS = ["prace.cz", "jobs.cz", "almacareer"]
ALERT_DAYS = 1          # kolik dní zpět se alerty čtou (1 = posledních 24 hodin)
MAX_ALERT_ITEMS = 40    # pojistka proti záplavě nesmyslných odkazů

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
]

# E-maily s těmito slovy v předmětu se přeskočí (potvrzení vlastních odpovědí apod.).
SKIP_SUBJECT_WORDS = ["hlásíte se", "hlasite se", "odeslali jste", "potvrzení", "potvrzeni"]

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
        # U alertů se hodnotí jen název (popis je společný předmět e-mailu).
        text = it["title"] if trusted else f'{it["title"]} {it["description"]}'

        if is_excluded(text):
            continue

        if trusted:
            # Nabídky z alertů už prošly hledáním na portálu, klíčová slova se nekontrolují.
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

    # Doporučené (víc shod s tím, co hledáš) nahoru; při shodě zůstává pořadí z feedu.
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


def fetch_alerts() -> list:
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_PASSWORD"]
    query = f'from:({" OR ".join(ALERT_SENDERS)}) newer_than:{ALERT_DAYS}d'
    items, seen_titles = [], set()

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(sender, password)
            imap.select("INBOX", readonly=True)
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


# ----------------------------- HTML E-MAIL -----------------------------


def build_html(jobs: list) -> str:
    e = html.escape
    cards = []
    for job in jobs:
        desc = job["description"]
        if len(desc) > 400:
            desc = desc[:400].rsplit(" ", 1)[0] + "…"
        tags = "".join(
            f'<span style="display:inline-block;background:#eef2ff;color:#3730a3;'
            f'border-radius:12px;padding:2px 10px;margin:2px 4px 2px 0;font-size:12px;">{e(t)}</span>'
            for t in job["tags"]
        )
        date_line = (
            f'<div style="color:#6b7280;font-size:12px;margin-bottom:8px;">{e(job["pubdate"])}</div>'
            if job["pubdate"] else ""
        )
        star = (
            '<div style="display:inline-block;background:#fef3c7;color:#92400e;border-radius:6px;'
            'padding:2px 8px;font-size:12px;font-weight:bold;margin-bottom:8px;">⭐ Doporučeno</div><br>'
            if job["priority"] else ""
        )
        cards.append(f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin-bottom:20px;">
          {star}<h2 style="margin:0 0 6px 0;font-size:18px;color:#111827;">{e(job['title'])}</h2>
          {date_line}
          <div style="margin-bottom:10px;">{tags}</div>
          <p style="margin:0 0 12px 0;color:#374151;font-size:14px;line-height:1.5;">{e(desc)}</p>
          <a href="{e(job['link'])}" style="display:inline-block;background:#2563eb;color:#ffffff;
             text-decoration:none;padding:8px 16px;border-radius:6px;font-size:14px;">Otevřít inzerát</a>
          <div style="margin-top:16px;background:#ecfdf5;border:1px solid #6ee7b7;border-radius:8px;padding:14px;">
            <div style="font-weight:bold;color:#065f46;margin-bottom:8px;font-size:13px;">
              Připravený text k zkopírování
            </div>
            <div style="white-space:pre-wrap;color:#064e3b;font-size:14px;line-height:1.5;">{e(build_reply(job))}</div>
          </div>
        </div>""")

    return f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:680px;margin:0 auto;padding:24px;">
    <h1 style="color:#111827;font-size:22px;margin:0 0 4px 0;">Job Agent</h1>
    <p style="color:#6b7280;margin:0 0 24px 0;font-size:14px;">
      Nalezeno nabídek: {len(jobs)} · doporučených: {sum(1 for j in jobs if j['priority'])} · {datetime.now().strftime('%d. %m. %Y')}
    </p>
    {''.join(cards)}
  </div>
</body></html>"""


def send_email(subject: str, html_body: str) -> None:
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText("Pro zobrazení nabídek použij e-mailového klienta s podporou HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())


# ----------------------------- MAIN -----------------------------


def main() -> int:
    # Naplánovaný běh se spouští 2x denně (kvůli letnímu/zimnímu času); odešle se jen ten v 19:xx pražského času.
    only_hour = os.environ.get("ONLY_AT_HOUR")
    if only_hour and datetime.now(ZoneInfo("Europe/Prague")).hour != int(only_hour):
        print("Mimo cílovou hodinu (pražský čas), končím.")
        return 0

    missing = [v for v in ("SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL") if not os.environ.get(v)]
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
