# Porównywarka substancji pomocniczych

Aplikacja Flask porównująca substancje pomocnicze w odpowiednikach leków dopuszczonych do obrotu w Polsce
(np. Ibuprom Max, MIG 400 i Nurofen Forte: ibuprofen 400 mg). Dane pochodzą z Rejestru Produktów Leczniczych
i punktu 6.1 Charakterystyk Produktów Leczniczych (ChPL).

## Jak to działa

1. **Import** (`flask import-data`, raz w tygodniu z crona) pobiera listę leków z API RPL, a dla każdego leku
   sprawdza, czy zmienił się jego ChPL. Najpierw wysyła zapytanie HEAD (nazwa pliku, rozmiar, ETag), a PDF pobiera
   tylko po zmianie. Ten sam plik pod kilkoma rekordami (np. różne moce) jest parsowany raz (SHA-256).
2. **Parser** (`app/chpl_parser.py`) wycina punkt 6.1, pomija etykiety („Rdzeń tabletki”, „Korpus:”), rozbija
   mieszanki na składniki, tłumaczy numery E, sprowadza nazwy do słownika i przypisuje skład do konkretnej mocy
   („[Tylko dla 20 mg]”, „267 mg tabletka”).
3. **Odpowiedniki** to te same substancje czynne (pole `commonName`) w tej samej mocy po normalizacji
   (`app/power.py`: „0,4 g” = „400 mg”, „120 mg/5 ml” = „24 mg/ml”). Postać jest filtrem w porównaniu.
4. **Panel redaktora** (`/admin`) pokazuje kolejkę leków do weryfikacji z linkiem do PDF, listę nieznanych nazw
   (jedna decyzja, np. „synonim”, „etykieta do pomijania” albo „mieszanka”, od razu przetwarza ponownie wszystkie
   dokumenty w kolejce), słowniki, dziennik importów i ręczną edycję składu. Skład wpisany ręcznie nie jest
   nadpisywany przez import; gdy ChPL się zmieni, lek wraca do kolejki.

## Wyszukiwanie

Jedno pole przyjmuje:
- **nazwę**: od 3 liter pojawia się lista pasujących nazw (`/api/podpowiedzi`), najpierw te, które zaczynają się
  od wpisanego tekstu;
- **GTIN**: EAN-13 z kodu kreskowego albo GTIN-14, także poprzedzony znakiem `@` (lista znaków w `SCAN_PREFIXES`);
- **kod Data Matrix** z opakowania (GS1: `(01)` GTIN, `(17)` data ważności, `(10)` seria, `(21)` numer seryjny):
  aplikacja wyciąga z niego GTIN, także gdy skaner zgubił separatory GS albo dodał prefiks `]d2`.

Kody można wpisać skanerem działającym jak klawiatura (kod + Enter) albo zeskanować aparatem telefonu
(przycisk „Skanuj aparatem”, biblioteka ZXing ładowana dopiero po kliknięciu). Aparat działa tylko przez HTTPS
(lokalnie także na `localhost`/`127.0.0.1`). Rozpoznawanie kodów jest w `app/codes.py`.

Kody GTIN opakowań importer bierze z pola `gtin` odpowiedzi RPL (kody rozdzielone znakiem nowej linii), tak jak
robiło to stare repozytorium. W odpowiedzi wyszukiwarki RPL z 2026 r. tego pola nie było, więc trzeba ustalić,
skąd je teraz pobierać; bez niego wyszukiwanie po kodzie nie znajdzie leku.

## Uruchomienie lokalne

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_APP=wsgi.py SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
flask init-db                      # tabele + słowniki startowe z katalogu seed/
flask create-admin redaktor        # konto do panelu
flask import-data --limit 50       # próbny import 50 leków (pełny: bez --limit)
flask run
```

Bez dostępu do sieci można wczytać dane demonstracyjne:

```bash
flask load-registry tests/fixtures/rejestr_demo.json
flask load-chpl katalog/z/plikami   # pliki o nazwach Charakterystyka-<id>-....pdf
```

Testy: `pip install pytest && python -m pytest`.

## Komendy

| Komenda | Opis |
|---|---|
| `flask init-db` | tworzy tabele i wczytuje słowniki z `seed/` (nie nadpisuje istniejących wpisów) |
| `flask create-admin LOGIN` | zakłada konto redaktora albo zmienia hasło |
| `flask import-data [--limit N] [--bez-chpl]` | import z RPL |
| `flask reparse [--wszystkie]` | ponowne parsowanie zapisanych punktów 6.1 po zmianach w słownikach |
| `flask load-registry PLIK` / `flask load-chpl KATALOG` | wczytanie danych z plików (testy, praca bez sieci) |

## Serwer

- Gunicorn za Nginx: `gunicorn -w 2 -b 127.0.0.1:8000 wsgi:app`, HTTPS z Let's Encrypt.
- Baza: domyślnie SQLite w `instance/leki.db`; na serwerze PostgreSQL przez `DATABASE_URL`
  (np. `postgresql+psycopg://leki:haslo@localhost/leki`, wymaga `psycopg[binary]`).
- Cotygodniowy import (crontab użytkownika aplikacji), np. w nocy z niedzieli na poniedziałek:

  ```
  15 2 * * 1  cd /srv/leki && .venv/bin/flask --app wsgi import-data >> /var/log/leki-import.log 2>&1
  ```

  Pełny import pierwszy raz pobiera ok. 23 tys. PDF i przy odstępie 1 s między zapytaniami trwa kilka godzin.
  Wynik każdego uruchomienia widać w panelu (dziennik importów); brak nowego wpisu oznacza, że cron nie działa.
- Przed uruchomieniem produkcyjnym sprawdź warunki korzystania z RPL (serwis ma ograniczenia dla robotów
  w robots.txt) i nie zmniejszaj odstępu między zapytaniami (`RPL_DELAY_SECONDS`).

## Struktura

```
app/
  __init__.py      create_app, konfiguracja ze zmiennych środowiskowych
  models.py        Product, ChplDocument, Excipient, Synonym, ENumber, IgnoredLabel, Premix, ImportRun, User
  chpl_parser.py   parser punktu 6.1 (bez zależności od Flaska)
  power.py         klucz odpowiedników
  importer.py      import z RPL i wykrywanie zmian ChPL
  codes.py         rozpoznawanie nazwy, GTIN i kodu Data Matrix
  public.py        wyszukiwarka z podpowiedziami, strona leku, porównanie
  admin.py         panel redaktora
  cli.py           komendy flask
seed/              słowniki startowe (do przejrzenia przez farmaceutę)
tests/             testy; fixtures/sekcje_6_1.json to punkty 6.1 z 21 prawdziwych ChPL
```

## Słowniki startowe

`seed/substancje.json` to około 100 popularnych substancji z synonimami, przygotowanych na start, a nie
oficjalna lista. Warto, żeby przejrzał je farmaceuta. Przyjęto, że różne formy tej samej substancji
(np. „laktoza jednowodna” i „laktoza”) to jedna substancja, bo dla alergika różnica nie ma znaczenia;
zmienia się to w słowniku.

Serwis ma charakter informacyjny i nie zastępuje porady lekarza ani farmaceuty.
