# Faza 1 – pilot (40 epizoda): izveštaj

Stanje 14. 11. 2026. Svih 40 pilot epizoda prošlo je ceo lanac: preuzimanje, priprema, transkripcija,
govornici, opis i indeks. Pretraga na test pitanjima prolazi pragove iz PLAN-a. Dve mere nisu
dostignute (brzina Whisper-a, Qwen za opise), a jedna nije izmerena (tačnost imena). Za njih su ispod
predlozi.

## Mere

| Mera | Prag | Rezultat | Ocena |
| --- | --- | --- | --- |
| Brzina Whisper-a | 4× brže od trajanja | **1,78×** (329 min zvuka; ponovljeni delovi uračunati) | ne |
| Tačnost imena | 90% posle normalizacije | **nije izmereno**: korak normalizacije imena još ne postoji | — |
| Tačnost govornika | 90% segmenata > 3 s, 5 epizoda | **95%** (194/204), ručna provera 3 epizode | da |
| Dijalekat | čuva ijekavicu i lokalne reči | čuva („vidjet ćemo“, „cijela večer“, „djete“) | da |
| Kvalitet opisa | Qwen „dovoljno dobar“ u 8 od 10 | Qwen nije ni ušao u ocenu (vidi dole); **Sonnet 3,75/5** | Qwen ne |
| Pretraga | tačna epizoda u prvih 5 za 80% | **11/13 (85%)**; na 1. mestu **9/13 (69%)** | da |

## Transkripcija

- Whisper (large-v3, deljeni server) radi 1,78× brže od trajanja zvuka. Prosečna epizoda traje 8,2 minuta.
- **Petlje.** Whisper se na muzici i tišini vrti u krug. Rešeno je ovako:
  - prvih i poslednjih ~30 s (špica) idu kao posebni delovi;
  - prepoznaju se ponovljeni redovi i „razvučeni“ segmenti;
  - takav deo se ponavlja u komadima od 30 s, sa početnim promptom pa bez njega;
  - na kraju se izbacuju poznate halucinacije („Hvala na gledanju“, „Amara.org“…).

  Posle toga nijedna od 40 epizoda nema petlju. Ponavljanje je bilo potrebno za 26 delova u celom pilotu.
- **Greške prepoznavanja ostaju**, i ključne su za kvalitet (primer iz „Narkotika“):
  - „Govni arhivator Čarko Đorđe“ umesto „glavni arhivator Čvarkov Đorđe“;
  - „Dragan Obrođak“ umesto „Draganov rođak“.
- **YouTube titlovi:** nijedna epizoda nema ni automatske ni ručne titlove. Ne služe ničemu.

## Govornici

- **Kako radi:** ECAPA otisak glasa na procesoru, ~4 s po epizodi. Profili su napravljeni od isečaka koje si
  izabrao (2–3,4 minuta po glumcu, iz 8 epizoda); tihi delovi isečaka se izbacuju.
- **Tvoja provera (254, 612, 2194):**
  - kad model kaže ime glumca, tačan je 349/353 puta;
  - prvobitna oznaka „drugi“ bila je pogrešna 40/46 puta. To su promenjeni glasovi: drogirani Dragan,
    Čvarkov na telefonu, Čvarkov kao Dođoš.

  Zato sada svaki segment dobija najbliži glas, a nesigurni nose upitnik (`<cvarkov?>`). „Drugi“ ostaje
  samo za muziku i reklame.
- **Segmenti kraći od 1,5 s** ostaju bez oznake (40% segmenata, ali mali deo teksta).

## Opisi epizoda (obogaćivanje)

| Model | Uspelo | s/epizodi | Slepa ocena v1 | Slepa ocena v2 | Cela serija |
| --- | --- | --- | --- | --- | --- |
| Qwen (lokalni) | 29/40 | 183 | — | — | 0 $, ~6–7 dana jedinog slota |
| GLM-5.3 (z.ai) | 40/40* | 21 | 2,8 | — | ~40 $ |
| Haiku 5.5 | 40/40 | 19 | 3,1 | 3,05 | ~7 $ (batch ~3,7 $) |
| **Sonnet 5.5** | 40/40 | 12 | 3,0 | **3,75** | ~84 $ (**batch ~42 $**) |

\* 4 epizode su posle prekida mreže uspele iz drugog pokušaja.

- **Qwen.** Bez nametnute šeme izmišlja svoja polja. Sa šemom 11/40 epizoda puca: vrti se u krug do
  granice od 4.000 tokena (prepisuje ceo transkript kao replike, ponavlja iste likove). Uz to deli jedini
  slot sa drugim projektom. Ne preporučujem ga.
- **Prompt v2** donosi:
  - transkript sa oznakom glasa i objašnjenjem da glas daje glumca, a ne lik;
  - jasno pravilo da su „Dragan“ i „Torbica“ jedan lik;
  - polje za javne ličnosti;
  - `seed/background.md`.

  Sonnet je od toga dobio ¾ ocene, a Haiku nije dobio ništa.
- **Šta i dalje fali (tvoje napomene):**
  - „fora“ i preokret na kraju epizode (2111, 768, 2194);
  - ko koga imitira (699);
  - pozadina sporednih likova.

  Sporedne likove si dopisao 14. 11. (vidi „Registar likova“), pa sledeći opisi to već dobijaju.

**Potrošeno:** Anthropic ~4 $ od 5 $ kredita, z.ai ~0,6 $.

## Indeks i pretraga

- **Šta se indeksira:** prozori transkripta (75 s, novi svakih 40 s, sa imenom govornika kad je glas
  siguran) i Sonnetov opis (ceo opis, plus po jedan deo za svaku priču i imitaciju, sa vremenom).
- **Kako se traži:**
  - FTS5 bez kvačica za reči, bge-m3 vektori za smisao;
  - dve liste se spajaju (RRF), po jedna epizoda sa minutom.
- **Brzina:** indeks traje ~11 s po epizodi na procesoru.
- **Servis `embed`** (TEI, bge-m3, 4 jezgra, samo 127.0.0.1) radi stalno.
- **Rezultati, 16 pitanja (13 za indeksirane epizode):**
  - na 1. mestu 9, u prvih 5 njih 11, u prvih 20 njih 12;
  - **254** (Dragan glumi drogiranog) promašen: ni transkript ni opis ne kažu to jasno;
  - **426** („prvi put Boškićev otac“) na 11. mestu: pilot ne može da zna „prvi put“;
  - **3 pitanja van pilota** uvek vrate neku epizodu. Odgovor „nije pronađeno“ traži prag pouzdanosti
    (faza 3).

## Procena pune obrade (2.557 epizoda, 372 h zvuka)

| Korak | Trajanje |
| --- | --- |
| Whisper (1,78×) | ~210 h |
| Preuzimanje sa pauzom 20–30 s | ~18 h |
| Govornici i indeks | ~11 h |
| Opisi (Sonnet, Batch API) | ~1 dan čekanja, paralelno |
| **Ukupno**, uz dnevnu pauzu 18:55–20:00 | **~10–11 dana** |

## Odstupanja od PLAN-a

- **Opisi:** Claude Sonnet umesto Qwen-a. Trošak ~42 $ sa Batch API-jem, više od odobrenih 30 $.
- **Špica:** ne seče se nego ide kao poseban deo, pa se dijalog uz muziku ne gubi (odobreno).
- **Vektori:** pretraga kroz numpy, ne kroz sqlite-vec. Za 2.557 epizoda (~60.000 vektora) to je dovoljno
  brzo i ne traži novu zavisnost. sqlite-vec po potrebi kasnije.
- **Profili glasa:** iz više kraćih isečaka umesto 2 minuta neprekidnog govora (odobreno).
- **Ručna provera govornika:** 3 epizode umesto 5.

## Problemi i preporuke za fazu 2

1. **YouTube 403:** povremen, prolazi iz drugog pokušaja. Treba automatsko ponavljanje sa
   pauzom.
2. **Batch API za Sonnet:** još nije napisan (sada je pojedinačni poziv). Prepolovljuje cenu.
3. **Normalizacija imena:** korak iz PLAN-a (tačka 5) još ne postoji. Ispravlja imena iz registra u
   transkriptu („Čarko“ → Čvarkov). Prag „tačnost imena“ meri se posle njega.
4. **SSH sa Mac-a:** posle prekida mreže deljeni kanal se zaglavi i komande vise. `common.sh` treba sam
   da ga obnovi.
5. **`uv sync` pri gradnji slike** povremeno padne (mreža). Drugi pokušaj prolazi.
6. **Prag „nije pronađeno“** za pitanja van baze (faza 3).

## Registar likova (pregled posle tvojih dopuna)

- **Pismo:** ćirilica iz Wikipedie prebačena je u latinicu; sadržaj nije menjan.
- **Dopunjeno sa sr.wikipedia** (stranica serije):
  - prazni opisi: Ličina, Lončar, Arpad Sabo, Debela Vukosava, Magda, Rade Kornjača, Dane od Šljive do
    Banane;
  - kratki opisi: Velinko i Jadranko Klać (otac i sin), Miloš portir, Duško poštar, Jagoda, Ristić,
    Dobrosavljev, Dobromirov, Tetak;
  - nadimci: Seki/Danilo/Dane za Sekerešija, Žika/Žiko, Duško Tegeltija, Jagoda Marko, gospođa fon
    Šilović, Stevo Višekruna.
- **Učitano u bazu:** 37 likova.

**Odluke vlasnika (14. 11.):**

- Boškića je zaposlio brat.
- Stevica je Đorđev brat (Wikipedia kaže „brat od strica“, važi tvoja verzija).
- Sekereši (Dane) i Dane od Šljive do Banane su dva lika.
- Mitar, Joja i Ildika za sada ostaju bez opisa.
- Dodato je 29 novih likova sa Wikipedie (grupa `sa_wikipedije`); registar sada ima 66 likova.
- Magda je prebačena u `samo_glas`, jer se na telefonu čuje samo njen glas.

Faza 1 je odobrena, kao i preporuke za fazu 2 i Sonnet sa troškom preko 30 $.

**Pitanja koja su bila postavljena:**

1. **Ko je zaposlio Boškića?** Tvoj opis kaže otac (Boškić stariji), a Wikipedia (i tekst koji si
   prekopirao) kaže stariji brat.
2. **Stevica:** ti si napisao „brat“, a Wikipedia kaže „bratić“, poslastičar koji prodaje slatkiše po
   vašarima i duguje Đorđu novac.
3. **„Dane“:** isti nadimak imaju bivši grof Sekereši (Danilo) i Dane od Šljive do Banane. Da li je to ista
   osoba?
4. **Prazni opisi:** Mitar Mitra Mitar, Joja i Ildika i dalje nemaju opis; Wikipedia ih ne opisuje.
5. **Novi likovi?** Wikipedia pominje likove kojih nema u registru, a pojavljuju se u opisima pilota.
   Na primer: Boškićev brat, Lazar Malinecki, kum Todor, Mirko Cemeš, Bela Mihalj, Boban i Deki, Anja,
   Vida, Šicer. Da li da ih dodam? Bez tvog odobrenja ne dodajem.

## Odluke posle pilota (za tebe)

1. **Model za opise:** predlažem Sonnet preko Batch API-ja, ~42 $ jednokratno. Treba dopuniti Anthropic
   kredit i odobriti iznos preko 30 $.
2. **MacBook za transkripciju:** predlažem da ga ne uključujemo. 10–11 dana na mini PC-u je prihvatljivo,
   a MacBook bi tražio poseban Whisper i prenos zvuka.
3. **YouTube titlovi:** napuštamo ih, jer ne postoje.
4. **Odgovori na pet pitanja o registru likova** (iznad).
5. **Odobrenje faze 1**, pa prelazak na fazu 2.
