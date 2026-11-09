# akcije-agent

Skuplja akcije iz prodavnica u Crnoj Gori i 2× sedmično šalje email sa proizvodima koji ispunjavaju pravila. Uz email objavljuje i web stranicu sa svim akcijama.

**Status (8.11.2026):** sve 4 prodavnice rade (puna proba: 5012 akcija, 392 ≥30%, ~6,5 min). Proizvodi su razvrstani u zajedničke kategorije. Stranica (sa šoping listom) se objavljuje na Vercelu automatski poslije svakog pokretanja na GitHubu.

## Pokretanje lokalno (Windows)

```powershell
cd akcije-agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env      # pa upiši RESEND_API_KEY, REPORT_TO i (opciono) GEMINI_API_KEY, SITE_URL
python -m pytest -q         # testovi, rade bez interneta
python -m akcije --dry-run  # skine akcije, sačuva izvjestaj.html i site/akcije.json, BEZ emaila i BEZ upisa istorije
python -m akcije            # skine akcije, pošalje email i upiše istoriju cijena (data/istorija.sqlite)
```

Lokalna istorija (`data/istorija.sqlite`) nije u gitu; na GitHubu istorija živi na posebnom branchu `data` (vidi dolje).

Opcije: `--min-discount 40` (prag popusta), `--samo-nove` (u emailu samo nove akcije), `-v` (detaljan log), `--output fajl.html` (za `--dry-run`), `--no-history`.

### Keš za lokalno testiranje

Da ne skidaš sajtove i ne trošiš Gemini limit pri svakom pokretanju:

```powershell
python -m akcije --dry-run --cache          # uzme ponude iz .cache/ ako nisu starije od 12 h; inače skine i sačuva ih
python -m akcije --dry-run --refresh-cache  # uvijek skine sveže ponude i prepiše .cache/
python -m akcije --dry-run --cache -v       # isto, sa detaljnim logom
rmdir /s /q .cache                          # obriši keš (PowerShell: Remove-Item -Recurse .cache)
```

- U `.cache/` je po jedan JSON fajl za svaku prodavnicu (`voli.json`, `cosmetics.json`, `kuca-hemije.json`, `idea.json`). U njemu su gotove ponude, već obrađene preko Gemini-ja, i vrijeme kad je keš napravljen. Odluke Gemini-ja za muški filter čuvaju se u `muski-filter.json`.
- U logu za svaku prodavnicu piše odakle su podaci, npr. `Voli: 412 ponuda iz keša (staro 2 h 15 min)` ili `Idea: 35 ponuda, sveže skinuto (sačuvano u keš)`.
- Ako prodavnica javi grešku, greška se ne kešira i sljedeće pokretanje pokušava ponovo. Oštećen fajl u kešu se ignoriše uz upozorenje.
- Keš je samo za razvoj. `--cache` i `--refresh-cache` rade isključivo uz `--dry-run` (bez njega program prekida). Istorija cijena, pa ni „prethodni izvještaj“ za `ONLY_NEW`/`--samo-nove`, se ne mijenja. `.cache/` je u `.gitignore`, a GitHub Actions keš nikad ne koristi.
- Kao i obično `--dry-run`, i ovo prepisuje `izvjestaj.html` i `site/akcije.json`.

Pregled stranice lokalno: `python -m http.server 8765 --directory site`, pa otvori http://127.0.0.1:8765/ (direktno otvaranje `index.html` iz foldera ne radi, jer pretraživač ne dozvoljava `fetch` sa `file://`).

`REPORT_TO` mora biti adresa kojom si se registrovao na Resend. Bez sopstvenog domena Resend šalje samo na nju.

## Varijable (.env lokalno, GitHub Secrets/Variables na serveru)

| Ime | Gdje na GitHubu | Obavezno | Šta radi |
|---|---|---|---|
| `RESEND_API_KEY` | Secret | da | ključ za slanje emaila |
| `REPORT_TO` | Secret | da | primalac emaila |
| `GEMINI_API_KEY` | Secret | ne | besplatan ključ sa https://aistudio.google.com/apikey. Bez njega se Idea preskače (piše se upozorenje u emailu), a nejasni „muški“ proizvodi se izostavljaju |
| `GEMINI_MODEL` | – | ne | drugi model, podrazumijevano `gemini-flash-lite-latest` (brz; `gemini-flash-latest` je ~50× sporiji jer „razmišlja“) |
| `MIN_DISCOUNT` | Variable | ne | prag popusta, podrazumijevano 30 |
| `SITE_URL` | Variable | ne | adresa stranice (npr. `https://akcije-xyz.vercel.app`). Ako postoji, email je kratak sa linkom; ako ne, email ima punu tabelu |
| `ONLY_NEW` | Variable | ne | `1` = u emailu samo nove akcije |

## GitHub Actions (automatski 2× sedmično)

1. Napravi **privatan** repo i pushaj ovaj folder (`.env` se ne pushuje, nalazi se u `.gitignore`).
2. Repo → Settings → Secrets and variables → Actions: upiši varijable iz tabele iznad.
3. Repo → Settings → Actions → General → Workflow permissions: **Read and write permissions** (bot pusha `site/akcije.json` na main i istoriju na branch `data`).
4. Actions → „Akcije izvjestaj“ → **Run workflow** za prvi ručni test.

Raspored: ponedjeljak i četvrtak ujutru.

Šta workflow radi:
- pokrene testove, pa preuzme istoriju cijena sa brancha `data`. Ako taj branch još ne postoji, počinje novu istoriju, ili preuzme `data/istorija.sqlite` sa maina ako je tamo ostao iz ranije verzije;
- pokrene `python -m akcije`, koji pošalje email i upiše istoriju;
- commituje `site/akcije.json` na main. Prije push-a radi `git pull --rebase` (do 3 pokušaja), pa ne smeta ako si u međuvremenu pushao kod;
- istoriju pusha na **orphan branch `data`** kao jedan commit koji se svaki put prepisuje (`--force`). Tako main ne raste sa svakom verzijom binarnog SQLite fajla (procjena: inače ~300 MB godišnje). Istorija se pusha samo ako je na početku uspješno preuzeta, pa se prava istorija nikad ne prepiše praznom.

Prelazak sa ranije verzije (kad je `data/istorija.sqlite` bio na mainu): prvo pokretanje sa novim workflowom samo prenese fajl na branch `data`. Poslije toga ga ukloni sa maina: `git rm --cached data/istorija.sqlite`, pa commit i push.

### Kad prodavnica padne

- **Jedna ili više (ne sve):** za tu prodavnicu se na stranici zadržavaju akcije iz prethodnog `akcije.json`, samo one kojima rok nije istekao, sa oznakom „stari podaci od …“. Email i stranica pokažu upozorenje. Stare akcije ne ulaze u email tabelu. U istoriji se ta prodavnica ne računa kao pokrenuta, pa joj sljedeći put sve akcije ne postanu 🆕.
- **Sve:** `akcije.json` i istorija se ne mijenjaju. Email sa upozorenjem se ipak pošalje, a run se završi kao neuspješan, pa ti GitHub pošalje obavještenje.
- **Idea bez `GEMINI_API_KEY`** se računa kao pala prodavnica (stari podaci + upozorenje).
- **Djelimičan gubitak:** ako Idea izgubi neki katalog ili neke strane (Gemini greška), ili ako prodavnica vrati manje od 30% proizvoda u odnosu na prošli uspješan put (`LOW_COUNT_SHARE_PCT` u `__main__.py`), podaci se koriste, a email i stranica pokažu upozorenje.
- **Email nije poslat:** run je neuspješan, a istorija se ne upisuje, pa iste akcije ostaju 🆕 u sljedećem emailu.
- `akcije.json` se upisuje atomično (privremeni fajl, pa rename), pa prekid nikad ne ostavi pokvaren fajl.

## Web stranica na Vercelu

1. vercel.com → Add New → Project → izaberi repo (radi i sa privatnim repoom, besplatno).
2. **Root Directory = `site`**, Framework = Other, bez build komande.
3. Dobijeni link upiši kao GitHub Variable `SITE_URL`.
4. Svaki commit bota (novi `akcije.json`) automatski ponovo objavljuje stranicu.

Stranica je javna za svakog ko ima link (ima `noindex`, pa je pretraživači ne indeksiraju). Prikazuje samo javne cijene, bez ličnih podataka.

## Struktura

| Fajl | Uloga |
|---|---|
| `akcije/models.py` | `Offer`, zajednički format za sve prodavnice. Popust se računa iz cijena. |
| `akcije/http.py` | GET klijent: pauza 1,5 s između zahtjeva, retry na 429/5xx |
| `akcije/adapters/voli.py` | Voli (HTML + JSON u karticama) |
| `akcije/adapters/cosmetics.py` | Cosmetics (interni JSON API, 100 artikala po zahtjevu) |
| `akcije/adapters/kucahemije.py` | Kuća hemije (Magento GraphQL, samo kategorije gdje su muški proizvodi) |
| `akcije/adapters/idea.py` | Idea katalozi (iPaper tekst → Gemini → provjera cijena) |
| `akcije/gemini.py` | Mali Gemini klijent (JSON odgovori, poštuje free-tier limit, retry na 429) |
| `akcije/categories.py` | Zajedničke kategorije za sve prodavnice (Meso i riba, Piće, Lična higijena…) |
| `akcije/gender.py` | Pravilo „isključivo muški proizvod“ za Cosmetics i Kuću hemije |
| `akcije/filters.py` | Pravila: min. popust, odbacuje istekle akcije |
| `akcije/history.py` | SQLite istorija cijena: 🆕 nove akcije (u odnosu na posljednji uspješan put te prodavnice), ⚠️ podignuta „stara“ cijena, broj proizvoda po prodavnici |
| `akcije/sitedata.py` | Piše `site/akcije.json` (atomično); za prodavnice koje su pale zadržava njihove prethodne akcije |
| `akcije/report.py` | Email: kratak (top 5 + link) ako postoji `SITE_URL`, inače puna tabela |
| `akcije/mailer.py` | Slanje preko Resend API-ja |
| `akcije/cache.py` | Lokalni keš ponuda i Gemini odluka (`--cache`, samo uz `--dry-run`) |
| `akcije/__main__.py` | Pokreće sve adaptere. Greška jedne prodavnice ne zaustavlja ostale i prikaže se u emailu. |
| `site/index.html` | Statička stranica (čist JavaScript): filteri, pretraga, sortiranje, šoping lista (čuva se u pregledaču, localStorage), prilagođena telefonu |
| `data/istorija.sqlite` | Istorija cijena. Lokalno nastaje pri prvom pravom pokretanju (ne pri `--dry-run`) i nije u gitu; na GitHubu je na branchu `data` |

Novi adapter = funkcija `fetch_offers(client) -> list[Offer]`, upisana u `ADAPTERS` u `__main__.py`.

## Kategorije

Svaka prodavnica ima svoje kategorije (Voli ih ima stotine: „Tuna“, „Biber“, „DEO“…), pa `akcije/categories.py` svaki proizvod svrstava u jednu od ~16 zajedničkih grupa: Bebe i djeca, Kućni ljubimci, Smrznuto, Parfemi, Odjeća i ostalo, Lična higijena, Kozmetika i njega, Domaćinstvo i čišćenje, Piće, Mliječni proizvodi i jaja, Meso i riba, Voće i povrće, Hljeb/tjestenina/žitarice, Slatkiši i grickalice, Namirnice i začini, Ostalo. Prvo se gleda kategorija prodavnice, pa naziv. Za Idea Gemini bira grupu sa iste liste, ali jasna riječ u nazivu ima prednost. Email (puna tabela) i stranica prikazuju proizvode po tim grupama; originalna kategorija prodavnice vidi se na kartici.

## Pravila (dogovoreno)

| Prodavnica | Uslov | Kartica |
|---|---|---|
| Idea | popust ≥ 30%, svi proizvodi | Super kartica se računa (`card_required=True`, 💳) |
| Voli | popust ≥ 30%, svi proizvodi | – |
| Cosmetics | popust ≥ 30% **i** isključivo muški proizvod | Beauty Friend cijene se **ignorišu** |
| Kuća hemije | popust ≥ 30% **i** isključivo muški proizvod | – |

„Muški“ (`akcije/gender.py`): jake riječi u nazivu (men, man, homme, uomo, muški, for him, Old Spice, Axe…) imaju prednost nad kategorijom prodavnice, jer su kategorije ponekad pogrešne (npr. „D&G K EDP man“ u kategoriji „za žene“). Slabe riječi (brijač, brijanje, after shave, Gillette, brada) važe samo ako kategorija nije ženska. Ženske riječi (ženski, women, Venus, lady…), unisex i dječji proizvodi (dječji, kids, junior, baby, kao i likovi poput Spider-Man, Iron Man, Batman) uvijek isključuju proizvod. „man“ unutar imena sa crticom („X-Man“) ne računa se kao muški signal. Parfemi i dezodoransi bez ikakvog signala su „nejasni“: odlučuje Gemini, a bez Gemini-ja ili u sumnji proizvod ne ulazi.

Cosmetics varijante (npr. 30ML i 100ML istog parfema) dobijaju veličinu u nazivu kad je naziv već ne sadrži, da se razlikuju na stranici i u šoping listi.

Poznato ograničenje: ako prodavnica stavi ženski proizvod u mušku kategoriju i u nazivu nema ženske riječi (npr. Burberry Goddess u „Parfimisani - set muški“ na Cosmetics), on će ući.

## Izvori podataka (provjereno 8.11.2026)

**Voli:** `voli.me/tag/akcije?page=N`, oko 19 stranica, server-rendered. Svaka `.product-card` ima JSON u atributu `:product` na `<add-to-cart-button>`. Pregled prikazuje najviše 10 proizvoda po kategoriji, pa se kategorije sa 10 proizvoda dopunjavaju sa `/tag/akcije/kategorije/<id>`. `robots.txt` dozvoljava sve.

**Cosmetics:** `https://cosmetics.2bi.me/wapi/articles?action_filter=Akcije&per_page=100&page=N` (oko 3.700 artikala = 37 zahtjeva). Polja: `brand`, `name`, `priceWithTax` (stara), `priceWithDiscount`, `discountStartDate/EndDate`, `mainCategoryDescription` → `categoryDescription` → `subCategoryDescription`, `firstClassifier`, `share_url`. Muške podkategorije postoje („Deo sprej muški“, „Parfem muški“, „Brijač muški“, „Bokserice muške“…). Interni API: ako se format promijeni, adapter baca `AdapterError`.

**Kuća hemije:** `drogerija.me` je Magento 2 sa javnim GraphQL-om (`/graphql?query=…`, GET). HTML stranica `/akcije` ima 6 MB po 32 proizvoda, pa se koristi GraphQL (100 proizvoda u par KB). Čitaju se kategorije 1039 Muška njega, 958 Muški parfemi, 960 Setovi parfema, 1023 Dezodoransi, 1013 Kupanje, 1038 Poklon setovi (~18 zahtjeva). Cijene iz `price_range.minimum_price` (regular/final), datum iz `special_to_date`. `robots.txt` ne zabranjuje `/graphql`.

**Idea:** `idea.co.me/Katalozi` → linkovi `/Katalozi/<naziv>-<dan>-DD.<mjesec>-YYYY.-…` (hrvatski nazivi mjeseci). Stranica kataloga ima link na `catalog.idea.co.me/<godina>/<mjesec>/<slug>/?page=1`, čiji HTML sadrži `"pageTexts":[...]`. Tekst je izmiješan (`-39% cijena uz 1 99 super karticu 3,25 EUR MASLAC…`), pa Gemini spaja nazive i cijene (12 strana po zahtjevu). Svaka stavka se provjerava: obje cijene moraju postojati u tekstu te strane, štampani procenat mora odgovarati cijenama (±2), a bez procenta popust ne smije biti veći od 80%. Sve što ne prođe se odbacuje. Link vodi na tačnu stranu kataloga. Neki katalozi (npr. „Specijal Italije“, „Novogodišnji specijal“, novembar 2026) imaju samo slike bez teksta; oni se preskaču (piše u logu). `robots.txt` dozvoljava pristup (Content-Signal ne zabranjuje ai-input).

## Moguća poboljšanja

- Kuća hemije: dodati još kategorija ako se pojave muški proizvodi van praćenih (npr. njega kose).
- Idea: OCR za kataloge koji su samo slike (npr. „Specijal Italije“); sada se preskaču.
- Idea: 💳 se oslanja samo na Gemini; moglo bi se provjeriti da tekst strane sadrži „kartic“.
- Istorija: grafik cijene po proizvodu na stranici.
