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
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "8"))          # cap wpisow na jedno zrodlo
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")
MODEL_MAX_RETRIES = int(os.environ.get("MODEL_MAX_RETRIES", "3"))  # ponawianie wywolania modelu

# Historia wyslanych artykulow (dedup miedzy biegami). Plik trzymany w cache
# GitHub Actions - patrz krok "Przywroc historie wyslanych" w workflow.
SENT_HISTORY_FILE = os.environ.get("SENT_HISTORY_FILE", ".state/sent.json")
SENT_HISTORY_DAYS = int(os.environ.get("SENT_HISTORY_DAYS", "7"))

# Zrodla RSS - wyselekcjonowana dwunastka (jakosc + roznorodnosc rol).
# Smialo dodawaj/usuwaj wpisy - martwe feedy sa pomijane, nie wywalaja skryptu.
FEEDS = [
    # --- Pierwsze zrodlo: laboratoria (premiery modeli i produktow wprost) ---
    {"name": "OpenAI News",        "url": "https://openai.com/news/rss.xml"},
    {"name": "Google DeepMind",    "url": "https://deepmind.google/blog/rss.xml"},
    # --- Dziennikarstwo: analizy, technika, startupy, sledztwa, enterprise ---
    {"name": "MIT Tech Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    {"name": "Ars Technica AI",    "url": "https://arstechnica.com/ai/feed/"},
    {"name": "TechCrunch AI",      "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "404 Media",          "url": "https://www.404media.co/rss/"},
    {"name": "IEEE Spectrum AI",   "url": "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss"},
    {"name": "The Register AI/ML", "url": "https://www.theregister.com/software/ai_ml/headlines.atom"},
    {"name": "Wired AI",           "url": "https://www.wired.com/feed/tag/ai/latest/rss"},
    # --- Newslettery ML (kuratorowany przeglad najwazniejszego) ---
    {"name": "The Batch",          "url": "https://www.deeplearning.ai/the-batch/feed/"},
    {"name": "Import AI",          "url": "https://importai.substack.com/feed"},
    # --- Spolecznosc / agregator ---
    {"name": "Hacker News (front)","url": "https://hnrss.org/frontpage?points=100"},
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


def _url_key(url: str) -> str:
    """Kanoniczny klucz URL (bez parametrow i koncowego /) - uzywany do
    deduplikacji, historii wyslanych i mapowania zrodel."""
    return (url or "").split("?")[0].rstrip("/")


def dedupe(items: list[dict]) -> list[dict]:
    """Usuwa duplikaty po URL i po znormalizowanym tytule."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out = []
    for it in items:
        url_key = _url_key(it.get("url", ""))
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
# 2b. Historia wyslanych (dedup miedzy biegami) + sprawiedliwy dobor wejscia
# ---------------------------------------------------------------------------

def load_sent_history(path: str) -> dict:
    """Wczytuje {url_key: 'YYYY-MM-DD'} i przycina wpisy starsze niz
    SENT_HISTORY_DAYS. Brak/uszkodzony plik = pusta historia."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SENT_HISTORY_DAYS)).date().isoformat()
    # Daty w formacie ISO (YYYY-MM-DD) porownuja sie poprawnie leksykalnie.
    return {k: v for k, v in data.items() if isinstance(v, str) and v >= cutoff}


def save_sent_history(path: str, history: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, sort_keys=True)


def filter_unseen(items: list[dict], history: dict) -> list[dict]:
    """Odrzuca wpisy, ktorych URL byl juz wyslany w oknie historii."""
    return [it for it in items if _url_key(it.get("url", "")) not in history]


def select_for_model(items: list[dict]) -> list[dict]:
    """Ogranicza liczbe wpisow na zrodlo (MAX_PER_FEED) i przeplata zrodla
    round-robin, zeby zadne pojedyncze zrodlo nie zdominowalo puli i zeby
    zrodla z konca listy nie wypadaly przed cieciem do MAX_ITEMS_TO_MODEL."""
    by_source: dict[str, list[dict]] = {}
    for it in items:
        by_source.setdefault(it.get("source", "?"), []).append(it)
    for src in by_source:
        by_source[src] = by_source[src][:MAX_PER_FEED]

    out: list[dict] = []
    idx = 0
    while True:
        added = False
        for lst in by_source.values():
            if idx < len(lst):
                out.append(lst[idx])
                added = True
        if not added:
            break
        idx += 1
    return out[:MAX_ITEMS_TO_MODEL]


def attach_sources(digest: dict, source_map: dict) -> None:
    """Uzupelnia pole 'source' w pozycjach digestu na podstawie mapy
    url_key -> source zbudowanej z pobranych wpisow (bez ufania modelowi)."""
    for it in digest.get("items", []):
        if not it.get("source"):
            src = source_map.get(_url_key(it.get("url", "")))
            if src:
                it["source"] = src


# ---------------------------------------------------------------------------
# 3. Streszczenie przez Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Jesteś redaktorem dziennego briefingu o AI i całej branży technologicznej wokół niej.
Dostajesz surową listę wpisów (tytuł, źródło, opis, link) z ostatniej doby — od publikacji naukowych po newsy produktowe i biznesowe.

Twoje zadanie:
- Wybierz od 8 do 12 NAJWAŻNIEJSZYCH rzeczy. Odsiej szum, drobne aktualizacje i clickbait.
- WAŻNE — dobór treści: pisz dla osoby zainteresowanej branżą, nie tylko dla naukowca. Priorytet mają: ruchy liderów rynku (OpenAI, Anthropic, Google/DeepMind, Meta, Microsoft, NVIDIA, xAI, Mistral, Amazon i in.), premiery i aktualizacje modeli, nowe produkty i funkcje, finansowanie, przejęcia i zatrudnienia, zmiany w regulacjach oraz szersze trendy w branży. Przełomowe badania nadal uwzględniaj, ale opisuj je przystępnie (co z nich wynika w praktyce) i pomijaj wąskie, czysto techniczne papers bez szerszego znaczenia. Docelowo większość pozycji powinna dotyczyć rynku/produktów/branży, a nie samych publikacji naukowych.
- Zadbaj o różnorodność kategorii i źródeł; grupuj podobne wątki i nie powielaj tej samej wiadomości.
- Każdej pozycji przypisz DOKŁADNIE JEDNĄ kategorię z tego zamkniętego zbioru (użyj dokładnie tej pisowni): „Modele i badania", „Produkty i narzędzia", „Biznes i rynek", „Regulacje i społeczeństwo", „Inne". Kategorii „Inne" używaj tylko gdy nic innego nie pasuje.
- Dla każdej pozycji napisz OBSZERNE, konkretne streszczenie: 3–5 zdań, które realnie opisują treść — co dokładnie się wydarzyło, najważniejsze szczegóły i liczby, kto za tym stoi i jaki jest kontekst. Unikaj ogólników i jednozdaniowych skrótów. Dodaj też 1–2 zdania „dlaczego to ważne".
- Dla każdej pozycji dodaj też pole "tldr": JEDNO zdanie (maksymalnie 15 wyrazów) z maksymalnie skondensowaną, konkretną informacją z tej pozycji. To zdanie trafi do listy TL;DR na początku pigułki, więc musi samodzielnie nieść sedno newsa. Bez „w tym artykule", bez wielokropków, bez łączenia dwóch newsów.
- Pisz w języku: {language}. Ton: rzeczowy, przystępny i konkretny — bez marketingowego żargonu i bez akademickiego przegadania.

Zwróć WYŁĄCZNIE poprawny JSON (bez ```), w formacie:
{{
  "intro": "1-2 zdania podsumowujące najważniejsze wątki dnia",
  "items": [
    {{
      "category": "Modele i badania|Produkty i narzędzia|Biznes i rynek|Regulacje i społeczeństwo|Inne",
      "title": "krótki tytuł",
      "tldr": "jedno zdanie, max 15 wyrazów, sedno newsa",
      "summary": "3-5 zdań opisujących treść",
      "why": "dlaczego to ważne (1-2 zdania)",
      "url": "link źródłowy"
    }}
  ]
}}"""


def _valid_digest(data) -> bool:
    """Minimalna walidacja struktury odpowiedzi modelu."""
    return (isinstance(data, dict)
            and isinstance(data.get("items"), list)
            and len(data["items"]) > 0
            and all(isinstance(it, dict) for it in data["items"]))


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

    # Ponawiamy wywolanie przy bledzie API / niepoprawnym lub pustym JSON.
    last_err = None
    for attempt in range(1, MODEL_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            data = json.loads(text)
            if not _valid_digest(data):
                raise ValueError("odpowiedz nie zawiera poprawnej, niepustej listy 'items'")
            return data
        except Exception as exc:  # noqa: BLE001 - lapiemy, by ponowic probe
            last_err = exc
            print(f"  [proba {attempt}/{MODEL_MAX_RETRIES}] blad modelu: {exc}",
                  file=sys.stderr)
            if attempt < MODEL_MAX_RETRIES:
                time.sleep(2 * attempt)  # prosty backoff: 2s, 4s, ...
    raise RuntimeError(f"Model zawiodl po {MODEL_MAX_RETRIES} probach: {last_err}")


# ---------------------------------------------------------------------------
# 4. Wysylka na Slacka (Block Kit)
# ---------------------------------------------------------------------------

# Kolejnosc, w jakiej kategorie pojawiaja sie w poscie (i w TL;DR).
CATEGORY_ORDER = [
    "Modele i badania",
    "Produkty i narzędzia",
    "Biznes i rynek",
    "Regulacje i społeczeństwo",
    "Inne",
]

CATEGORY_EMOJI = {
    "Modele i badania": "🧠",
    "Produkty i narzędzia": "🚀",
    "Biznes i rynek": "💼",
    "Regulacje i społeczeństwo": "⚖️",
    "Inne": "📌",
}


def group_by_category(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Grupuje pozycje wg CATEGORY_ORDER (nieznane kategorie -> 'Inne').
    Zwraca liste (kategoria, pozycje) tylko dla niepustych grup, w ustalonej
    kolejnosci. Kolejnosc pozycji wewnatrz grupy zachowana z wejscia."""
    buckets: dict[str, list[dict]] = {cat: [] for cat in CATEGORY_ORDER}
    for it in items:
        cat = it.get("category", "").strip()
        if cat not in buckets:
            cat = "Inne"
        buckets[cat].append(it)
    return [(cat, buckets[cat]) for cat in CATEGORY_ORDER if buckets[cat]]


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

    # Grupujemy pozycje wg kategorii; ta sama kolejnosc obowiazuje
    # zarowno w TL;DR, jak i w czesci szczegolowej ponizej.
    grouped = group_by_category(digest.get("items", []))
    ordered_items = [it for _, group in grouped for it in group]

    # Sekcja TL;DR: po jednym krotkim zdaniu (max ~15 wyrazow) na kazdy artykul,
    # podlinkowanym do artykulu (jeden klik prosto do tekstu).
    tldr_lines = []
    for it in ordered_items:
        sentence = it.get("tldr", "").strip() or it.get("title", "").strip()
        if not sentence:
            continue
        url = it.get("url", "").strip()
        tldr_lines.append(f"• <{url}|{sentence}>" if url else f"• {sentence}")
    if tldr_lines:
        tldr_text = "*⚡ TL;DR*\n" + "\n".join(tldr_lines)
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": tldr_text[:2900]}})

    blocks.append({"type": "divider"})

    # Czesc szczegolowa: pozycje pogrupowane pod naglowkiem kategorii.
    for category, group in grouped:
        emoji = CATEGORY_EMOJI.get(category, "📌")
        blocks.append({"type": "header",
                       "text": {"type": "plain_text", "text": f"{emoji} {category}"}})
        for it in group:
            title = it.get("title", "").strip()
            url = it.get("url", "").strip()
            summary = it.get("summary", "").strip()
            why = it.get("why", "").strip()
            source = it.get("source", "").strip()

            title_line = f"*<{url}|{title}>*" if url else f"*{title}*"
            if source:
                title_line += f"  _via {source}_"
            text = f"{title_line}\n{summary}"
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


def post_failure_notice(reason: str) -> None:
    """Krotki komunikat awaryjny na kanal, gdy pigulka nie powstala -
    lepsze niz cicha porazka. Bledy wysylki celowo ignorujemy."""
    try:
        requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": f"⚠️ Dzienna pigułka AI nie powstała dziś: {reason}"},
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - nie maskujemy oryginalnego bledu
        pass


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

    # Mapa url -> zrodlo (do atrybucji pozycji w poscie).
    source_map = {_url_key(it.get("url", "")): it.get("source", "")
                  for it in items if it.get("url")}

    # Tweak #1: odfiltruj artykuly juz wyslane w ostatnich dniach.
    # SENT_HISTORY_DAYS=0 wylacza dedup miedzy biegami (przydatne przy testach).
    history: dict = {}
    if SENT_HISTORY_DAYS > 0:
        history = load_sent_history(SENT_HISTORY_FILE)
        before = len(items)
        items = filter_unseen(items, history)
        print(f"Po odfiltrowaniu juz wyslanych: {len(items)} (pominieto {before - len(items)}).",
              file=sys.stderr)
        if not items:
            print("Nic nowego wzgledem ostatnich dni - nic nie wysylam.", file=sys.stderr)
            return 0

    # Tweak #3: sprawiedliwy dobor wejscia (cap na zrodlo + przeplatanie).
    selected = select_for_model(items)
    print(f"Do modelu: {len(selected)} wpisow (max {MAX_PER_FEED}/zrodlo).", file=sys.stderr)

    # Tweak #2: przy trwalym bledzie modelu nie milczymy - dajemy znac na kanal.
    try:
        digest = summarize(selected)
    except Exception as exc:  # noqa: BLE001
        print(f"BLAD: {exc}", file=sys.stderr)
        post_failure_notice(str(exc)[:300])
        return 1

    print(f"Model wybral {len(digest.get('items', []))} pozycji.", file=sys.stderr)

    # Tweak #4: uzupelnij zrodla na podstawie mapy (bez ufania modelowi).
    attach_sources(digest, source_map)

    post_to_slack(digest)

    # Tweak #1: zapisz wyslane URL-e do historii (na kolejne biegi).
    if SENT_HISTORY_DAYS > 0:
        today = datetime.now(timezone.utc).date().isoformat()
        for it in digest.get("items", []):
            key = _url_key(it.get("url", ""))
            if key:
                history[key] = today
        save_sent_history(SENT_HISTORY_FILE, history)
        print(f"Historia wyslanych: {len(history)} URL-i.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
