# Audit kompatibility T3 Code a Herdecku

Datum: 2026-09-06. Jde o research, nikoliv implementaci oprav.

## Rozsah a důkazy

- Herdeck: větev `feat/t3-code-integration`, HEAD `430e11e`. Poslední oprava
  ukládání pinů ještě čeká na nasazení na offline MacBench. Výzkum tento stav nemění.
- T3 server: instalovaná verze `0.0.38`, ověřená v lokálním service-state.
  Upstream tag `v0.0.38` odkazuje na `c0995d2eaf8ec787b3318ed1169ae266ed1529f8`.
- Zdrojem pro kontrakty níže je tento konkrétní tag, ne proměnlivý `main`.
- Lokální SQLite byla otevřena pouze v read-only režimu. Žádná zpráva, rozhodnutí,
  settle/unsettle ani jiný zápis nebyl odeslán do T3.
- Přímé volání `thread_state` ověřilo mapování syntetických kontraktních případů.
  Reálná lokální databáze obsahovala jeden nearchivovaný, nesmazaný settled thread:
  `settled_override=settled`, session `stopped`, poslední turn `completed`.
  Převod těchto skutečných stavových polí v Herdecku vrátil `done`.

## Závěr

Adaptér dnes zaměňuje dokončení posledního běhu s životním cyklem celého threadu.
`latestTurn.completedAt` dokládá, že proběhla práce; nedokládá, že její výsledek
stále čeká na uživatele. Jedna enum hodnota IDLE/WORKING/DONE nestačí k zachování
všech významů T3. Rychlá náhrada všech DONE za IDLE by znovu ztratila skutečná dokončení.

## Potvrzené chyby a mezery

| ID / priorita | Situace | Současný Herdeck | Potřebné chování |
| --- | --- | --- | --- |
| R1 / P1 | `settledOverride=settled`, poslední turn completed | DONE, nabízí Continue | Uzavřený thread nezobrazovat jako nevyřízené dokončení; respektovat settle i unsettle. |
| R2 / P1 | Změna pouze settled/snooze při nezměněné session a turnu | `backend_revision` se nezmění | Zneplatnit předchozí ovládání po změně životního cyklu. Staré Continue nesmí thread nečekaně znovu otevřít. |
| R3 / P1 | Čerstvá uživatelská zpráva, ještě nepřevzatá session | DONE + Continue, pokud předchozí turn completed | Rozpoznat queued start; nenabízet další běh během převzetí požadavku. |
| R4 / P1 | Plan mode + `hasActionableProposedPlan` | DONE, případně background WORKING/WAITING | Plan Ready jako požadavek na rozhodnutí. Implementaci plánu spouštět správným kontraktem, nikoliv generickou zprávou Continue v plan mode. |
| R5 / P1 | `session.status=error` a `lastError` | UNKNOWN, bez chybové zprávy v preview | Odlišit chybu agenta od výpadku připojení; ukázat srozumitelnou příčinu. |
| R6 / P2 | `snoozedUntil` v budoucnosti, bez nové události | Stále běžná dlaždice / DONE | Odložit viditelnost, vrátit při uplynutí času nebo nové události vyžadující pozornost. |
| R7 / P2 | Completed již přečtený v T3 | DONE přetrvává do další práce | Definovat potvrzení výsledku v Herdecku; přesnou shodu mezi zařízeními neslibovat bez sdíleného read-state API. |
| R8 / P2 | Background working bez `activeTurnId` | WORKING, pouze read, Stop neaktivní | Jasně zobrazit, co běží a co lze zastavit. Interrupt turnu, stop session a ukončení background tasku nejsou zaměnitelné. |

R1–R6 a R8 jsou ověřeny proti konkrétním polím a větvím adaptéru; R1 navíc proti
živým stavovým datům. R7 je potvrzený rozdíl významu mezi implementacemi klientů.
R2 potvrzuje nezměněnou revizi a stále dostupné Continue; skutečné znovuotevření
živého threadu jsme záměrně nezkoušeli. U R3 jsme ověřili chybnou dostupnost tlačítka,
nikoliv skutečné spuštění druhého agenta: o přijetí rozhoduje T3 server.

### Reprodukce bez zápisů

Výchozí fixture: session ready, žádný activeTurnId, poslední turn completed,
runtimeMode approval-required, interactionMode default, žádné pending requests.
Samostatné změny fixture dávají:

```text
settledOverride=settled                    -> done     [read, continue]
snoozedUntil=future; snoozedAt>completedAt  -> done     [read, continue]
interactionMode=plan; actionablePlan=true  -> done     [read, continue]
session.status=error; lastError=...        -> unknown  [read]; preview bez chyby
session=null; latestUserMessageAt=now      -> done     [read, continue]
backgroundLiveness=working                -> working  [read]
settle only                               -> stejná backend_revision
```

Kód Herdecku: [mapování stavů a revizí](../../src/herdeck/t3.py), funkce
`thread_state`; schovává pouze `archivedAt` ve `T3Connector.refresh`.
Snooze, settled, pending plan ani queued-start pole se nepoužívají.

## Ovládání a další zlepšení

1. **Schvalování a vstupy (P2).** T3 kontrakt má accept, acceptForSession,
   acceptAlways, decline a cancel. Adaptér propouští jen accept/decline/cancel.
   Jde o současné omezení, nikoliv důvod automaticky povolovat trvalá oprávnění.
   Rozšíření má respektovat přesné nabídnuté options a vyžadovat vědomý výběr.
   Více otázek, multi-select a volný text jsou záměrně nepodporované; detail má
   uživateli jasně říct „Odpověz v T3“, zachovat znění otázky a nenabízet neúplnou odpověď.

2. **Chyba jednoho threadu nesmí shodit celý zdroj (P2).** Refresh dělá jeden shell
   request a postupně detail každého nearchivovaného threadu. Jakákoliv chyba detailu
   přeruší celý refresh; `run` označí celý server offline. Například smazání mezi
   shellem a detailem může způsobit falešné Offline/Reconnecting. Je to dosažitelná
   větev v kódu, nikoliv nově pozorovaný produkční incident. Testovat 404 jednoho
   detailu zvlášť od 401 celé relace a od nedostupnosti serveru.

3. **Latence a konzistence (P2).** N+1 sériových requestů na každý cyklus a stejný
   úplný refresh před každou akcí. Interval není přesně sekunda: je to doba všech
   requestů + sekunda. Navíc drží stejný lock jako ovládání. Doporučení: shell jako
   základ overview, detail jen při změně nebo otevřeném ovládání, případně stream
   událostí s resync. Nikdy neuvolnit ochranu před zastaralým rozhodnutím kvůli rychlosti.

4. **Vyjednání schopností (P2).** Docstring adaptéru deklaruje kontrakt 0.0.31,
   instalace je 0.0.38. Adaptér nemá explicitní načtení schopností serveru.
   Nové příkazy settle/snooze/pin/plan musí být podmíněné podporou serveru;
   chybějící pole nejsou automaticky false. Přidat kontraktní fixture z podporovaných
   verzí a čitelné hlášení při nekompatibilitě.

5. **Dva různé piny (P2).** T3 `pinnedAt`/`pinOrderKey` upravují jeho seznam;
   nové Herdeck piny rezervují fyzické místo. Automaticky je neslučovat.
   Kontrakt 0.0.38 výslovně ponechává settled/snoozed thread v příslušné skupině
   i při T3 pinu. Lokální pin nesmí rušit settle/snooze. Pokud budoucí filtr pouze
   odstraní settled thread ze snapshotu, současná pin logika ho mylně vykreslí jako
   „Pinned · offline“. Zachovat znalost lifecycle a správně označit rezervaci.

6. **Připojení a credential (P2, již dokumentované).** MacBench používá port forward
   vlastněný desktopovou T3 aplikací; může zaniknout nebo změnit port. Zprovoznění
   serverové služby na HEADLESSu tento klientský forward samo nenahrazuje.
   Integrační bearer session je časově omezená a nemá automatickou obnovu.
   Obojí je zaznamenáno v [runbooku](../agent-setup.md). V tomto auditu nebylo možné
   zkontrolovat macBench procesy, protože stroj je offline. Doporučit stabilní
   dedikované spojení a obnovu přístupu; síťovou infrastrukturu zde neměnit.

## Doporučený model a pořadí implementace

Oddělit alespoň tři osy: **běh** (queued/starting/running/background/monitoring/
stopped/error), **požadavek na uživatele** (approval/input/plan/unseen completion)
a **životní cyklus** (active/settled/snoozed/archived/deleted). Offline je stav
spojení, pin je preference umístění. Nezaměňovat je za stavy threadu.

1. Opravit R1/R2/R6 společně: lifecycle metadata, pravidla viditelnosti, snooze
   časovač a invalidace akcí. Výchozí návrh: settled ani snoozed neplní aktivní
   overview; připnutá rezervace zůstává rozpoznatelná jako settled/snoozed.
   Nový approval/input/error má mít přednost podle upstream pravidel.
2. Doplnit R3/R4/R5: queued start, Plan Ready a chyby; navázat dostupnost akcí
   na sémantiku serveru. Obecné Continue v plan mode není implementace plánu.
3. Vyřešit R7/R8 a transport: lokální potvrzení Done s jasným významem,
   background ovládání, izolace chyby detailu, stabilní připojení.

## Minimální akceptační matice

- Completed → settle z mobilu → overview i připnutá pozice nemají nevyřízené DONE.
- Unsettle / nový turn → thread se vrátí bez změny identity a bez ztráty lokálního pinu.
- Snooze → uplynutí času bez serverové události; zvlášť nové approval, input,
  error a completion novější než snoozedAt. Starší chyba nesmí sama rušit odložení.
- Plan Ready + background monitoring → plán má správnou prioritu a správnou akci.
- Zpráva odeslána z mobilu, session ještě null → žádné zavádějící DONE ani Continue.
- Error ve session i posledním turnu → srozumitelný chybový detail.
- Změna lifecycle během otevřeného detailu → staré tlačítko je neplatné.
- Jediný detail vrátí 404 → ostatní thready zůstávají živé; 401 → obnovení přístupu.
- Piny + archive/delete/settle/snooze/reconnect + změna profilu + restart runtime.
- Odpověď z mobilu zneplatní stejné pending rozhodnutí na decku; žádný retry zápisu.

## Upstream zdroje pro 0.0.38

- [Kontrakty threadů, lifecycle, approval decisions a příkazů](https://github.com/pingdotgg/t3code/blob/c0995d2eaf8ec787b3318ed1169ae266ed1529f8/packages/contracts/src/orchestration.ts)
- [Queued start, snooze a předčasné probuzení](https://github.com/pingdotgg/t3code/blob/c0995d2eaf8ec787b3318ed1169ae266ed1529f8/packages/client-runtime/src/state/threadSettled.ts)
- [Desktop: priority stavů, Plan Ready, unseen completion](https://github.com/pingdotgg/t3code/blob/c0995d2eaf8ec787b3318ed1169ae266ed1529f8/apps/web/src/components/Sidebar.logic.ts)
- [Mobil: chybový stav a Plan Ready](https://github.com/pingdotgg/t3code/blob/c0995d2eaf8ec787b3318ed1169ae266ed1529f8/apps/mobile/src/features/threads/threadPresentation.ts)
- [Serverová pravidla settle, snooze a reaktivace](https://github.com/pingdotgg/t3code/blob/c0995d2eaf8ec787b3318ed1169ae266ed1529f8/apps/server/src/orchestration/decider.ts)
- [Strukturované odpovědi na otázky](https://github.com/pingdotgg/t3code/blob/c0995d2eaf8ec787b3318ed1169ae266ed1529f8/apps/web/src/pendingUserInput.ts)

Historické upstream issues [#4589](https://github.com/pingdotgg/t3code/issues/4589)
(stale klient a settle), [#6333](https://github.com/pingdotgg/t3code/issues/6333)
(auto-settle podle PR) a [#7653](https://github.com/pingdotgg/t3code/issues/7653)
(ztráta PR po smazání branche) jsou podněty pro regresní scénáře, nikoliv důkaz,
že stejná upstream chyba stále existuje v 0.0.38. Aktuální server už vlastní
settle rozhodnutí; starší klientskou heuristiku podle PR nekopírovat do Herdecku.
