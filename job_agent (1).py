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
]

CONTRACT_KEYWORDS = [
    "dpp", "dpč", "dohoda", "ičo", "ico", "živnost", "osvč", "poloviční",
    "part-time", "brigáda", "částečný",
]

# Krátká klíčová slova se hledají jako celá slova (aby "it" nesedlo na "city").
WHOLE_WORD = {"it", "dpp", "dpč", "ico", "ičo"}

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


def filter_jobs(items: list) -> list:
    result, seen = [], set()
    for it in items:
        if not it["link"] or it["link"] in seen:
            continue
        if it.get("trusted"):
            # Nabídky z alertů už prošly hledáním na portálu, klíčová slova se nekontrolují.
            roles, contracts = [], []
        else:
            text = f'{it["title"]} {it["description"]}'
            roles = find_keywords(text, ROLE_KEYWORDS)
            contracts = find_keywords(text, CONTRACT_KEYWORDS)
            if not roles or not contracts:
                continue
            if LOCATION_KEYWORDS and not find_keywords(text, list(LOCATION_KEYWORDS)):
                continue
        if not is_recent(it["pubdate"]):
            continue
        seen.add(it["link"])
        it["roles"], it["contracts"] = roles, contracts
        result.append(it)
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


def build_reply(job: dict) -> str:
    roles = set(job["roles"])
    focus = []
    if roles & {"webmaster", "e-shop", "eshop", "it", "administrator", "správce"}:
        focus.append("s tvorbou a správou webů, prací ve WordPressu a obsluhou e-shopů")
    if roles & {"marketing", "copywriter"}:
        focus.append("s tvorbou obsahu a podporou online marketingu")
    if roles & {"administrativ", "asistent", "podpora", "junior"}:
        focus.append("s administrativou a účetním programem Pohoda")
    generic = not focus
    if generic:
        focus.append("s weby, WordPressem, programem Pohoda a administrativou")
    extra = "" if generic else "Mám také zkušenosti s weby, WordPressem, programem Pohoda a administrativou, "

    return (
        "Dobrý den,\n\n"
        f"na základě Vašeho inzerátu „{job['title']}“ bych se rád ucházel o tuto pozici. "
        "Jsem student Obchodní akademie Praha – Vinohrady a hledám brigádu nebo "
        "částečný úvazek, který bych zvládal skloubit se školou.\n\n"
        f"Mám praktické zkušenosti {', '.join(focus)}. "
        f"{extra}"
        f"{'Jsem' if generic else 'jsem'} zodpovědný, rychle se učím a rád pracuji samostatně.\n\n"
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
            for t in job["roles"] + job["contracts"]
        )
        date_line = (
            f'<div style="color:#6b7280;font-size:12px;margin-bottom:8px;">{e(job["pubdate"])}</div>'
            if job["pubdate"] else ""
        )
        cards.append(f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin-bottom:20px;">
          <h2 style="margin:0 0 6px 0;font-size:18px;color:#111827;">{e(job['title'])}</h2>
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
      Nalezeno nabídek: {len(jobs)} · {datetime.now().strftime('%d. %m. %Y')}
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

    send_email(f"Job Agent: {len(jobs)} nových nabídek", build_html(jobs))
    print("E-mail odeslán.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
