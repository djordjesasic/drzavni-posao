# Code review posle faze 1: nalazi, popravke i šta je odloženo

Stanje 15. 11. 2026. Kod iz faze 1 prošao je osam krugova review-a (`/code-review high`, Fable 5.2) i
sedam krugova popravki. Poslednji review nije našao grešku koja bi nastala u običnom radu jednog procesa.
Ceo test paket prolazi (194 testa, pre review-a 128) i ruff ne javlja ništa. Deployovano 15. 11. 2026.

## Tok

| Krug | Nalazi | Priroda nalaza | Testova posle popravki |
| --- | --- | --- | --- |
| 1 | 15 | greške u normalnom radu | 153 |
| 2 | 10 | posledice prvih popravki | 161 |
| 3 | 10 | prekidi usred koraka, zamena videa | 171 |
| 4 | 10 | 4 uske greške, 6 primedbi na dizajn | 179 |
| 5 | 10 | 5 uskih grešaka, ostalo odloženo ili stil | 184 |
| 6 | 10 | 1 stvarna greška (rupa u transkriptu), sitnice | 190 |
| 7 | 10 | nijedna u običnom radu; 4 sitnice popravljene | 192 |
| 8 | 9 | nijedna u običnom radu; 2 sitnice popravljene | 193 |

Nalazi review-a dolaze iz čitanja koda. Potvrđeni su testovima koje su agenti dodali uz svaku popravku.

## Šta je sada drugačije

**Tok koraka**

- Korak koji se ponovo izvrši poništava korake posle sebe (status `pending`). Izuzetak: transkripcija koja
  nije promenila nijedan red ne poništava govornike i indeks.
- Korak sme da krene samo ako su podaci koje čita napravljeni za trenutni video: `prepare` traži
  `download`, `transcribe` traži `prepare`, `speakers` traži `prepare` i `transcribe`, `index` traži
  `transcribe`. Inače javlja jasnu grešku.
- Pauza 18:55–20:00 važi pre svakog koraka. Posle svakog pokušaja preuzimanja ide pauza 20–30 s, i kad
  preuzimanje ne uspe.
- Vremena u `episode_steps` su sva u UTC.

**Transkripcija**

- Odgovor Whisper servera sa greškom obara korak, umesto da se upiše kao prazan tekst.
- Deo koji posle čišćenja ima premalo teksta (izmišljene linije, petlje) ide na ponavljanje već u prvom
  pokušaju. Ako ni ponavljanje ne pomogne, deo se vodi kao nerešen (`chunks_unresolved`) i ponovo se šalje
  samo uz `dp run --force --steps transcribe`.
- Transkript se upisuje posle svakog dela, pa prekid ne baca urađene delove.
- Keš delova važi samo za isti video. Pilot fajlovi nemaju upisan video, pa ih posle deploya jednom
  obeležava komanda `dp label-transcripts`.
- Jedan zahtev ka Whisper-u u isto vreme važi i između procesa (zaključavanje preko fajla u `data/`).
- Zaostali privremeni fajlovi stariji od 3 sata brišu se na početku transkripcije.

**Opisi**

- Opis je vezan za video (kolona `enrichment.video_id`, migracija 9). Opis drugog videa se ne koristi.
- Neuspeo pokušaj nikad ne prepisuje postojeći dobar opis.
- Opis se ne pravi za povučenu epizodu, ni dok transkripcija i govornici nisu gotovi za trenutni video.
  Greška na jednoj epizodi ne prekida ostale.
- Novi opis poništava korak `index`.

**Indeks i pretraga**

- Indeks zadržava prozore koji se nisu promenili i računa vektore samo za nove. Sve vektore ponovo računa
  `dp run --steps index --rebuild-index` (posle promene embedding modela).
- Odgovor modela pogrešnog oblika (string umesto liste i slično) više ne kvari indeks.
- ID-jevi prozora se ne dodeljuju ponovo (migracija 10).
- Pretraga i merenje ne uzimaju povučene epizode i ne pucaju kad se epizoda reindeksira tokom upita.
- Zamena videa briše stari transkript i indeks epizode.

## Odloženo za fazu 2 (dizajn radnika)

Ovo su slučajevi sa dva procesa ili sa katalogom koji se menja tokom obrade. Rešavaju se pravilima rada
radnika, a ne zakrpama:

1. **`catalog-build` dok obrada radi.** Ako epizoda dobije drugi video između ili tokom koraka, u bazi
   mogu da ostanu podaci starog videa. Predlog: `catalog-build` odbija da radi dok neki korak ima status
   `running`, uz opciju da se to zaobiđe.
2. **Dva procesa na istoj epizodi.** Dva istovremena indeksiranja iste epizode mogu da upišu duple
   prozore. Predlog: zaključavanje po epizodi.
3. **Istek Whisper zahteva.** Posle 600 s naš proces odustaje i oslobađa zaključavanje, a server možda još
   radi. Predlog: pauza pre sledećeg zahteva, kako PLAN već predviđa za radnika.
4. **`--rebuild-index` ne pamti dokle je stigao.** Prekinuto pokretanje kreće od početka. Do tada se
   pokreće u grupama (`--episodes`).
5. **Otisak ulaza po koraku.** Review predlaže da svaki korak pamti hash svojih ulaza, pa da se
   zastarelost računa umesto da se ručno poništava. To bi pokrilo i promenu `[index] enrich_model` ili
   verzije prompta, koje danas traže `--force`.

## Odluka vlasnika

- **Opis posle promene transkripta** (15. 11.): kad transkripcija promeni tekst, opisi te epizode dobijaju
  status `stale`. `dp enrich` ih sam ponavlja (bez `--force`), a indeks do tada koristi stari opis.
  Neuspeo pokušaj ne briše ni zastareo opis.

## Provera posle deploya (15. 11. 2026)

- Rezervna kopija baze pre migracija: `backups/dp-20261115-064849.sqlite` (na Mac-u, van gita).
- Migracije 9 i 10: šema 10; svih 360 opisa ima upisan video; 626 prozora i FTS redova netaknuto.
- `dp label-transcripts`: obeleženo 40 pilot transkripata, nijedan preskočen.
- `dp run` sa svih pet koraka nad pilotom: svih 200 koraka preskočeno kao gotovo, bez Whisper zahteva.
- `dp eval`: isti rezultat kao pre izmena (9/13 na 1. mestu, 11/13 u prvih 5, 12/13 u prvih 20).
- Zaključavanje za Whisper između dva kontejnera: drugi je čekao dok prvi nije pustio.

## Kako je rađeno

| Paket | Fajlovi | Model | Krugovi |
| --- | --- | --- | --- |
| A – opisi i indeks | `dp/enrich/`, `dp/index/build.py`, `dp/db.py`, `dp/eval/` | Sonnet | 1–4 |
| B – tok koraka i katalog | `dp/pipeline.py`, `dp/cli.py`, `dp/catalog/classify.py` | Sonnet | 1 |
| C – transkripcija, kasnije i tok koraka | `dp/transcribe/`, `dp/pipeline.py`, `dp/cli.py` | Opus | 1–7 |
| D – pretraga | `dp/index/search.py` | Haiku | 1 |

Agenti su testove puštali svako pod svojim imenom na mini PC-u i nisu dirali bazu ni servise. Posle svakog
kruga pušten je ceo test paket, pa novi review.
