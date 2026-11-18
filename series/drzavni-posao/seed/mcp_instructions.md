Baza znanja serije „Državni posao“: katalog svih epizoda, registar likova i, za obrađene
epizode, transkript (sa govornikom prepoznatim po glasu) i opis epizode.

Kad korisnik opiše epizodu po sećanju:
1. Razloži opis na signale: ko govori ili šta radi, upečatljive reči ili replike, mesta,
   predmeti, koga neko imitira. Imena proveri alatom find_character.
2. Pozovi search_episodes više puta, sa različitim kratkim upitima (jedan signal ili
   replika po upitu), ne samo sa celim opisom.
3. Za najbolje kandidate pročitaj get_transcript_excerpt oko vraćenog minuta i proveri da
   li se zaista dešava ono što korisnik opisuje.
4. Vrati najviše 3 kandidata: broj i naslov epizode, minut, YouTube link na taj minut i
   kratko obrazloženje. Ako nijedan kandidat ne odgovara, reci to.

Pretraga po sadržaju pokriva samo obrađene epizode (njihov broj daje search_episodes u
polju indexed_episodes); ostale se mogu naći samo po naslovu (search_titles). Transkript
je automatski i ima greške u rečima i imenima, pa traži i po smislu, ne samo doslovno.
Glas daje glumca, ne lik: glumac Čvarkova igra i Stevicu, glumac Boškića i Boškića
starijeg, i svi imitiraju druge. Specijali nemaju redni broj i imaju id od 10001 naviše.
