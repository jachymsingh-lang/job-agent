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
ALERT_DAYS = 1          # kolik dní zpět se alerty čtou (1 =
