# Državni posao – baza znanja

Baza znanja za seriju „Državni posao“ (oko 2.500 epizoda na YouTube-u). Prvi cilj: korisnik opiše
epizodu po sećanju, a sistem vrati tu epizodu i link na tačan minut.

Ceo plan je u `PLAN.md`. Radi se fazu po fazu, kako piše tamo. Posle svake faze napiši izveštaj u
`reports/faza-N.md` i stani. Sledeća faza počinje tek kad je vlasnik odobri.

## Mašine

- **MacBook (ovde):** ovde se piše kod. Ovde se ne pokreće obrada i ne grade Docker slike.
- **Mini PC (Ubuntu, x86):** tamo sve radi, u Docker Compose-u. Pristup preko Tailscale SSH-a: `ssh llmserver`.
- Slike se grade samo na mini PC-u (`scripts/deploy.sh`). Mac je ARM i pravio bi slike za pogrešnu arhitekturu.

## Pravila za mini PC (ne smeju se prekršiti)

Na mini PC-u već rade servisi drugog projekta, i ovaj projekat ih samo koristi:

- `whisper-server` (whisper.cpp, large-v3) na `172.17.0.1:8081`, endpoint `/inference`
- `llama-server` (Qwen3.6-35B-A3B) na `172.17.0.1:8080`, OpenAI-kompatibilan API, 2 slota po 8.192 tokena

1. Ne menjaj njihove unit fajlove, modele ni kontekst. Oni pripadaju drugom repou.
2. Ne čitaj `.env` drugog projekta.
3. Ne gradi i ne pokreći ništa sa Vulkan-om na mini PC-u. Trajno obara propusnost memorije do restarta.
4. Ne učitavaj nove modele na grafičku (zauzeto je oko 76%). Sve novo radi na procesoru.
5. Koristi najviše jedan slot llama-server-a. Whisper-u šalji jedan deo snimka u isto vreme.
6. Masovna obrada pauzira od 18:55 do 20:00.
7. Ne menjaj firewall, Docker podešavanja hosta ni sistemske servise bez pitanja.

Pre bilo kakve radnje koja dira nešto van ovog projekta na mini PC-u, pitaj vlasnika.

## Tehnologija

- Python 3.12, `uv`. Backend FastAPI.
- **Bez npm-a, React-a i build koraka za frontend.** Web je Jinja2 + malo običnog JavaScript-a
  (eventualno htmx kao jedan fajl u repou).
- Docker Compose: servisi `api`, `worker`, `embed`.
- Baza: SQLite (FTS5 + sqlite-vec), WAL režim. `worker` piše, `api` čita.
- MCP server je deo `api` servisa, Streamable HTTP na `/mcp`.

## Podaci i tajne

- `data/` postoji samo na mini PC-u i nikad ne ide u git.
- `seed/` je u gitu: početni podaci koje daje vlasnik (`seed/characters.yaml`).
- `.env` postoji samo na mini PC-u. U repou je samo `.env.example`. Nikad ne upisuj ključeve u kod ni u git.



## Način rada

- Svaki korak obrade upisuje status u bazu i mora moći da se prekine i nastavi bez ponavljanja.
- Pre masovne obrade uvek prvo probaj na 2–3 epizode i pokaži rezultat.
- Tekst iz serije je na srpskom, latinica i ćirilica. Čuvaj original, a za pretragu pravi latinicu bez kvačica.
- Imena likova uvek proveravaj u `seed/characters.yaml` i u tabeli `characters`. Ne izmišljaj likove ni veze.
- Kad nisi siguran šta vlasnik želi, pitaj umesto da pretpostaviš.