# Državni posao – baza znanja

Baza znanja za seriju „Državni posao“ (oko 2.500 epizoda na YouTube-u). Opišeš epizodu po sećanju
(u Claude Desktop-u ili preko HTTP-a), a sistem vrati epizodu i link na tačan minut.

Plan rada je u [PLAN.md](PLAN.md), izveštaji po fazama u [reports/](reports/), pravila za mini PC
u [CLAUDE.md](CLAUDE.md).

## Kako je složeno

- **Mac:** ovde se piše kod i odavde se pokreću skripte iz `scripts/`. Ovde se ništa ne obrađuje i
  ne grade se Docker slike.
- **Mini PC** (`ssh llmserver`, Tailscale): sve radi u Docker Compose-u, u `/opt/drzavni-posao`.
  - `api`: FastAPI + MCP server na `/mcp` (port 8090)
  - `worker`: obrada epizoda (preuzimanje → priprema → transkripcija → govornici → indeks)
  - `embed`: bge-m3 embedding model na procesoru
  - koristi tuđe servise `whisper-server` (172.17.0.1:8081) i `llama-server` (172.17.0.1:8080),
    samo za čitanje; vidi pravila u [CLAUDE.md](CLAUDE.md)
- **Podaci:** `data/` (SQLite baza, zvuk, transkripti) postoji samo na mini PC-u i nikad ne ide u
  git. `series/drzavni-posao/` je u gitu: `config.toml` i `seed/` (likovi, glasovi, uputstva
  za opise i za Claude, test pitanja). Vidi „Više serija“ ispod.

## Prvo podešavanje

1. **SSH:** sa Mac-a mora da radi `ssh llmserver` bez lozinke (Tailscale SSH).
2. **`.env` na mini PC-u:** kopiraj [.env.example](.env.example) u `/opt/drzavni-posao/.env` i upiši
   vrednosti. `.env` se pravi i menja samo ručno, na mini PC-u.
   - `DP_API_TOKEN`: token za `/mcp` i HTTP API (npr. `openssl rand -hex 32`)
   - `DP_BIND_ADDR`, `DP_API_PORT`: Tailscale adresa mini PC-a i port (podrazumevano 8090)
   - `ANTHROPIC_API_KEY`: za opise epizoda (Sonnet)
   - `HF_TOKEN`: opciono (vidi komentar u `.env.example`)
3. **Deploy:** `bash scripts/deploy.sh`. Šalje kod na mini PC, tamo gradi sliku, pokreće migracije
   baze (`dp init`) i diže `api` i `embed`. Ako je radnik radio, zaustavlja ga dok traje deploy i
   posle ga ponovo pokreće.
4. **Provera servisa:** `bash scripts/dp.sh check-services`
5. **Katalog i likovi** (jednom, ili kad izađu nove epizode):
   ```bash
   bash scripts/dp.sh seed-characters
   bash scripts/dp.sh catalog-fetch
   bash scripts/dp.sh catalog-import
   bash scripts/dp.sh catalog-build      # odbija dok neki korak radi; --force samo ako znaš zašto
   bash scripts/dp.sh catalog-stats
   ```
6. **Profili glasova** (posle izmene `series/drzavni-posao/seed/voices.yaml`): `bash scripts/dp.sh voice-profiles`

Skripte se uvek pokreću kao `bash scripts/x.sh`, nikad sa `source`.

## Radnik (obrada cele serije)

Radnik uzima epizode redom i radi sve korake. Može da se zaustavi i ponovo pokrene u bilo kom
trenutku i nastavlja gde je stao. Sam poštuje pravila:

- pauza svakog dana od 18:55 do 20:00 (beogradsko vreme);
- jedan Whisper zahtev u isto vreme;
- pauza od 20–30 s posle svakog preuzimanja sa YouTube-a, a duža (do 2 h) dok YouTube odbija;
- neuspeo korak se ponavlja posle 10 min, pa sve ređe (do 24 h); posle 5 neuspeha zaredom ostaje
  za ručnu proveru;
- temperature se beleže svakog minuta; kad procesor, grafička ili disk pređu granicu iz
  `series/drzavni-posao/config.toml` (`[worker]`), ne počinje novu epizodu dok se ne ohladi.

| Komanda | Šta radi |
|---|---|
| `bash scripts/worker.sh start` | pokreće radnika kao servis (preživi restart mini PC-a) |
| `bash scripts/worker.sh stop` | zaustavlja ga; prekinut korak se vraća u red, bez kazne |
| `bash scripts/worker.sh status` | da li radi |
| `bash scripts/progress.sh [sati]` | koliko je epizoda gotovo, neuspeli koraci, poslednja temperatura; za poslednjih N sati (podrazumevano 12): epizoda na sat, vreme po koraku, čekanje zbog toplote i procena koliko je ostalo |
| `bash scripts/logs.sh worker` | prati log radnika (Ctrl-C za izlaz; radnik nastavlja) |
| `bash scripts/dp.sh worker --limit 3` | jednokratna proba na 3 epizode, bez servisa |

Ručna obrada određenih epizoda (radnik ne mora da radi, ali Whisper je zajednički):

```bash
bash scripts/dp.sh run --episodes 612,739 --steps download,prepare,transcribe,speakers,index
bash scripts/dp.sh run --episodes 612 --steps transcribe --force   # ponovo, i kad je gotovo
bash scripts/dp.sh run --steps index --rebuild-index                # posle promene embedding modela
```

## Opisi epizoda (LLM)

```bash
bash scripts/dp.sh enrich --model sonnet --episodes 612,739
```

Modeli su u `series/drzavni-posao/config.toml` (`[enrich.models.*]`). Kad se transkript promeni, opis se sam označi
kao zastareo i čeka ponovni opis.

## Pretraga

**Claude Desktop (Mac):**

1. Instaliraj `mcp-proxy`: `uv tool install mcp-proxy`
2. U `~/Library/Application Support/Claude/claude_desktop_config.json` dodaj:
   ```json
   {
     "mcpServers": {
       "drzavni-posao": {
         "command": "/Users/<ime>/.local/bin/mcp-proxy",
         "args": ["--transport=streamablehttp",
                  "--headers", "Authorization", "Bearer <DP_API_TOKEN>",
                  "http://llmserver:8090/mcp"]
       }
     }
   }
   ```
3. Restartuj Claude Desktop. Alati: `search_episodes` (po sadržaju), `search_titles`,
   `get_episode`, `get_transcript_excerpt`, `find_character`, `catalog_info`.

**Komandna linija:** `bash scripts/dp.sh search "mama da dođe po njega"`

**HTTP** (sa tokenom u `Authorization: Bearer …`):
`POST /search` (`{"query": "...", "limit": 5}`), `GET /episodes/{id}`,
`GET /episodes/{id}/transcript?start_s=..&end_s=..`

**Test pitanja:** `bash scripts/dp.sh eval` (pitanja u `series/drzavni-posao/seed/test_questions.yaml`)

## Ostale skripte

| Komanda | Šta radi |
|---|---|
| `bash scripts/logs.sh [api\|worker]` | prati log jednog ili svih servisa |
| `bash scripts/shell.sh [worker\|api]` | shell u privremenom kontejneru |
| `bash scripts/backup.sh` | kopija baze na Mac, u `backups/` |
| `bash scripts/export_catalog.sh` | spisak epizoda u `exports/epizode.csv` |
| `bash scripts/dp.sh <komanda>` | bilo koja `dp` komanda (`bash scripts/dp.sh --help`) |

## Više serija

Kod je jedan, a svaka serija ima svoj folder `series/<ime>/` i radi kao poseban compose projekat:
svoji kontejneri, svoja baza i podaci, svoj port i svoj MCP server u Claude Desktop-u.
Zajednički su samo kod, Docker slika, tajne u `.env` i zaključavanje Whisper-a
(`locks/`, jedan zahtev u isto vreme za sve serije zajedno).

```
series/<ime>/
  config.toml              kanal, [series] ime i naslov, [catalog] parser, modeli, granice
  series.env               DP_SERIES, DP_DATA_DIR (npr. ./data-<ime>), DP_API_PORT, DP_EMBED_PORT
  seed/
    characters.yaml        registar likova
    voices.yaml            isečci glasova
    enrich_system.md       uputstvo za opise (šta je serija, ko su glavni likovi)
    mcp_instructions.md    uputstvo za Claude-a u Desktop-u
    background.md          pozadina serije (opciono)
    test_questions.yaml    test pitanja
```

Skripte rade na seriji iz `DP_SERIES` (podrazumevano `drzavni-posao`):

```bash
DP_SERIES=<ime> bash scripts/deploy.sh
DP_SERIES=<ime> bash scripts/worker.sh start
DP_SERIES=<ime> bash scripts/progress.sh
```

Nova serija:
1. Kopiraj `series/drzavni-posao/` u `series/<ime>/`; u `series.env` promeni `DP_SERIES`,
   `DP_DATA_DIR`, `DP_EMBED_PORT` i dodaj `DP_API_PORT` (svi različiti od drugih serija).
2. U `config.toml` promeni kanal i `[series]`; prepiši `seed/` za novu seriju.
3. Ako naslovi videa nisu u obliku „DRŽAVNI POSAO - Ep.N: …“, dodaj parser u
   `dp/catalog/classify.py` (`PARSERS`) i upiši ga u `[catalog] parser`.
4. `DP_SERIES=<ime> bash scripts/deploy.sh`, pa katalog i probu na 2–3 epizode.

## Razvoj

- Testovi i ruff se pokreću na mini PC-u, u izolovanoj kopiji (ne diraju pokrenute servise):
  ```bash
  bash scripts/test.sh mojtest                         # sve
  bash scripts/test.sh mojtest tests/test_worker.py    # jedan fajl
  ```
- Nova zavisnost u `pyproject.toml` → `bash scripts/lock.sh` osvežava `uv.lock` (uv je samo u
  Docker-u).
- Šema baze se menja migracijama u `dp/db.py` (`PRAGMA user_version`); primenjuje ih `deploy.sh`.
