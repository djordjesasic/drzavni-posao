# Faza 0: repo i katalog – izveštaj

12. novembar 2026.

## Ukratko

Katalog je gotov: 2.538 epizoda i 19 specijala, svaki sa YouTube ID-jem, sezonom, naslovom i datumom emitovanja. Cela isporuka ide jednim pozivom `scripts/deploy.sh`. API i MCP rade na mini PC-u.

Most `mcp-proxy` je proveren sa Mac-a, istom komandom koju pokreće Claude Desktop (initialize, tools/list, tools/call). Ostaje samo da vlasnik restartuje Claude Desktop i potvrdi da vidi alate.

| Kriterijum | Stanje |
| --- | --- |
| ≥ 95% epizoda ima YouTube ID | **99,7%**: 2.538 od 2.545 rednih brojeva; 7 brojeva nema na kanalu |
| Ostatak na listi za ručni pregled | da, niže i u `faza-0-ne-epizode.md` |
| Isporuka jednim pozivom skripte | da, `scripts/deploy.sh` |
| Claude Desktop vidi MCP alat | config je upisan, a most je proveren; čeka se restart i potvrda vlasnika |

## Katalog

Izvor je YouTube kanal (odluka vlasnika: bez Vikipedije). Sa kartica `videos`, `streams` i `shorts`, kao i iz 22 plejliste, povučeno je 2.779 jedinstvenih videa. Povlačenje traje oko 2,5 minuta.

- **Redni broj, naslov i datum** dolaze iz naslova videa, npr. `DRŽAVNI POSAO [HQ] - Ep.2036: Na potpis (25.09.2023.)`. Ravna lista sa YouTube-a ne daje datum otpremanja, pa je datum emitovanja iz naslova jedini izvor datuma. Ima ga svih 2.538 epizoda.
- **Sezona** dolazi iz plejliste „DRŽAVNI POSAO N“. Devet epizoda nije ni u jednoj plejlisti (1196–1199, 1406, 1902, 1948, 2324, 2329). One su dobile sezonu suseda, jer su se susedi sa obe strane slagali.
- Nema dupliranih rednih brojeva.

**Raspodela svih videa sa kanala:**

| Vrsta | Broj | Sati |
| --- | --- | --- |
| epizoda | 2.538 | 362,8 |
| specijal (novogodišnji i drugi) | 19 | 9,1 |
| gegovi / cenzurisano | 67 | 6,5 |
| Pravi fudbal (druga serija) | 26 | 17,8 |
| klipovi bez broja epizode | 26 | 3,0 |
| najave i promo | 71 | 1,0 |
| shorts | 32 | – |

**Redni brojevi kojih nema na kanalu:** 999, 1000, 1083, 1084, 1085, 1543, 1734.

### Trajanje epizoda

Ukupno je **362,8 sati**. Prosek je 8,58 min, a medijana 8,45 min. Plan je računao sa 338 sati (8 min po epizodi), pa je stvarni obim oko 7% veći. Sa specijalima je ukupno **371,9 sati** za obradu.

| Raspon | Broj epizoda |
| --- | --- |
| 3–5 min | 16 |
| 5–6 min | 89 |
| 6–7 min | 305 |
| 7–8 min | 558 |
| 8–9 min | 626 |
| 9–10 min | 472 |
| 10–11 min | 273 |
| 11–12 min | 151 |
| 12–15 min | 45 |
| 25–26 min | 2 (ep. 500 „500“, ep. 2000 „Dosije Gajdobra“) |
| 59–60 min | 1 (ep. 283 „Sindikalni doček 2014.“) |

Percentili: p10 6:35, p50 8:27, p90 10:47.

### Po sezonama

| Sezona | Epizode | Sati | Brojevi | Period |
| --- | --- | --- | --- | --- |
| 1 | 187 | 22,8 | 1–187 | 09/2012 – 06/2013 |
| 2 | 214 | 29,1 | 188–401 | 06/2013 – 07/2014 |
| 3 | 190 | 28,8 | 402–591 | 09/2014 – 06/2015 |
| 4 | 173 | 24,8 | 592–764 | 09/2015 – 07/2016 |
| 5 | 169 | 23,0 | 765–933 | 10/2016 – 06/2017 |
| 6 | 147 | 21,2 | 934–1082 | 10/2017 – 06/2018 |
| 7 | 164 | 23,2 | 1086–1249 | 09/2018 – 06/2019 |
| 8 | 181 | 26,1 | 1250–1430 | 09/2019 – 07/2020 |
| 9 | 204 | 30,2 | 1431–1635 | 09/2020 – 07/2021 |
| 10 | 208 | 30,7 | 1636–1844 | 09/2021 – 07/2022 |
| 11 | 186 | 27,1 | 1845–2030 | 09/2022 – 06/2023 |
| 12 | 189 | 28,2 | 2031–2219 | 09/2023 – 06/2024 |
| 13 | 184 | 27,4 | 2220–2403 | 09/2024 – 06/2025 |
| 14 | 142 | 20,3 | 2404–2545 | 09/2025 – 04/2026 |

Sezona 1 je pre ispravke regex-a imala 181 epizodu. Šest ranih naslova nije imalo `[HQ]` ili je imalo višak iza datuma (ep. 63, 92–96).

### Ažurirana procena za fazu 2

Osnov je 362,8 sati zvuka umesto 338.

- Transkripcija: 52–91 sat rada, pri 4–7 puta bržem od trajanja.
- Opisi sa Qwen-om: oko 58 sati, ako broj tokena raste srazmerno trajanju.

## Šta je urađeno

- **Repo** po strukturi iz plana: `dp/` (config, baza sa migracijama, normalizacija teksta, katalog, API, MCP), `seed/characters.yaml`, `compose.yaml`, `docker/Dockerfile`, `scripts/` (deploy, worker, logs, shell, backup i dodatno `dp.sh` i `lock.sh`).
- **Zavisnosti** su zaključane u `uv.lock` (uv 0.13.7, fastapi 0.162, mcp 2.3.0, yt-dlp 2026.10.26). Ni Mac ni mini PC nemaju `uv`, pa `scripts/lock.sh` pravi lock u privremenom kontejneru na mini PC-u.
- **Mreža ka postojećim servisima** radi po opciji 1 (podrazumevana compose mreža). Iz kontejnera odgovaraju `whisper` (`/`) i `llama` (`/health`, `/v1/models`). Provera šalje samo GET zahteve, bez inference poziva. Na postojećim servisima, firewall-u i Docker-u hosta ništa nije menjano.
- **Registar likova:** 36 likova i njihovi nadimci su u tabelama `characters` i `aliases`. Svaki nadimak ima i oblik bez kvačica, pa „marjanovic“ nalazi oba Marjanovića.
- **API** (`api` kontejner, `100.84.205.39:8090`, samo na Tailscale adresi):
  - `/health` je javan; sve ostalo traži `Authorization: Bearer <DP_API_TOKEN>`
  - `GET /catalog`, `GET /episodes?q=&season=&year=`, `GET /episodes/{id}`, `GET /characters?q=`
  - **MCP** na `/mcp` (Streamable HTTP), alati `search_episodes`, `get_episode`, `find_character` i `catalog_info`. U ovoj fazi pretraga radi samo nad naslovima.
- **Worker** postoji kao servis pod profilom `worker`, ali u fazi 0 nema korake obrade.
- **Testovi:** 19 testova (normalizacija, registar, klasifikacija naslova, API, MCP uključujući token) prolazi u kontejneru. `ruff check` i `ruff format` su čisti.

## Odluke vlasnika (12. novembar 2026.)

- **Specijali se obrađuju kao epizode.** Ima ih 19 (9,1 h), u tabeli `episodes` su sa `kind = 'special'`, a ID-jevi su im 10001–10019, poređani po datumu. Novogodišnji specijal iz 2012. ima 5 delova, i svaki deo je zaseban unos. Sezona dolazi iz plejliste, a ako je nema, iz poslednje epizode emitovane pre specijala.
- **Gegovi, „Pravi fudbal“, klipovi, najave i shorts** ostaju van obrade.
- **Brojevi koji nedostaju** (999, 1000, 1083–1085, 1543, 1734) se ne traže. Deo njih su novogodišnji specijali, a deo verovatno greške u numeraciji.

## Postavka koju je uradio vlasnik

- `.env.example` i `/opt/drzavni-posao/.env` (`DP_API_TOKEN`, `DP_BIND_ADDR`)
- `uv` na Mac-u (`/opt/homebrew/bin/uvx`)
- token u `claude_desktop_config.json` (server `drzavni-posao` je upisan u config, a token je uneo vlasnik)

Claude Code se po želji povezuje ovako:

```
claude mcp add --transport http drzavni-posao http://100.84.205.39:8090/mcp \
  --header "Authorization: Bearer <DP_API_TOKEN>"
```

## Popravke posle code review-a

- ID-jevi specijala su trajni: postojeći specijal zadržava svoj ID, a novi dobija sledeći slobodan.
- Build kataloga više ne puca kad dve epizode imaju isti datum, a jedna nema poznatu sezonu.
- `catalog_info` odvojeno broji epizode i specijale. Iz MCP uputstva su uklonjeni ručno upisani brojevi.
- `api` radi sa `network_mode: host`, a uvicorn sam vezuje `DP_BIND_ADDR` (sada obavezan u `.env`). Ako Tailscale posle restarta još nije podignut, kontejner se ponovo pokreće dok bind ne uspe.
- `deploy.sh` ne šalje `backups/`, a `.dockerignore` drži `data/`, `backups/`, `.env` i `.git` van slike.
- `lock.sh` sada postavlja vlasnika `uv.lock` po korisniku na mini PC-u, a ne po Mac-u.
- Povlačenje kataloga ide preko `data/catalog/.staging`. Ako jedna lista ne uspe, ostale idu dalje, a ponovno pokretanje dovlači samo ono što fali. Objavljeni spisak se menja tek kad je sve uspelo, pa stare plejliste ne ostaju.
- `limit` mora biti 1–100 (API vraća 422), a `%` i `_` u pretrazi likova se tretiraju kao obični znakovi.
- Testova je sada 29, i svi prolaze.

## Incident: pogrešan rsync

Tokom popravki sam `scripts/common.sh` učitao u zsh umesto u bash. `REPO_DIR` je zato pokazivao na `~/repositories`, a `rsync --delete` je ceo taj direktorijum (oko 4,4 GB, uključujući ključeve iz `verobotics-archive`) preneo u `/opt/drzavni-posao` na mini PC-u. Fajlovi koda projekta tamo su obrisani.

- `data/` i `.env` nisu dirani, i ništa nije otišlo van tvojih mašina.
- Vlasnik je počistio direktorijum, a deploy je vratio kod.
- **Zaštita:** `common.sh` sada odbija da radi ako se ne učitava iz bash-a ili ako `REPO_DIR` nije koren ovog repoa (proverava `compose.yaml`, `dp/` i `PLAN.md`).

## Napomene za sledeću fazu

- `embed` kontejner (bge-m3) još ne postoji. Ulazi u fazu 1, kad zatrebaju vektori.
- `ffmpeg` još nije u slici. Dodaje se u fazi 1.
- yt-dlp brzo zastareva zbog izmena na YouTube-u. Pre faze 1 treba osvežiti lock (`scripts/lock.sh`).
- Kontejneri rade po `TZ=Europe/Belgrade`, a host po UTC-u. Pauza od 18:55 do 20:00 se računa po beogradskom vremenu.
