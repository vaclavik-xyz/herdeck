# D200 spinner-stall — the real fix: neutralize strmdck's retry-loop sleep

**Status:** design approved + on-device verified · 2026-07-01
**Type:** performance bugfix (third-party monkeypatch)
**Scope:** `src/herdeck/driver/d200.py` (+ its test)
**Follows:** [[2026-06-30-d200-tile-write-diff-design]] (merged, but did NOT fix the freeze — see below)

## Problém / co měření odhalilo

Předchozí fix (per-index write-diff, merged do main) zásek **neodstranil**. On-device měření na macbench D200 (instrumentace z té fáze) ukázalo skutečnou příčinu:

- Každý **spinner zápis (2–6 dlaždic) trvá 400–800 ms**, jeden i **5889 ms** — a děje se každých ~0,4 s (`render_working` na každém ticku). ~40 worker-bloků >250 ms za 90 s.
- `compress_folder` = **1,3 ms** (zanedbatelné). Rozdíl je strmdck `_prepare_zip` **retry smyčka**: deck má firmware bug (bytes `0x00`/`0x7c` na hranicích paketů 1016+1024k glitchnou displej), strmdck to obchází přebalováním zipu s náhodným balastem, a mezi pokusy spí `time.sleep(0.05)` (ulanzi_d200.py). Smyčka iteruje ~8–15× na zápis → 400–800 ms čistě v sleepech.
- **Write-diff to nemůže opravit:** spinner dlaždice nejdou diffnout pryč (fáze se mění každý frame) a cena je **per-zápis** (retry sleep), ne per-velikost.

Ten `time.sleep(0.05)` je **čistě CPU prodleva mezi pokusy o přebalení** — nečeká na žádné I/O ani na zařízení (USB zápis jde až po `_prepare_zip`). Lze ho beztrestně zkrátit na nulu.

## Ověření fixu (on-device, hotovo)

Dočasná neutralizace `time.sleep(0.05)` → `sleep(0)` na macbench, 60 s capture (172 zápisů = deck aktivně renderoval):

| | PŘED (sleep 0,05) | PO (sleep=0) |
|---|---|---|
| nejhorší zápis | 400–800 ms, jeden 5889 ms | **101 ms** |
| worker-block >250 ms | ~40× / 90 s, až 5889 ms | nejhorší **258 ms**, žádný multi-sekundový |

Fix potvrzen. Plně reverzibilní, deck po revertu beze změny.

## Fix

Za běhu **neutralizovat `time.sleep` v strmdck ulanzi modulu** z herdeck D200 driveru. Při otevření zařízení (`_open_device`, kde se strmdck importuje) se modulu `strmdck.devices.ulanzi_d200` vymění atribut `time` za tenký proxy, jehož `sleep()` je no-op, a **všechny ostatní `time.*` (např. `time.time()` použité jinde v modulu) projdou na reálný modul**. Retry smyčka pak iteruje bez prodlevy (~tens of ms místo 400–800 ms).

```python
class _SleeplessTime:
    """Proxy time module: sleep() is a no-op, everything else passes through."""
    def __init__(self, real): self._real = real
    def sleep(self, *_a, **_k): return None
    def __getattr__(self, name): return getattr(self._real, name)

def _neutralize_retry_sleep() -> None:
    try:
        import strmdck.devices.ulanzi_d200 as ud
        if isinstance(ud.time, _SleeplessTime):  # idempotent
            return
        ud.time = _SleeplessTime(ud.time)
    except Exception:
        log.warning("could not neutralize strmdck retry sleep; D200 spinner may stall")
```

Voláno v `_open_device` hned po `from strmdck.devices.ulanzi_d200 import UlanziD200Device`.

## Komponenty / soubory

- `src/herdeck/driver/d200.py` (změna): `_SleeplessTime` třída + `_neutralize_retry_sleep()` funkce (modul-level) + volání v `_open_device`.
- `tests/test_d200_panel.py` (test): unit test funkce přes injekci fake `strmdck.devices.ulanzi_d200` do `sys.modules` (lokální venv strmdck nemá).

## Klíčová rozhodnutí / robustnost

- **Idempotence:** `isinstance(ud.time, _SleeplessTime)` guard — opakované volání (re-open, více decků) nepřebaluje.
- **Graceful degradace:** celé v `try/except` — kdyby strmdck změnil strukturu nebo chyběl, jen se zaloguje WARNING a driver běží dál (zásek by se vrátil, ale nic se nerozbije).
- **Přesnost:** patchuje se `time` jen v modulu `ulanzi_d200`, ne globální `time.sleep`. Ověřeno, že v `ulanzi_d200.py` je **jediný** `time.sleep` (ten retry, ulanzi_d200.py:306); herdeck vlastní `time.sleep(delay)` v `_open_device` jede přes modul-level `time` (nedotčen). Base `device.py` sleepy (pokud jsou) jsou taky nedotčené.
- **Testovatelnost bez HW:** funkce se testuje injekcí fake strmdck modulu; aplikuje se jen v reálném `_open_device`, který fake-device testy přepisují → lokální testy strmdck neimportují.
- **Write-diff zůstává:** merged per-index diff je neškodná drobná optimalizace (méně redundantních zápisů); sleep fix je vlastní léčba zásek. Nerevertuje se.

## Testy

- **Unit (pytest):** `_neutralize_retry_sleep` přes fake `sys.modules["strmdck.devices.ulanzi_d200"]` (`.time = real time`) → po volání je `sleep(5.0)` okamžitý (no-op), `time.time()` pořád funguje (proxy passthrough), druhé volání je idempotentní (proxy se nepřebalí).
- **Recyklace:** stávající `test_d200_panel.py` + `test_render_pump.py` zůstávají zelené (fake-device testy přepisují `_open_device`, monkeypatch se v nich nevolá).
- **Manuální gate (macbench D200):** už **proběhl a potvrzen** (101 ms vs 5889 ms). Po nasazení čistého fixu re-verify, že writes ≤ ~100 ms a žádný multi-sekundový blok.

## Non-goals (YAGNI)

- Fork/vendoring strmdcku ani upstream PR (monkeypatch stačí; upstream je následný krok mimo tuto fázi).
- Revert write-diffu (neškodný, zůstává).
- Snížení frekvence spinner zápisů (se sleep fixem netřeba — i 0,4s tick má rezervu).
- Cachování připraveného zipu (working se mění každý frame; sleep fix dělá velikost/cache irelevantní).
