# Državni posao – baza znanja: tehnički plan

Nov 12, 2026 · @Djordje

## Cilj i obim

Prva faza je uspešna kada opis epizode po sećanju vrati tačnu epizodu među prvih 5 rezultata, sa linkom na YouTube koji otvara pravi minut.

Ovaj plan pokriva sve do tog cilja (faze 0–4) i daje okvir za kasnije faze. Namenjen je Claude Code-u, koji ga izvršava fazu po fazu. Svaka faza ima kriterijum „gotovo“ i završava se kratkim izveštajem koji vlasnik pregleda pre sledeće.

**U obimu sada:**

- katalog svih epizoda (oko 2.500, snimanje je završeno, pa nema novih)
- transkript svake epizode sa vremenskim oznakama i oznakom ko govori
- strukturisan opis svake epizode (sažetak, likovi, priče, imitacije, replike)
- osnovni registar likova sa nadimcima
- servis za pretragu i povezivanje sa chat botom

**Kasnije, van ovog obima:** biografije likova, graf veza, kanon i kontradikcije, mape preduzeća i grada, javni sajt.

**Prvi test-primer:** Čvarkov i Torbica sede sami u kancelariji, Boškić kasni. Čvarkov priča kako je neko u lovu ponizio Marjanovića, u vreme kad je Čvarkov bio finansijski a Marjanović generalni direktor. Pri tome imitira tog čoveka jakim sarajevskim naglaskom. Epizoda još nije poznata; sistem mora da je nađe.

## Polazno stanje

Sva teška obrada ide na mini PC, preko servisa koji tamo već rade. Ništa novo se ne učitava na grafičku memoriju.

| Mašina | Opis | Uloga u projektu |
| --- | --- | --- |
| Mini PC (Ubuntu) | Ryzen AI 7 350, Radeon 860M, 96 GB RAM, 32 GB dodeljeno grafici | obrada, baza, servis za pretragu |
| MacBook | M3 Pro, 36 GB RAM | Claude Code piše kod, Claude Desktop je chat; rezerva za transkripciju |

Claude Code radi na MacBook-u i pristupa mini PC-u preko Tailscale SSH-a.

**Postojeći servisi na mini PC-u (deljeni sa drugim projektom):**

| Servis | Model | Adresa | Ograničenje |
| --- | --- | --- | --- |
| whisper-server | large-v3 | 172.17.0.1:8081 | jedan zahtev u isto vreme |
| llama-server | Qwen3.6-35B-A3B Q4\_K\_M | 172.17.0.1:8080 | 2 slota po 8.192 tokena |

Izmerene brzine: Qwen generiše oko 18,5 tokena u sekundi i čita prompt brzinom oko 265 tokena u sekundi. Whisper obradi snimak od 11 s za 3,8 s.

**Pravila koja Claude Code ne sme da prekrši:**

1. Ne menja unit fajlove, modele ni veličinu konteksta postojećih servisa. Oni pripadaju drugom repou.
2. Ne čita `.env` drugog projekta.
3. Ne gradi i ne pokreće ništa sa Vulkan-om na mini PC-u. Vulkan tamo trajno obara propusnost memorije do restarta.
4. Ne učitava nove modele na grafičku. Zauzeto je oko 76% memorije. Embedding i prepoznavanje govornika rade na procesoru.
5. Koristi najviše jedan slot llama-server-a i šalje Whisper-u jedan po jedan fajl. Drugi slot ostaje postojećem botu.
6. Masovna obrada pauzira od 18:55 do 20:00, dok postojeći bot radi svoje obogaćivanje (traje do sat vremena).
7. Oba servisa slušaju samo na 172.17.0.1. Obrada zato radi u kontejnerima na samom mini PC-u, a ne sa druge mašine.

## Ključne odluke

Podrazumevano sve radi lokalno i besplatno. Claude API je rezerva koju pilot može da opravda ili odbaci.

| Odluka | Izbor | Razlog |
| --- | --- | --- |
| Transkripcija | postojeći whisper-server, large-v3 | već radi, bolji je na srpskom od turbo varijante |
| Ko govori | prepoznavanje glasa trojice glumaca, na procesoru | glumci su stalni, pa je klasifikacija pouzdanija od slepog grupisanja |
| Glas nije lik | glas daje glumca, a lik određuje jezički model iz konteksta | Čvarkov glumi i Stevicu, Boškić i Boškića starijeg, a tu su i imitacije |
| Obogaćivanje | lokalni Qwen; Claude samo ako pilot pokaže da Qwen nije dovoljan | bez dodatnog troška |
| Baza | jedan SQLite fajl: FTS5 za reči, sqlite-vec za vektore | 2.500 epizoda je malo; nema servera za održavanje |
| Embedding | bge-m3 na procesoru | višejezičan, dobar za srpski, ne troši grafičku memoriju |
| Pilot epizode | pretraga ih uključuje sve; kanon se rešava kasnije | možda se traži baš rana epizoda, a kontradikcije pretrazi ne smetaju |
| Interfejs | jedno jezgro (HTTP API), a preko njega MCP i kasnije web | povezivanje „preko bilo čega“ bez prepravki jezgra |
| Glavni chat | Claude Desktop preko MCP-a | ide na postojeću pretplatu, bez API troška |
| Pismo | čuva se original; pretraga radi nad latinicom bez kvačica | Whisper meša ćirilicu i latinicu, a upiti stižu bez kvačica |

**Zašto je registar likova deo prve faze.** Imena su zamka za svaki automatski korak:

- direktor Marjanović i Ćevara Marjanović su različiti likovi
- Dobrosavljev i Dobromirov zvuče skoro isto
- Miloš Torbica i Miloš portir dele ime
- Velinko i Jadranko Klać dele prezime

Registar zato ima kanonsko ime, sve nadimke i kratak opis po kome model razlikuje likove. Početni spisak od 36 likova daje vlasnik. Obrada predlaže nova imena, a vlasnik ih odobrava.

## Arhitektura i model podataka

Svaka epizoda prolazi isti niz koraka, a svaki korak upisuje svoj status u bazu. Obrada se zato može prekinuti i nastaviti bez ponavljanja.

**Tok obrade jedne epizode:**

1. **Preuzimanje.** `yt-dlp` skida samo zvuk i metapodatke. Zvuk se čuva kao Opus, mono.
2. **Priprema.** `ffmpeg` pravi WAV od 16 kHz za Whisper. Uvodna i odjavna špica se prepoznaju i preskaču, jer Whisper na muzici izmišlja tekst.
3. **Transkripcija.** Fajl ide na `/inference` postojećeg whisper-servera, sa jezikom `sr`, vremenskim oznakama po segmentu i početnim promptom koji sadrži imena likova.
4. **Govornici.** Za svaki segment se računa otisak glasa i poredi sa profilima trojice glumaca. Rezultat je glumac A, B, C ili „drugi“.
5. **Normalizacija.** Čuva se originalni tekst, latinična verzija i verzija bez kvačica za pretragu. Imena se ispravljaju prema registru.
6. **Obogaćivanje.** Jezički model čita transkript i registar likova i vraća JSON po šemi ispod.
7. **Indeksiranje.** Tekst ide u FTS5. Prozori od 60–90 s sa preklapanjem i sažeci dobijaju vektore.

**Tabele (SQLite):**

| Tabela | Sadržaj |
| --- | --- |
| `episodes` | redni broj, sezona, naslov, datum, YouTube ID, trajanje, status po koraku |
| `segments` | epizoda, početak, kraj, glumac, lik, tekst u tri oblika |
| `chunks` | prozori teksta sa vektorom |
| `enrichment` | JSON opis epizode, model i verzija prompta |
| `characters`, `aliases` | registar likova: kanonsko ime, vrsta, ko ga glumi, opis, nadimci |
| `mentions` | lik, epizoda, segment, vrsta: pojavljuje se, pominje se, imitiran je |
| `eval_queries` | test pitanja i očekivana epizoda |

Kasnije se dodaje tabela `facts` (subjekat, odnos, objekat, epizoda, segment), na kojoj počivaju biografije i graf veza.

**Šema obogaćivanja (JSON po epizodi):**

- sažetak u 3–5 rečenica i glavne teme
- aktuelni događaj koji epizoda komentariše, ako postoji
- početna scena: ko je u kancelariji, ko kasni ili ga nema
- likovi na ekranu i likovi koji se samo pominju, sa ID-jem iz registra
- priče i anegdote: ko priča, kome, o kome, iz kog perioda, kratak sadržaj
- imitacije i govor: ko koga imitira, kojim naglaskom ili dijalektom
- upečatljive replike, kratke, sa vremenom
- mesta, firme i predmeti
- nepoznata imena, kao predlozi za registar
- mesta gde model nije siguran

Polja „početna scena“, „priče“ i „imitacije“ postoje zbog prvog test-primera. Opis po sećanju se najčešće oslanja baš na njih.

**Struktura repoa:**

```
drzavni-posao/
  PLAN.md              ovaj dokument, izvezen kao Markdown
  CLAUDE.md            pravila iz odeljka „Polazno stanje“
  compose.yaml         servisi api, worker, embed
  .env.example         spisak tajni, bez vrednosti
  config.toml          adresa YouTube kanala, putanje, pragovi
  seed/characters.yaml početni registar likova (u gitu)
  docker/              Dockerfile za api i worker
  scripts/             deploy, logs, worker, shell, backup (pokreću se sa Mac-a)
  pyproject.toml       Python 3.12, uv
  dp/catalog/          katalog i preuzimanje
  dp/transcribe/       Whisper klijent, govornici, normalizacija
  dp/enrich/           prompt, šema, klijent za OpenAI-kompatibilan API
  dp/index/            FTS5, vektori
  dp/search/           HTTP API i MCP server
  dp/web/              Jinja2 šabloni i statički fajlovi, bez npm-a
  dp/eval/             test pitanja i merenje
  data/                baza, zvuk, transkripti (samo na mini PC-u, van gita)
  reports/             izveštaj posle svake faze
```

Klijent za obogaćivanje priča sa bilo kojim OpenAI-kompatibilnim servisom. Prelazak sa Qwen-a na Claude je zato promena podešavanja, a ne koda.

## Okruženje, Docker i isporuka

Sve novo radi u Docker Compose-u na mini PC-u. MacBook samo piše kod i šalje ga skriptom. Backend je Python, a u projektu nema React-a, npm-a ni koraka za build frontenda.

**Kontejneri:**

| Servis | Šta radi | Napomena |
| --- | --- | --- |
| `api` | FastAPI: HTTP API za pretragu, MCP na `/mcp`, web stranica | sluša samo na Tailscale adresi mini PC-a |
| `worker` | ceo lanac obrade: yt-dlp, ffmpeg, Whisper klijent, govornici, obogaćivanje, indeksiranje | zaustavlja se nezavisno od `api` |
| `embed` | bge-m3 na procesoru, zajednički za `worker` i `api` | npr. CPU slika text-embeddings-inference; Claude Code bira i obrazlaže |

Postojeći whisper-server i llama-server ostaju van Dockera, kao i do sada. Novi kontejneri ne koriste grafičku, pa ROCm u Dockeru nije potreban.

**Podaci:** jedan direktorijum na mini PC-u, montiran u kontejnere. U njemu su SQLite baza (WAL režim: `worker` piše, `api` čita), zvuk, transkripti i keš modela.

**Mreža ka postojećim servisima.** README postojećeg projekta kaže da kontejner u sopstvenoj compose mreži možda ne vidi 172.17.0.1. Faza 0 zato proverava redom:

1. podrazumevana compose mreža uz `extra_hosts: host.docker.internal:host-gateway`
2. ako to ne radi: `network_mode: bridge` za `worker` i `api`
3. poslednja opcija: `network_mode: host`

Izmene na postojećim servisima ili firewall-u nisu dozvoljene bez odobrenja vlasnika.

**Web bez npm-a:** Jinja2 šabloni koje renderuje server i malo običnog JavaScript-a. Ako zatreba više interaktivnosti, htmx ulazi kao jedan fajl u repou.

**Skripte za isporuku (pokreću se na Mac-u):**

| Skripta | Šta radi |
| --- | --- |
| `deploy.sh` | rsync koda na mini PC preko Tailscale SSH-a (bez `data/` i `.env`), pa tamo `docker compose up -d --build` |
| `worker.sh` | start, stop i status obrade |
| `logs.sh` | prati logove jednog ili svih servisa |
| `shell.sh` | ulaz u kontejner |
| `backup.sh` | kopija baze na Mac |

Slike se grade na mini PC-u, ne na Mac-u. Mac je ARM, a mini PC x86, pa bi slika napravljena na Mac-u bila za pogrešnu arhitekturu.

**Tajne:** `.env` postoji samo na mini PC-u (Claude API ključ, Hugging Face token, token za MCP) i nikad ne ide u git. U repou je samo `.env.example`.

### Kako se Claude Desktop povezuje na MCP na mini PC-u

MCP server radi na mini PC-u, u kontejneru `api`, kao Streamable HTTP na `/mcp`. Claude Desktop ga koristi preko malog lokalnog mosta.

1. Na Mac-u se instalira Python alat `mcp-proxy` (`uv tool install mcp-proxy`). On prima stdio od Claude Desktop-a i prosleđuje na mini PC preko Tailscale-a.
2. U `claude_desktop_config.json` dodaje se server:

```json
{
  "mcpServers": {
    "drzavni-posao": {
      "command": "/Users/<ime>/.local/bin/mcp-proxy",
      "args": ["--transport=streamablehttp",
               "--headers", "Authorization", "Bearer <token>",
               "http://<minipc>:8090/mcp"]
    }
  }
}
```

3. Claude Desktop se restartuje i alati se pojave u razgovoru.

Putanja do `mcp-proxy` mora biti puna, jer Claude Desktop ne vidi `PATH` iz terminala. Claude Code se povezuje direktno, bez mosta, preko `claude mcp add --transport http`.

„Add custom connector“ u podešavanjima ovde ne radi. Ta veza ide sa Anthropic-ovih servera, pa adresa mora biti javna, a mini PC je dostupan samo preko Tailscale-a. Ako projekat postane javan, `/mcp` dobija javnu HTTPS adresu (Tailscale Funnel ili Cloudflare Tunnel) i OAuth. Tada radi i u claude.ai na webu i telefonu.

## Faza 0: repo i katalog

Ishod je tabela `episodes` u kojoj svaka epizoda ima YouTube ID. Nema obrade zvuka.

1. Napraviti repo na MacBook-u po strukturi iznad, sa `CLAUDE.md` (pravila za mini PC) i `.env.example`.
2. Napisati skripte za isporuku i proveriti da `deploy.sh` podiže prazan `api` kontejner na mini PC-u.
3. Iz kontejnera proveriti da li odgovaraju whisper-server i llama-server, redom po opcijama iz odeljka o mreži. Bez ikakve izmene na njima.
4. Povući spisak svih videa sa kanala čija je adresa u config.toml: `yt-dlp --flat-playlist`, sa naslovom, trajanjem i datumom.
5. Povući spisak epizoda sa Vikipedije (sezona, broj, naslov, datum) i spojiti ga sa YouTube spiskom. Naslovi na YouTube-u obično nose redni broj epizode, pa je to glavni ključ.
6. Izdvojiti videe koji nisu epizode (najave, specijali, kompilacije) u posebnu listu.
7. Uneti početni registar likova iz seed/characters.yaml u bazu.
8. Podesiti MCP most na MacBook-u i proveriti da Claude Desktop vidi alat. Za sada alat vraća samo podatke iz kataloga.

**Gotovo kada:** najmanje 95% epizoda ima YouTube ID, a ostatak je na listi za ručni pregled. Isporuka radi jednim pozivom skripte, a Claude Desktop vidi MCP alat. Izveštaj navodi ukupan broj epizoda, ukupno trajanje i raspodelu trajanja.

Ukupno trajanje iz ovog koraka zamenjuje pretpostavku od 8 minuta po epizodi u svim procenama niže.

## Faza 1: pilot na 40 epizoda

Pilot provlači 40 epizoda kroz ceo lanac i daje brojeve na osnovu kojih se odlučuje o punoj obradi. Posle pilota se staje dok vlasnik ne pregleda izveštaj.

**Izbor epizoda:**

- 10 iz prvih 50, zbog pilot perioda
- 20 ravnomerno kroz sve sezone
- 5 sa gostima ili dvostrukim ulogama (Stevica, Boškić stariji, Duško, Jagoda, grof Sekereši, matičar); bira ih vlasnik
- 5 koje vlasnik dobro pamti, među njima bar jedna sa imitacijom dijalekta

**Koraci:**

1. Preuzeti zvuk za 40 epizoda. Proveriti da li YouTube nudi automatske titlove na srpskom i sačuvati ih za poređenje.
2. Proveriti parametre `/inference`: početni prompt, izlaz sa vremenskim oznakama, prihvaćeni format zvuka.
3. Transkribovati svih 40 i izmeriti brzinu.
4. Napraviti profile glasa: vlasnik potvrđuje po 2 minuta čistog govora za svakog od trojice glumaca. Zatim označiti govornike. Prvi pokušaj je ECAPA otisak glasa na procesoru, a rezerva je pyannote.
5. Izbrojati tokene transkripta Qwen-ovim tokenizatorom. Epizode koje ne staju u 8.192 tokena dele se na dva dela, pa se opisi spajaju.
6. Obogatiti svih 40 Qwen-om, sa isključenim razmišljanjem (`enable_thinking: false`).
7. Iste epizode obogatiti i sa Claude Haiku 5.5 i Sonnet 5.5 (odobreno). Trošak je ispod 1 USD.
8. Indeksirati i postaviti 15–20 test pitanja koja vlasnik napiše po sećanju.

**Šta se meri:**

| Mera | Kako | Prag |
| --- | --- | --- |
| Brzina Whisper-a | sati zvuka po satu rada | bar 4 puta brže od trajanja |
| Tačnost imena | ručna provera 5 epizoda | bar 90% imena iz registra ispravno posle normalizacije |
| Tačnost govornika | ručna provera 5 epizoda | bar 90% segmenata dužih od 3 s |
| Dijalekat | epizoda sa imitacijom | da li transkript čuva ijekavicu i lokalne reči |
| Kvalitet opisa | vlasnik ocenjuje 10 epizoda, bez oznake modela | Qwen je „dovoljno dobar“ u bar 8 od 10 |
| Pretraga | test pitanja | tačna epizoda u prvih 5 za bar 80% pitanja |

**Gotovo kada:** izveštaj sadrži sve mere, procenu trajanja pune obrade na osnovu izmerenih brzina i preporuku za model obogaćivanja.

**Odluke posle pilota:** Qwen ili Claude za obogaćivanje; da li MacBook preuzima deo transkripcije; da li YouTube titlovi ičemu služe.

## Faza 1b: pretraga sadržaja u Claude Desktop-u

Odluka vlasnika 15. 11. 2026: osnovna pretraga po sadržaju ide pre pune obrade, da bi se način korišćenja isprobao na pilot epizodama. Ovo je deo faze 3, premešten unapred; ostatak faze 3 ostaje gde jeste.

1. MCP alat `search_episodes` traži po sadržaju (transkript i opis, isti indeks kao `dp eval`) i vraća epizodu, minut, isečak i YouTube link na tačan minut. Pretraga naslova ostaje kao poseban alat.
2. MCP alati `get_episode` (sa opisom epizode) i `get_transcript_excerpt` (deo transkripta između dva vremena, sa govornicima).
3. Isto preko HTTP API-ja: `POST /search` i `GET /episodes/{id}/transcript`.
4. Servis `api` koristi `embed` za upite.

Pretraga vidi samo indeksirane epizode (posle faze 1: 40 pilot epizoda). Kako faza 2 obrađuje epizode, one ulaze u pretragu bez dodatnog posla.

**Gotovo kada:** vlasnik u Claude Desktop-u opiše epizodu iz pilota i dobije je sa linkom na tačan minut.

## Faza 2: puna obrada

Svih oko 2.500 epizoda prolazi isti lanac kao u pilotu, kao pozadinski posao na mini PC-u koji se sam nastavlja posle prekida.

1. **Radnik sa redom poslova.** Jedan proces uzima sledeću epizodu i sledeći korak iz baze. Radi u kontejneru worker, koji se zaustavlja i pokreće skriptom sa Mac-a.
2. **Preuzimanje sa pauzama.** Jedan video na 20–30 s, uz kolačiće iz browsera ako YouTube počne da odbija. Zvuk za sve epizode zauzima oko 7–10 GB.
3. **Obziran raspored.** Whisper dobija jedan deo snimka od 2–3 minuta u isto vreme, presečen na tišini, pa postojeći bot ne čeka duže od tridesetak sekundi. Qwen dobija jedan slot. Radnik pauzira u prozoru postojećeg bota i kad servis vrati grešku ili zauzeće.
4. **Rast registra.** Nepoznata imena iz obogaćivanja skupljaju se u listu predloga, sa brojem pojavljivanja. Vlasnik ih jednom nedeljno odobrava ili spaja sa postojećim likom.
5. **Drugi prolaz za imena.** Kad registar naraste, normalizacija imena i tabela `mentions` računaju se ponovo za sve epizode. To je jeftin korak, bez modela.
6. **Kontrola kvaliteta.** Automatski se označavaju sumnjive epizode: premalo teksta za trajanje, ponavljanje iste rečenice, više od 30% segmenata „drugi“. Vlasnik pregleda uzorak od 20.

**Nadzor opterećenja.** Puna obrada drži grafičku skoro stalno zauzetom oko nedelju dana, a procesor delimično. Za ovaj mini PC to je normalan rad, samo dugotrajan. Worker zato svakog minuta beleži temperature procesora i grafičke i pauzira kad pređu zadat prag. Prva 24 sata vlasnik prati izveštaj, pa se prag ili noćni režim po potrebi menjaju.

**Gotovo kada:** najmanje 98% epizoda ima transkript, govornike, opis i indeks. Ostatak je na listi sa razlogom.

## Faza 3: servis za pretragu i povezivanje

Osnovna pretraga sadržaja (tačke 1, 2 i 4 ispod, i alati `search_episodes`, `get_episode`, `get_transcript_excerpt`) urađena je unapred u fazi 1b. U fazi 3 ostaju filteri po strukturi, epizode po liku, prag „nije pronađeno“ i web stranica.

Pretraga je jedan HTTP servis na mini PC-u. Chat bot ga koristi kao alat, a model u chatu čita kandidate i presuđuje.

**Kako servis traži:**

1. Pretraga po rečima nad transkriptom i opisima (FTS5, trigram, bez kvačica). Hvata replike i imena, i kad su zapamćeni približno.
2. Pretraga po smislu nad prozorima teksta i sažecima (bge-m3). Hvata opise situacije.
3. Filteri po strukturi: ko govori, ko se pominje, ko je u početnoj sceni, ima li imitacije, period, sezona.
4. Spajanje tri liste u jednu (reciprocal rank fusion). Vraća se 20 kandidata sa isečkom, vremenom i YouTube linkom.

**Krajnje tačke:**

| Tačka | Namena |
| --- | --- |
| `POST /search` | upit i opcioni filteri; vraća kandidate |
| `GET /episodes/{id}` | metapodaci i opis epizode |
| `GET /episodes/{id}/transcript` | deo transkripta između dva vremena |
| `GET /characters` | registar likova, pretraga po nadimku |
| `GET /characters/{id}/episodes` | epizode u kojima se lik pojavljuje ili pominje |

MCP server izlaže iste mogućnosti kao alate: `search_episodes`, `get_episode`, `get_transcript_excerpt`, `find_character`. Opis alata upućuje model da opis razloži na signale, pokrene više pretraga, pročita isečke i vrati najviše 3 kandidata sa obrazloženjem.

**Načini povezivanja:**

| Klijent | Kako | Kada |
| --- | --- | --- |
| Claude Desktop na MacBook-u | lokalni most `mcp-proxy` prosleđuje na `/mcp` preko Tailscale-a | sada |
| Claude Code | direktno na `/mcp` preko HTTP-a | sada |
| Skripte i drugi alati | HTTP API sa OpenAPI opisom | sada |
| Web stranica | Jinja2 stranica na istom servisu, preko Tailscale-a | osnovna pretraga uz fazu 3 |
| Lokalni chat (Open WebUI i Qwen) | HTTP API kao alat | po želji |
| claude.ai na webu i telefonu | custom connector na javnu HTTPS adresu, uz OAuth | ako projekat postane javan |

Servis sluša samo na Tailscale adresi mini PC-a. Postojeći servisi ostaju nedostupni spolja, kao i do sada.

**Gotovo kada:** vlasnik u Claude Desktop-u opiše prvi test-primer i dobije kandidate sa linkom na tačan minut.

## Faza 4: evaluacija

Prva faza projekta je završena kada je tačna epizoda u prvih 5 rezultata za bar 80% test pitanja, a na prvom mestu za bar 60%.

1. Vlasnik piše 30–50 opisa epizoda koje zna, onako kako ih pamti: replika, situacija, ko o kome priča, način govora, približan period.
2. Za svaki opis upisuje se očekivana epizoda u `eval_queries`.
3. Skripta pušta sva pitanja kroz `/search` i računa pogodak u prvih 1, 5 i 20.
4. Za svaki promašaj beleži se uzrok: greška u transkriptu, pogrešan govornik, lik nije prepoznat, opis epizode ne pominje bitan detalj, ili rangiranje.
5. Popravke idu redom po broju promašaja koje rešavaju. Skripta se pušta posle svake izmene, da se vidi i šta se pokvarilo.

Merenje ide dva puta: samo servis (ponovljivo, bez modela) i ceo razgovor u Claude Desktop-u na uzorku od 10 pitanja.

**Gotovo kada:** oba praga su dostignuta i prvi test-primer je pronađen.

## Kasnije faze

Ove faze se planiraju detaljno tek kad pretraga radi. Ovde je samo okvir, da model podataka od početka ostavi mesta za njih.

**Faza 5: likovi i kanon.**

- Iz svake epizode izvlače se činjenice u tabelu `facts`, svaka sa epizodom i vremenom kao izvorom.
- Kontradikcije se ne brišu. Svaka verzija činjenice ostaje, sa rasponom epizoda u kome važi.
- Granica pilot perioda traži se u podacima: od koje epizode se osnovne činjenice o glavnim likovima više ne menjaju. Vlasnik tu granicu potvrđuje ili pomera.
- Biografije piše model iz odobrenih činjenica, sa fusnotama na epizode. Likovi koji postoje samo kroz priču dobijaju isti tretman kao oni sa ekrana.
- Graf veza (rodbinske, poslovne, prijateljske) crta se iz iste tabele.

Ovo je mesto gde se Claude najviše isplati, jer traži pažljivo čitanje i poravnavanje preko stotina epizoda.

**Faza 6: mape.** Iz činjenica se izdvajaju mesta i odnosi među njima (sprat, sektor, ulica, „preko puta“). Model predlaže nacrt organizacije preduzeća i mape grada, a vlasnik ga ručno doteruje.

**Faza 7: javna verzija.** Web interfejs nad istim API-jem. Javno bi išli sažeci, likovi, kratki isečci i linkovi na YouTube, a celi transkripti ostaju interni. Pre objave treba proveriti autorska prava i, po mogućstvu, javiti se autorima serije.

## Procena vremena i troškova

Lokalna obrada ne košta ništa i traje jednu do dve nedelje rada mašine. Claude API bi za opise svih epizoda koštao od oko 1,5 USD (Haiku 5.5) do oko 28 USD (Sonnet 5.5), jednokratno.

**Pretpostavke:** 2.534 epizode po 8 minuta u proseku, što je oko 338 sati zvuka. Po epizodi oko 6.000 ulaznih tokena (transkript oko 4.500, uputstvo i registar oko 1.500) i oko 1.000 izlaznih za opis. Stvarni brojevi mogu biti upola manji ili duplo veći; pilot ih zamenjuje izmerenim.

**Lokalna obrada (0 USD):**

| Korak | Procena | Osnov |
| --- | --- | --- |
| Preuzimanje zvuka | oko 1 dan | jedan video na 25 s |
| Transkripcija | 50–85 sati rada, realno oko nedelju dana | procena 4–7 puta brže od trajanja, iz izmerenih 1,2 s enkodera po prozoru od 30 s |
| Govornici | meri se u pilotu | radi na procesoru, uporedo sa ostalim |
| Opisi (Qwen) | oko 54 sata, 2–3 dana | oko 77 s po epizodi pri 265 i 18,5 tokena u sekundi |
| Vektori | nekoliko sati | bge-m3 na procesoru, oko 40.000 prozora |

Transkripcija i opisi mogu da teku uporedo, jer koriste različite servise. MacBook može da preuzme deo transkripcije istim modelom, ako pilot pokaže da vredi.

**Claude API, jedan prolaz kroz sve epizode (USD):**

| Model | Cena ulaz / izlaz po milion tokena | Opisi epizoda | Opisi, Batch API | Činjenice za fazu 5, Batch API |
| --- | --- | --- | --- | --- |
| Haiku 5.5 | 0,10 / 0,50 | 2,8 | 1,4 | 1,7 |
| Sonnet 5.5 | 2 / 10 | 56 | 28 | 34 |
| Opus 5.5 | 4 / 20 | 111 | 56 | 68 |
| Fable 5.2 | 8 / 40 | 223 | 112 | 137 |

Račun: 15,2 miliona ulaznih i 2,5 miliona izlaznih tokena za opise; za činjenice 3,8 miliona izlaznih. Batch API daje 50% popusta, a rezultati stižu sa zadrškom, što ovde ne smeta. Cene su sa [zvanične stranice](https://platform.claude.com/docs/en/about-claude/pricing).

**Napomene:**

- Batch cena traži Anthropic-ov Batch API, pa bi za nju trebalo dopisati mali poseban klijent.
- Razgovor u Claude Desktop-u ide na pretplatu i ne troši API.
- Poređenje modela na 40 pilot epizoda košta manje od 1 USD i jedino može da pokaže da li se plaćanje isplati.

**Preporuka:** krenuti lokalno. Ako pilot pokaže da Qwen-ovi opisi promašuju likove ili priče, prvi korak je Haiku 5.5, a drugi Sonnet 5.5. Za fazu 5 Claude vredi razmotriti nezavisno od ishoda pilota. Vlasnik je odobrio jednokratni trošak do 30 USD.

## Rizici i otvorena pitanja

Najveći rizik je kvalitet transkripta za vojvođanski govor, žargon i imena. Zato pilot dolazi pre svega ostalog.

| Rizik | Posledica | Ublažavanje |
| --- | --- | --- |
| Whisper greši imena i žargon | pretraga po imenu promašuje | početni prompt sa imenima, ispravka prema registru, približna pretraga |
| Whisper „ispravlja“ dijalekat u standardni jezik | gubi se trag imitacije | polje „imitacije“ u opisu; proveriti u pilotu |
| Početni prompt prima oko 220 tokena | ne staje ceo registar | u prompt idu najčešća imena, ostala rešava ispravka |
| Kratke replike i upadice | pogrešan govornik | označavaju se samo segmenti duži od 1,5 s, ostale rešava model iz konteksta |
| Duga epizoda ne staje u 8.192 tokena | obogaćivanje pada | podela na dva dela i spajanje opisa |
| YouTube odbija masovno preuzimanje | zastoj | pauze, kolačići, nastavak od mesta prekida |
| Deljeni servisi | postojeći bot usporava | jedan slot, jedan fajl, pauza u njegovom prozoru |
| Qwen slabije razume humor i aluzije | siromašni opisi | poređenje sa Claude-om u pilotu |

**Odgovori vlasnika, 12. novembar 2026:**

| Pitanje | Odgovor |
| --- | --- |
| Večernji posao postojećeg bota | traje do sat vremena od 19:00; obrada pauzira 18:55–20:00 |
| Opterećenje mini PC-a | verovatno sme; nadzor temperature i pauza na pragu (faza 2) |
| 10 epizoda po izboru vlasnika | vlasnik bira naknadno, pre početka faze 1 |
| Claude API | odobreno do 30 USD jednokratno |
| Hugging Face | vlasnik ima nalog i daje token ako zatreba; token ide u `.env` |

## Izvori

- [Cene Claude API-ja, Claude Platform Docs](https://platform.claude.com/docs/en/about-claude/pricing)
- [Spisak epizoda, Vikipedija](https://en.wikipedia.org/wiki/List_of_Dr%C5%BEavni_posao_episodes)
- [Broj sezona i epizoda (podaci TMDB-a)](https://watch.mewayz.com/show/46831)
- Podaci o mini PC-u, servisima i izmerenim brzinama: beleške vlasnika od 12. novembra 2026.
