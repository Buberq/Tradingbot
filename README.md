# Bot tradingowy Kraken

Bot na rynek SPOT Kraken (tylko pary do EUR), z dwoma agentami LLM i Risk Managerem w kodzie.
Plan i etapy: dokument „Bot tradingowy Kraken – plan projektu i TODO”.

**Stan: Etap 2 w toku (łączność z Krakenem, tylko odczyt).** Kod klienta i wycena portfela
są gotowe i przetestowane na przykładowych odpowiedziach. Brakuje testu na żywo z serwera
z kluczem tylko do odczytu i whitelistą IP.

## Szybki start

Wymagany Python 3.12 lub nowszy.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp config/config.example.yaml config/config.yaml
cp .env.example .env            # sekrety uzupełnisz w kolejnych etapach

pytest                          # wszystkie testy muszą przejść
krakenbot check-config          # walidacja konfiguracji
krakenbot start                 # start i wpis do bazy data/krakenbot.sqlite3
krakenbot check-kraken          # test API Kraken: publiczne, a z kluczem także prywatne
krakenbot portfolio             # wycena portfela w EUR i snapshot w bazie (wymaga klucza)
```

## Kraken: zasady klienta

- Tylko HTTPS z weryfikacją certyfikatu (TLS 1.2+). Podpis zapytań zgodny z dokumentacją
  Krakena (test na oficjalnym przykładzie z dokumentacji).
- Metody wypłat i transferów są zablokowane w kodzie dla każdego klucza. Klient z kluczem
  tylko do odczytu odrzuca też metody handlowe, zanim zapytanie wyjdzie z serwera.
- Błędy chwilowe (przeciążenie, limity, 5xx, sieć) są ponawiane z backoffem. Błędy klucza,
  uprawnień, nonce i blokady nie są ponawiane, bo powtórki mogą zablokować klucz.
- Salda z sufiksem (np. `DOT.S`, `ETH.F`) to staking lub Earn: raportowane osobno,
  niewliczane do wartości portfela, którym zarządza bot.

## Tryby pracy

- `paper` (domyślny): symulacja na żywych cenach, zlecenia tylko w bazie.
- `live`: prawdziwe zlecenia. Wymaga **jednocześnie** `mode: live` w `config/config.yaml`
  i zmiennej `KRAKENBOT_ALLOW_LIVE=yes`. Bez obu bot odmawia startu. Od Etapu 3 dojdzie
  potwierdzenie na Telegramie.

## Zasady, których pilnuje kod

- Sekrety tylko w zmiennych środowiskowych. Plik YAML z sekcją `secrets` jest odrzucany,
  a wartości sekretów są maskowane w logach (`***`) i nie trafiają do bazy.
- Twarde reguły (tylko SPOT, tylko EUR, tylko zlecenia rynkowe i rynkowe SL/TP, bez dźwigni)
  są w `src/krakenbot/hard_rules.py`. Parametry ryzyka z `config.yaml` muszą mieścić się
  w granicach `risk_bounds`, a te w granicach z kodu.
- Zmiennymi środowiskowymi nie da się zmienić trybu ani parametrów ryzyka.
- Baza SQLite ma uprawnienia 600. Tabele audytowe (`audit_log`, `decisions`,
  `agent_debates`, `orders`, `order_events`, `portfolio_snapshots`, `capital_flows`,
  `risk_param_changes`) są tylko do dopisywania: UPDATE i DELETE blokują triggery w bazie.
- Stan bota (np. flaga pauzy) jest w tabeli `bot_state`, przetrwa restart, a każda zmiana
  trafia do audytu.

## Struktura

```
config/config.example.yaml   wzór konfiguracji
.env.example                 wzór zmiennych środowiskowych (sekrety)
src/krakenbot/
  config.py                  ładowanie i walidacja konfiguracji, przełącznik paper/live
  hard_rules.py              reguły i granice zapisane w kodzie
  logging_setup.py           logi z rotacją i maskowaniem sekretów
  retry.py                   ponawianie z wykładniczym backoffem
  db.py                      schemat bazy, migracje, audyt, stan bota
  main.py                    punkt wejścia (krakenbot start / check-config)
tests/                       pytest
.github/workflows/ci.yml     CI: ruff + pytest na Python 3.12 i 3.13
```
