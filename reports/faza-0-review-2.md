# Faza 0: drugi code review i plan popravki

12. novembar 2026.

## Nalazi

| # | Fajl | Nalaz | Odluka |
| --- | --- | --- | --- |
| 1 | `dp/catalog/classify.py` | Red sa `episode_steps` se ne briše, pa premeštanje videa na drugi ID ruši build na `UNIQUE(video_id)` | popraviti (agent A) |
| 2 | `dp/catalog/classify.py` | Redovi koje klasifikator više ne daje, a imaju korake, ostaju sa zastarelim podacima i vide se u pretrazi | popraviti (agent A) |
| 3 | `dp/catalog/classify.py` | `next_special` uzima samo postojeće specijale, pa se ID obrisanog najvišeg specijala ponovo koristi | popraviti (agent A) |
| 4 | `dp/catalog/classify.py` | Izbor među duplikatima istog broja zavisi od redosleda u SELECT-u | popraviti (agent A) |
| 5 | `dp/catalog/classify.py` | Video bez trajanja postaje „short“ pre provere za specijal | popraviti (agent A) |
| 6 | `dp/catalog/classify.py` | Provera za specijal ne zavisi od velikih i malih slova, a `SPECIAL_RE` zavisi, pa specijal ostaje bez datuma | popraviti (agent A) |
| 7 | `dp/catalog/characters.py` | Ponovno učitavanje seed-a briše sve nadimke lika, a likovi uklonjeni iz seed-a ostaju u bazi | popraviti (agent B) |
| 8 | `dp/cli.py` | `catalog-stats` pada na praznoj bazi | popraviti (agent B) |
| 9 | `dp/search/catalog.py` | Pretraga naslova skenira celu tabelu u Python-u | **ne menja se**: oko 2.500 redova, a faza 3 zamenjuje ovo sa FTS5 |
| 10 | `config.toml` | `testserver` je u produkcionoj listi dozvoljenih host-ova | popraviti (agent B) |

## Priprema (urađeno pre agenata)

- **Migracija 3** u `dp/db.py`:
  - `episodes.retired` i `episodes.retired_video_id`: redovi epizoda se više ne brišu, nego se povlače
  - `aliases.source` (`seed`, `pipeline` ili `owner`)
- **`scripts/test.sh NAME`** pokreće pytest i ruff u izolovanoj kopiji na mini PC-u (`~/dp-test/NAME`, slika `drzavni-posao-test:NAME`). Ne dira `/opt/drzavni-posao` ni `api` koji radi.

## Agent A: klasifikacija i tabela epizoda (nalazi 1–6)

**Fajlovi:** `dp/catalog/classify.py`, `dp/search/catalog.py` (samo filter `retired`), `tests/test_build_episodes.py`, `tests/test_classify.py`, `tests/test_catalog_queries.py`.

1. **Redovi se ne brišu, nego se povlače.** Red koji klasifikator više ne daje dobija `retired=1`, `retired_video_id=<stari video_id>` i `video_id=NULL`. Tako ID nikad ne nestaje i ne koristi se ponovo, a `episode_steps` ostaju vezani za ono što je stvarno obrađeno.
2. **Premeštanje videa.** Ako video pređe na drugi ID, a stari red ga još drži, stari red se prvo povlači, pa se upisuje novi. Nema više `IntegrityError`.
3. **Vraćanje.** Ako se povučeni red ponovo pojavi sa istim ID-jem, `retired` se vraća na 0, a `retired_video_id` na NULL.
4. **ID-jevi specijala.** Sledeći ID je veći od svih ikad korišćenih ID-jeva specijala, uključujući povučene: `max(id)` gde je `id > SPECIAL_ID_BASE`, nad svim redovima. Postojeći specijal se prepoznaje po `video_id` ili po `retired_video_id`.
5. **Duplikati.** Pravilo izbora je deterministično: prvo snimak iz plejliste sezone, pa duže trajanje, pa leksički manji `video_id`.
6. **Specijali bez trajanja i sa drugačijim slovima.** Provera za specijal ide pre provere za shorts. `SPECIAL_RE` i `EPISODE_RE` ne zavise od velikih i malih slova.
7. **Pretraga.** `search_titles`, `get_episode` i `catalog_summary` u `dp/search/catalog.py` vide samo `retired=0`.
8. **Testovi** za svaki slučaj iznad.

## Agent B: registar, CLI i konfiguracija (nalazi 7, 8, 10)

**Fajlovi:** `dp/catalog/characters.py`, `dp/cli.py`, `config.toml`, `tests/test_characters.py`, `tests/test_api.py`, novi `tests/test_cli.py`.

1. **Nadimci.** `load_seed` briše i ponovo upisuje samo nadimke sa `source='seed'`. Nadimci iz drugih izvora ostaju.
2. **Likovi uklonjeni iz seed-a.** Lik sa `source='seed'` kog više nema u YAML-u se ne briše tiho, jer na njega mogu pokazivati reference. Funkcija ga vraća u izveštaju, a CLI ispisuje upozorenje.
3. **`catalog-stats` na praznoj bazi** ispisuje poruku i izlazi bez greške.
4. **`testserver`** se uklanja iz `config.toml`. Testovi koriste `TestClient(app, base_url="http://llmserver:8090")`, a taj host je već dozvoljen.
5. **Testovi** za tačke 1–4.

## Pravila za agente

- Svaki agent menja samo svoje fajlove.
- Testove pokreće samo sa `bash scripts/test.sh agent-a` ili `bash scripts/test.sh agent-b`.
- Ne pokreće `deploy.sh`, `dp.sh`, ručni `rsync` niti ssh komande koje menjaju `/opt/drzavni-posao`.
- Ne čita `.env`, ne commit-uje i ne dodaje dependencije.
- Kod, komentari i nazivi su na engleskom, sa type hint-ovima.

## Rezultat prvog kruga

- Oba agenta su završila. Nad spojenim izmenama prolazi 40 testova, a ruff je čist.
- Na kopiji produkcione baze klasifikacija daje isti rezultat: 2.538 epizoda i 19 specijala, svaki ID pokazuje na isti video, nijedan red nije povučen i nijedan short nije postao specijal.

## Drugi krug: nalazi i plan

| # | Nalaz | Odluka |
| --- | --- | --- |
| 1 | `import_raw` ne označava videe koji su nestali sa kanala, pa ostaju aktivne epizode | agent A: migracija 4 dodaje `videos.missing`; `import_raw` ga postavlja; `build_episodes` takve videe ne daje |
| 2 | Ako se za isti broj izabere drugi video, `episode_steps` ostaju vezani za stari video | agent A: kad se `video_id` aktivnog reda promeni, koraci tog ID-ja se brišu (obrada se ponavlja) i to se navodi u rezultatu build-a |
| 3 | `catalog-stats` i `catalog-build` broje povučene redove | agent A: filter `retired=0` u `dp/cli.py` |
| 4 | Lik izbačen iz seed-a zadržava seed nadimke i vidi se u pretrazi | agent B: seed nadimci takvog lika se brišu, a lik ostaje |
| 5 | Upsert likova ne postavlja `source='seed'` | agent B |
| 6 | `lock.sh` instalira nepinovanu verziju uv-a | agent B: `uv==0.13.7`, kao u Dockerfile-u |
| 7 | `.env.example` je prazan | vlasnik |
| 8 | Bearer: prima token bez šeme, a odbija `bearer` malim slovima | agent B: šema obavezna, bez obzira na velika i mala slova |
| 9 | Port 8090 je u compose-u i u `config.toml` | agent B: jedan izvor, `DP_API_PORT` (podrazumevano 8090), koriste ga compose i `_allowed_hosts`; uklanja se nekorišćen `extra_hosts` |
| 10 | Pretraga naslova u Python-u | ne menja se, FTS5 dolazi u fazi 3 |

**Priprema:** migracija 4 (`videos.missing`) je dodata u `dp/db.py`.

**Raspodela fajlova:**
- **Agent A:** `dp/catalog/youtube.py`, `dp/catalog/classify.py`, `dp/cli.py` i njihovi testovi.
- **Agent B:** `dp/catalog/characters.py`, `dp/search/catalog.py` (samo `find_characters`), `dp/search/app.py`, `dp/config.py`, `compose.yaml`, `config.toml`, `scripts/lock.sh` i njihovi testovi.

## Rezultat drugog kruga

- Prolazi 50 testova.
- Na kopiji produkcione baze nema promene: isti ID-jevi, 0 nestalih videa, 0 obrisanih koraka.

## Treći krug: nalazi (default review i Fable review, spojeni)

Fable review nije našao ništa kritično. Auth middleware je ocenjen kao ispravan: odbija sve kad token nije podešen, pokriva `/mcp` i poredi token u konstantnom vremenu.

### Agent A: tok kataloga

**Fajlovi:** `dp/catalog/youtube.py`, `dp/catalog/classify.py`, `dp/cli.py`, `dp/text.py` i njihovi testovi.

| # | Nalaz | Plan |
| --- | --- | --- |
| A1 | Epizoda sa brojem ≥ 10000 (ili 0) ulazi u prostor ID-jeva specijala | klasifikovati kao `clip` uz razlog; test |
| A2 | Neispravan datum u naslovu (npr. 31.13.) | provera preko `datetime.date`; kod greške `air_date=None` |
| A3 | `[Private video]` i `[Deleted video]` iz plejliste pregaze pravi red i računaju se kao prisutni | takve stavke se ne upisuju i ne računaju kao prisutne |
| A4 | Upsert prepisuje poznato trajanje sa NULL | `COALESCE(excluded.duration_s, videos.duration_s)` |
| A5 | `_publish` nije atomski | zamena celog direktorijuma preko `rename`; `import_raw` čita i stari raspored kao rezervu |
| A6 | `.staging` nema rok trajanja | staging stariji od 6 h se odbacuje |
| A7 | Skraćen listing može masovno da označi videe kao nestale | odbiti ako bi nestalo više od 10% ranije prisutnih videa, osim uz `--force` |
| A8 | Ista plejlista dvaput u `videos.playlists` | ukloniti duplikate |
| A9 | `catalog-build` broj nestalih videa ispisuje kao „episodes retired“ | ispraviti tekst |
| A10 | `fold()` ne pretvara akcentovanu ćirilicu (ѐ, ѝ) | NFKD i uklanjanje akcenata pre transliteracije |

### Agent B: API, baza, skripte, konfiguracija i registar

**Fajlovi:** `dp/db.py`, `dp/search/*`, `dp/config.py`, `dp/catalog/characters.py`, `scripts/*.sh`, `.gitignore`, `config.toml` i njihovi testovi.

| # | Nalaz | Plan |
| --- | --- | --- |
| B1 | Deploy ne primenjuje migracije | `deploy.sh` pokreće `dp.cli init` pre `up -d api` |
| B2 | `api` vraća 500 kad baza ne postoji ili je stara šema | 503 sa jasnom porukom |
| B3 | `migrate()` ne prijavljuje bazu noviju od koda | greška kad je `user_version` veći od broja migracija |
| B4 | Argumenti se šalju udaljenom shell-u bez navodnika (`dp.sh`, `test.sh`, `logs.sh`, `shell.sh`) | `printf '%q '`; provera imena servisa |
| B5 | Isključen je samo tačan naziv `.env`; liste isključenja su prepisane na dva mesta | jedna lista u `common.sh`: `.env*` osim `.env.example`, `*.sqlite*`; `.gitignore` dopuniti |
| B6 | Zaštita za `rsync --delete` proverava samo lokalni direktorijum | provera i udaljenog odredišta (`compose.yaml` ili prazan direktorijum) |
| B7 | Prazan `DP_API_PORT=` ruši api | `or 8090` |
| B8 | `episode_id`, `season` i `year` nemaju granice, pa veliki brojevi daju 500 | granice u API-ju i MCP alatima |
| B9 | Prazan seed briše sve seed nadimke | prazan seed se odbija |
| B10 | `llmserver:8090` je upisan sa portom, nezavisno od `DP_API_PORT` | u config-u samo imena host-ova, a port se dodaje iz `DP_API_PORT` |

Ne menja se: pretraga naslova u Python-u (FTS5 u fazi 3).

## Rezultat trećeg kruga

- Svih 76 testova prolazi, a ruff je čist.
- Na kopiji produkcione baze nema promene. Novi kod čita stari raspored kataloga, nijedan video nije označen kao nestao i nijedan korak nije obrisan.

## Četvrti krug (Fable review)

Nema kritičnih nalaza ni regresija iz trećeg kruga. Preostali nalazi i predlog:

| # | Nalaz | Težina | Predlog |
| --- | --- | --- | --- |
| 1 | Plejlista koja trajno puca blokira objavu celog kataloga | srednja, hipotetična | kasnije: opcija `--skip-failed` |
| 2 | Skraćen listing plejliste promeni izbor duplikata i obriše korake | srednja, hipotetična (sada 0 duplikata) | **faza 1**: korake vezati za `video_id`, umesto da se brišu |
| 3 | Dve istovremene migracije: druga pada na „duplicate column“ | niska | sada: `BEGIN IMMEDIATE` i ponovno čitanje verzije |
| 4 | Deploy ne zaustavlja i ne restartuje worker | sada bezopasno | sada: zaustaviti worker pre `init`, pa ga vratiti |
| 5 | Port za uvicorn i port u `allowed_hosts` mogu da se razlikuju | niska | sada: `DP_API_PORT` u `environment` u compose-u |
| 6 | openrsync možda ne štiti `data/` i `.env` od `--delete` | – | **provereno dry-run-om**: zaštićeni su |
| 7 | MCP iz browsera (Origin) i FQDN/HTTPS | niska | kad zatreba (faza 3 ili javni pristup) |
| 8 | Nema testova za granu `.old` i za skripte | niska | sada: test za `.old` |
| – | Neusidreni `data/` i `backups/` u isključenjima, poruka kad ssh ne uspe, `lock.sh` montira ceo direktorijum, poruke za loš YAML | sitno | sada |

## Četvrti krug popravki (koordinator Fable)

Koordinator je sam podelio posao i izabrao modele:

| Agent | Model | Stavke |
| --- | --- | --- |
| db | opus | zaključavanje migracija (`BEGIN IMMEDIATE`, ponovno čitanje verzije, bez `executescript`) |
| scripts | sonnet | deploy zaustavlja i vraća worker; usidrena isključenja; poruka kad ssh ne uspe; `lock.sh` u privremenom direktorijumu |
| seed | haiku | jasne greške za loš YAML |
| koordinator | – | `DP_API_PORT` u okruženju kontejnera; test za prekinuto objavljivanje kataloga |

**Rezultat:**
- Prolazi 97 testova, a ruff je čist.
- Na kopiji produkcione baze migracija ide od v2 do v4, a drugi poziv ništa ne menja. ID-jevi su isti, a seed nema siročića.

## Deploy, 12. novembar 2026.

- Novi `deploy.sh` je prošao: build, zatim `dp.cli init` (šema v4), pa `up -d api`. Worker nije radio, pa nije ni diran.
- Na produkciji su pokrenuti `catalog-import` (2.779 videa, 0 nestalih) i `catalog-build` (2.538 epizoda, 19 specijala).
- Provereno posle deploy-a:
  - `/health` vraća 200, a `/catalog` vraća epizode i specijale odvojeno
  - `bearer` malim slovima prolazi, token bez šeme dobija 401, a `/episodes/999999` daje 422
  - MCP `get_episode(2036)` radi preko `mcp-proxy`

**Ostaje za fazu 1:** vezati korake obrade za `video_id`. **Kasnije:** MCP iz browsera i FQDN, i plejlista koja trajno puca.
