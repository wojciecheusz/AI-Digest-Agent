#!/usr/bin/env python3
"""
Dzienna pigułka AI/ML → Slack.

Potok:
  1. Pobiera wpisy z listy źródeł RSS (badania + produkty/biznes).
  2. Filtruje do ostatnich N godzin i deduplikuje.
  3. Prosi Claude o zwięzłą pigułkę (JSON -> Slack Block Kit).
  4. Wysyła na Slacka przez Incoming Webhook.

Konfiguracja przez zmienne środowiskowe (patrz README):
  OPENAI_API_KEY          - klucz API OpenAI (wymagany)
  SLACK_WEBHOOK_URL       - Incoming Webhook Slacka (wymagany)
  DIGEST_LANGUAGE         - jezyk pigulki (domyslnie "polski")
  LOOKBACK_HOURS          - okno czasowe w godzinach (domyslnie 24)
  MAX_ITEMS_TO_MODEL      - ile wpisow max wyslac do modelu (domyslnie 60)
  OPENAI_MODEL            - model (domyslnie gpt-5.5; tanszy: gpt-5.4-mini)
  OPENAI_REASONING_EFFORT - none|minimal|low|medium|high (domyslnie low; pusty = pomijany)
"""

import os
import re
import sys
import json
import html
import time
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from openai import OpenAI

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
DIGEST_LANGUAGE = os.environ.get("DIGEST_LANGUAGE", "polski")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
MAX_ITEMS_TO_MODEL = int(os.environ.get("MAX_ITEMS_TO_MODEL", "80"))
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")

# Zrodla RSS. Klucz "waga" jest tylko informacyjny (badania vs produkt).
# Smialo dodawaj/usuwaj wpisy - martwe feedy sa pomijane, nie wywalaja skryptu.
FEEDS = [
    # --- Liderzy rynku / laboratoria (premiery modeli, produkty, zapowiedzi) ---
    {"name": "OpenAI News",        "url": "https://openai.com/news/rss.xml"},
    {"name": "Google DeepMind",    "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "Hugging Face Blog",  "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "NVIDIA Blog",        "url": "https://blogs.nvidia.com/feed/"},
    {"name": "Microsoft AI Blog",  "url": "https://blogs.microsoft.com/ai/feed/"},
    {"name": "Meta AI",            "url": "https://ai.meta.com/blog/rss/"},
    # --- Branża / newsy / analizy (popularne, szeroki odbiorca) ---
    {"name": "The Batch",          "url": "https://www.deeplearning.ai/the-batch/feed/"},
    {"name": "Import AI",          "url": "https://importai.substack.com/feed"},
    {"name": "The Decoder",        "url": "https://the-decoder.com/feed/"},
    {"name": "TechCrunch AI",      "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "VentureBeat AI",     "url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "The Verge AI",       "url": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"},
    {"name": "Wired AI",           "url": "https://www.wired.com/feed/tag/ai/latest/rss"},
    {"name": "Ars Technica AI",    "url": "https://arstechnica.com/ai/feed/"},
    {"name": "MIT Tech Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    {"name": "Simon Willison",     "url": "https://simonwillison.net/atom/everything/"},
    {"name": "Hacker News (front)","url": "https://hnrss.org/frontpage?points=100"},
    {"name": "r/LocalLLaMA",       "url": "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day"},
    # --- Badania / papers (uwzglednij wybiorczo, opisuj przystepnie) ---
    {"name": "arXiv cs.AI",        "url": "http://export.arxiv.org/rss/cs.AI"},
    {"name": "arXiv cs.LG",        "url": "http://export.arxiv.org/rss/cs.LG"},
    {"name": "arXiv cs.CL",        "url": "http://export.arxiv.org/rss/cs.CL"},
    {"name": "Google Research",    "url": "https://research.google/blog/rss/"},
    {"name": "BAIR (Berkeley)",    "url": "https://bair.berkeley.edu/blog/feed.xml"},
]

USER_AGENT = "Mozilla/5.0 (compatible; DailyAIDigestBot/1.0; +https://example.com)"


# ---------------------------------------------------------------------------
# 1. Pobieranie
# ---------------------------------------------------------------------------

def _clean_text(raw: str, limit: int = 400) -> str:
    """Usuwa HTML/nadmiarowe spacje i przycina."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)          # tagi HTML
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(feed: dict, cutoff: datetime) -> list[dict]:
    """Pobiera jeden feed. Zwraca liste wpisow z ostatnich LOOKBACK_HOURS."""
    items = []
    try:
        parsed = feedparser.parse(feed["url"], agent=USER_AGENT)
    except Exception as exc:  # noqa: BLE001 - feed nie moze zabic calego runu
        print(f"  [pomijam] {feed['name']}: blad parsowania ({exc})", file=sys.stderr)
        return items

    if getattr(parsed, "bozo", 0) and not parsed.entries:
        print(f"  [pomijam] {feed['name']}: brak wpisow / feed niedostepny", file=sys.stderr)
        return items

    for entry in parsed.entries:
        published = _entry_datetime(entry)
        # Jesli feed nie podaje daty, bierzemy wpis mimo to (moze byc swiezy).
        if published is not None and published < cutoff:
            continue
        title = _clean_text(entry.get("title", ""), 300)
        if not title:
            continue
        items.append({
            "source": feed["name"],
            "title": title,
            "url": entry.get("link", ""),
            "summary": _clean_text(entry.get("summary", entry.get("description", "")), 700),
            "published": published.isoformat() if published else "",
        })
    print(f"  [ok] {feed['name']}: {len(items)} wpisow", file=sys.stderr)
    return items


def collect_all() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    print(f"Zbieram wpisy z ostatnich {LOOKBACK_HOURS}h (od {cutoff.isoformat()})...",
          file=sys.stderr)
    all_items = []
    for feed in FEEDS:
        all_items.extend(fetch_feed(feed, cutoff))
        time.sleep(0.3)  # grzecznosc wobec serwerow
    return all_items


# ---------------------------------------------------------------------------
# 2. Deduplikacja
# ---------------------------------------------------------------------------

def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def dedupe(items: list[dict]) -> list[dict]:
    """Usuwa duplikaty po URL i po znormalizowanym tytule."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out = []
    for it in items:
        url_key = it["url"].split("?")[0].rstrip("/")
        title_key = _norm(it["title"])
        if url_key and url_key in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# 3. Streszczenie przez Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Jesteś redaktorem dziennego briefingu o AI i całej branży technologicznej wokół niej.
Dostajesz surową listę wpisów (tytuł, źródło, opis, link) z ostatniej doby — od publikacji naukowych po newsy produktowe i biznesowe.

Twoje zadanie:
- Wybierz od 8 do 12 NAJWAŻNIEJSZYCH rzeczy. Odsiej szum, drobne aktualizacje i clickbait.
- WAŻNE — dobór treści: pisz dla osoby zainteresowanej branżą, nie tylko dla naukowca. Priorytet mają: ruchy liderów rynku (OpenAI, Anthropic, Google/DeepMind, Meta, Microsoft, NVIDIA, xAI, Mistral, Amazon i in.), premiery i aktualizacje modeli, nowe produkty i funkcje, finansowanie, przejęcia i zatrudnienia, zmiany w regulacjach oraz szersze trendy w branży. Przełomowe badania nadal uwzględniaj, ale opisuj je przystępnie (co z nich wynika w praktyce) i pomijaj wąskie, czysto techniczne papers bez szerszego znaczenia. Docelowo większość pozycji powinna dotyczyć rynku/produktów/branży, a nie samych publikacji naukowych.
- Zadbaj o różnorodność kategorii i źródeł; grupuj podobne wątki i nie powielaj tej samej wiadomości.
- Dla każdej pozycji napisz OBSZERNE, konkretne streszczenie: 3–5 zdań, które realnie opisują treść — co dokładnie się wydarzyło, najważniejsze szczegóły i liczby, kto za tym stoi i jaki jest kontekst. Unikaj ogólników i jednozdaniowych skrótów. Dodaj też 1–2 zdania „dlaczego to ważne".
- Dla każdej pozycji dodaj też pole "tldr": JEDNO zdanie (maksymalnie 15 wyrazów) z maksymalnie skondensowaną, konkretną informacją z tej pozycji. To zdanie trafi do listy TL;DR na początku pigułki, więc musi samodzielnie nieść sedno newsa. Bez „w tym artykule", bez wielokropków, bez łączenia dwóch newsów.
- Pisz w języku: {language}. Ton: rzeczowy, przystępny i konkretny — bez marketingowego żargonu i bez akademickiego przegadania.

Zwróć WYŁĄCZNIE poprawny JSON (bez ```), w formacie:
{{
  "intro": "1-2 zdania podsumowujące najważniejsze wątki dnia",
  "items": [
    {{
      "category": "Rynek|Modele|Produkty|Biznes|Badania|Narzędzia|Inne",
      "title": "krótki tytuł",
      "tldr": "jedno zdanie, max 15 wyrazów, sedno newsa",
      "summary": "3-5 zdań opisujących treść",
      "why": "dlaczego to ważne (1-2 zdania)",
      "url": "link źródłowy"
    }}
  ]
}}"""


def summarize(items: list[dict]) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Ograniczamy liczbe wpisow wyslanych do modelu (kontrola tokenow/kosztu).
    payload_items = items[:MAX_ITEMS_TO_MODEL]
    compact = [
        {"source": it["source"], "title": it["title"],
         "summary": it["summary"], "url": it["url"]}
        for it in payload_items
    ]
    user_content = (
        f"Oto {len(compact)} wpisów z ostatniej doby. Zrób z nich pigułkę:\n\n"
        + json.dumps(compact, ensure_ascii=False, indent=1)
    )

    # Budujemy argumenty. reasoning_effort dodajemy tylko jesli ustawiony
    # (nie wszystkie modele go wspieraja - pusta wartosc = pomijamy).
    kwargs = dict(
        model=OPENAI_MODEL,
        max_completion_tokens=6000,
        response_format={"type": "json_object"},  # tryb JSON - pewne parsowanie
        messages=[
            {"role": "developer", "content": SYSTEM_PROMPT.format(language=DIGEST_LANGUAGE)},
            {"role": "user", "content": user_content},
        ],
    )
    if OPENAI_REASONING_EFFORT.strip():
        kwargs["reasoning_effort"] = OPENAI_REASONING_EFFORT.strip()

    resp = client.chat.completions.create(**kwargs)
    text = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("BLAD: model nie zwrocil poprawnego JSON. Surowa odpowiedz:\n" + text,
              file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# 4. Wysylka na Slacka (Block Kit)
# ---------------------------------------------------------------------------

CATEGORY_EMOJI = {
    "Rynek": "📈", "Badania": "🔬", "Modele": "🧠", "Produkty": "🚀",
    "Biznes": "💼", "Narzędzia": "🛠️", "Inne": "📌",
}


def build_slack_blocks(digest: dict) -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"🧪 Dzienna pigułka AI — {today}"}},
    ]
    intro = digest.get("intro", "").strip()
    if intro:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"_{intro}_"}})

    items = digest.get("items", [])

    # Sekcja TL;DR: po jednym krotkim zdaniu (max ~15 wyrazow) na kazdy artykul,
    # w tej samej kolejnosci co szczegolowe pozycje ponizej.
    tldr_lines = []
    for it in items:
        line = it.get("tldr", "").strip() or it.get("title", "").strip()
        if line:
            tldr_lines.append(f"• {line}")
    if tldr_lines:
        tldr_text = "*⚡ TL;DR*\n" + "\n".join(tldr_lines)
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": tldr_text[:2900]}})

    blocks.append({"type": "divider"})

    for it in items:
        emoji = CATEGORY_EMOJI.get(it.get("category", "Inne"), "📌")
        title = it.get("title", "").strip()
        url = it.get("url", "").strip()
        summary = it.get("summary", "").strip()
        why = it.get("why", "").strip()

        title_line = f"*<{url}|{title}>*" if url else f"*{title}*"
        text = f"{emoji} {title_line}\n{summary}"
        if why:
            text += f"\n> _Dlaczego ważne:_ {why}"
        # Slack: limit ~3000 znakow na sekcje
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": text[:2900]}})

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "🤖 Wygenerowano automatycznie na podstawie źródeł RSS z ostatniej doby.",
        }],
    })
    return blocks


def post_to_slack(digest: dict) -> None:
    blocks = build_slack_blocks(digest)
    # Slack pozwala max 50 blokow na wiadomosc.
    payload = {
        "text": digest.get("intro", "Dzienna pigułka AI"),  # fallback do powiadomien
        "blocks": blocks[:50],
    }
    r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Slack odrzucil wiadomosc: {r.status_code} {r.text}")
    print("Wyslano na Slacka.", file=sys.stderr)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    missing = [k for k, v in {"OPENAI_API_KEY": OPENAI_API_KEY,
                              "SLACK_WEBHOOK_URL": SLACK_WEBHOOK_URL}.items() if not v]
    if missing:
        print(f"Brak zmiennych srodowiskowych: {', '.join(missing)}", file=sys.stderr)
        return 1

    items = collect_all()
    print(f"Zebrano lacznie: {len(items)} wpisow.", file=sys.stderr)
    items = dedupe(items)
    print(f"Po deduplikacji: {len(items)} wpisow.", file=sys.stderr)

    if not items:
        print("Brak wpisow w oknie czasowym - nic nie wysylam.", file=sys.stderr)
        return 0

    digest = summarize(items)
    print(f"Model wybral {len(digest.get('items', []))} pozycji.", file=sys.stderr)
    post_to_slack(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
