# AI Digest Agent

Automatyczny agent, który raz dziennie zbiera najważniejsze wiadomości ze świata **AI / ML**, prosi model językowy o zwięzłą "pigułkę" i wysyła ją na **Slacka**. Uruchamiany codziennie przez GitHub Actions — bez własnego serwera.

## Jak to działa

1. **Zbieranie** — pobiera wpisy z listy źródeł RSS (badania: arXiv, DeepMind, Google Research, Hugging Face, BAIR; produkty/biznes: OpenAI News, The Batch, TechCrunch, VentureBeat, MIT Tech Review, Hacker News, Reddit).
2. **Filtrowanie i deduplikacja** — zawęża do ostatnich `LOOKBACK_HOURS` godzin i usuwa duplikaty (po URL i tytule).
3. **Streszczenie** — przekazuje wpisy do modelu, który wybiera 6–10 najważniejszych pozycji i zwraca ustrukturyzowany JSON.
4. **Wysyłka** — formatuje pigułkę w Slack Block Kit i publikuje przez Incoming Webhook.

## Uruchom własną kopię (fork)

Chcesz mieć tego agenta u siebie? Wszystko dzieje się w Twoim własnym repo — nie potrzebujesz serwera.

1. Kliknij **Fork** (prawy górny róg) — dostaniesz własną kopię repo.
2. W swoim forku wejdź w *Settings → Secrets and variables → Actions* i dodaj dwa sekrety:
   - `OPENAI_API_KEY` — Twój klucz API,
   - `SLACK_WEBHOOK_URL` — Twój Slack Incoming Webhook ([jak go utworzyć](https://api.slack.com/messaging/webhooks)).
3. Wejdź w zakładkę *Actions*, wybierz **Dzienna pigułka AI** i kliknij **Run workflow**, żeby przetestować. Dalej poleci codziennie o 06:00 UTC.

Ustawienia (język, okno czasowe, model, źródła RSS) dostosujesz zmiennymi środowiskowymi i listą `FEEDS` — szczegóły niżej.

## Struktura

```
.
├── daily_ai_digest.py            # główny skrypt (pipeline)
├── requirements.txt              # zależności Pythona
├── .env.example                  # szablon zmiennych środowiskowych
└── .github/workflows/
    └── daily-digest.yml          # harmonogram GitHub Actions (codziennie 06:00 UTC)
```

## Uruchomienie lokalne

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # uzupełnij wartości
set -a && source .env && set +a                     # załaduj zmienne (Linux/macOS)
python daily_ai_digest.py
```

## Uruchomienie automatyczne (GitHub Actions)

Workflow `daily-digest.yml` uruchamia się codziennie o **06:00 UTC** (oraz ręcznie z zakładki *Actions → Run workflow*).

W ustawieniach repo dodaj sekrety (*Settings → Secrets and variables → Actions*):

| Sekret | Opis |
| --- | --- |
| `OPENAI_API_KEY` | klucz API do modelu językowego |
| `SLACK_WEBHOOK_URL` | adres Slack Incoming Webhook |

## Konfiguracja (zmienne środowiskowe)

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | klucz API (wymagany) |
| `SLACK_WEBHOOK_URL` | — | Incoming Webhook Slacka (wymagany) |
| `DIGEST_LANGUAGE` | `polski` | język pigułki |
| `LOOKBACK_HOURS` | `24` | okno czasowe w godzinach |
| `MAX_ITEMS_TO_MODEL` | `60` | maks. liczba wpisów wysyłanych do modelu |
| `OPENAI_MODEL` | `gpt-5.5` | używany model |
| `OPENAI_REASONING_EFFORT` | `low` | `none`/`minimal`/`low`/`medium`/`high`; pusty = pomijany |

## Źródła RSS

Listę feedów edytujesz bezpośrednio w `daily_ai_digest.py` (stała `FEEDS`). Martwe/niedostępne feedy są pomijane i nie przerywają działania.

## Licencja

MIT — zobacz [LICENSE](LICENSE).
