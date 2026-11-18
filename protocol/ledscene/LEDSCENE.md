# LEDSCENE: binair scèneformaat voor ledstrips (`ledscene/1`)

```yaml
ledscene-format: "ledscene/1"
status: voorstel   # genormeerd voor HUISDEV 1.1 (scènes, zie HUISDEV.md §16)
```

Een **scène** is een kleine binaire beschrijving van een ledstrip-animatie: een vaste kleur, een kleurovergang in een bepaald tempo
(zonsopkomst), of een lus (regenboog). De huisconfig bouwt hem, stuurt hem via HUISDEV naar een ESP (en bewaart hem daar in een slot),
en de ESP **speelt hem zelf en volledig af**. Daarom bevat de scène alles wat het afspelen bepaalt: duur, einde, fade-out, en hoe lang
na een herstart hervatten nog zin heeft.

Dit document is **normatief en bit-exact**: alles is geheeltallig, zodat de Python-implementatie (add-on) en de C++-implementatie (ESP)
voor dezelfde bytes en tijd dezelfde uitvoer geven. Referenties: `addon/kkaddon/scenes.py` en `protocol/ledscene/ledscene.h`;
gedeelde testvectoren: `protocol/ledscene/vectors/`.

Alle meerbyte-getallen zijn **little-endian**. `//` en `>>` zijn *floor* (ook voor negatieve getallen); `mod` is floor-modulo (resultaat ≥ 0).
Rekenen gebeurt in 64 bit met teken.

---

## 1. Bytelayout

### Kop (16 bytes)

| offset | veld | betekenis |
|---|---|---|
| 0 | `L` `S` | magic `0x4C 0x53` |
| 2 | `ver` u8 | 1 |
| 3 | `nkf` u8 | aantal frames (keyframes), 1..32 |
| 4 | `loops` u8 | 1 = eenmaal, n = n keer, **0 = herhalen tot stop** (het device begrenst, zie §5) |
| 5 | `end` u8 | 0 = na het spelen fade-out en uit; 1 = laatste beeld vasthouden |
| 6 | `tick` u16 | milliseconden per tijdseenheid, 1..60000 (meestal 100) |
| 8 | `hold` u16 | seconden vasthouden bij `end=1`; 0 = tot stop (het device begrenst). Bij `end=0` moet het 0 zijn |
| 10 | `fade` u8 | duur van de fade-out in eenheden van 100 ms (bij einde en bij stop). 0 = meteen |
| 11 | gereserveerd u8 | 0 |
| 12 | `resume` u16 | na een herstart van het device hervat de scène alleen als er sinds de start hoogstens zoveel seconden verstreken zijn. 0 = nooit hervatten, 65535 = altijd (zolang hij nog loopt) |
| 14 | gereserveerd u16 | 0 |

### Frame (7 bytes + 9 bytes per span), `nkf` keer

| veld | type | betekenis |
|---|---|---|
| `dt` | u16 | ticks van dit frame tot het volgende. Bij het **laatste** frame: ticks van het laatste frame terug naar frame 0 (alleen bij `loops≠1`; bij `loops=1` moet het 0 zijn). Elk niet-laatste frame heeft `dt ≥ 1` |
| `ease` | u8 | overgang van dit frame naar het volgende: 0 = vasthouden (springt op het volgende), 1 = lineair, 2 = smoothstep |
| `lum` | u8 | globale helderheid van het frame, gamma-gecodeerd (255 = vol) |
| `scroll` | i16 | verschuiving van het patroon over dit segment, in 1/256 van de strip (256 = één hele strip). Positief = naar hogere ledindex |
| `n` | u8 | aantal spans, 0..16 (0 = zwart frame) |

### Span (9 bytes)

| veld | type | betekenis |
|---|---|---|
| `a` | u8 | begin. Zonder vlag: 0..255 in 1/256 van de strip (apparaat-onafhankelijk). Met vlag: ledindex |
| `len` | u8 | lengte in dezelfde eenheid; **0 betekent 256** |
| `flags` | u8 | bit 0: `a` en `len` zijn **ledeenheden** (exact adresseren van losse leds). Overige bits: 0 |
| `c1` | r,g,b | kleur aan het begin van de span (gamma-gecodeerd, 8 bit) |
| `c2` | r,g,b | kleur aan het einde. Gelijk aan `c1` = effen |

Totale lengte = `16 + Σ (7 + 9·n)`; er mogen geen bytes overblijven. Grootte van een zonsopkomst van 12 frames met 3 spans: 424 bytes.

---

## 2. Tijd

Met `N` leds en `tick` ms:

```
start[k] = tick * Σ_{j<k} dt[j]                      (ms, k = 0..nkf-1)
span_ms  = tick * Σ_{j<nkf} dt[j]                    (bij loops=1 is dt[laatste]=0, dus = start[laatste])
play_ms  = loops==0 ? ∞ : loops * span_ms
```

Voor `t` ms na de start (`t ≥ 0`):

* **Spelen** (`loops==0` of `t < play_ms`): `tc = t mod span_ms`; `k` = grootste index met `start[k] <= tc`; `nxt = k+1 < nkf ? k+1 : 0`.
  ```
  dur = dt[k] * tick
  w   = (tc - start[k]) * 65536 // dur                  Q16 lineaire voortgang van het segment, 0..65535
  we  = ease[k]==0 ? 0 : ease[k]==1 ? w : (((w*w) >> 16) * (196608 - 2*w)) >> 16     eased gewicht (smoothstep)
  sh  = (scroll[k] * N * w) >> 16                        verschuiving in positie-eenheden (zie §3), met het gewone w
  ```
  Beeld = overgang van frame `k` (gewicht `1-we`) naar frame `nxt` (gewicht `we`), beide met dezelfde verschuiving `sh` (§4).
* **Einde** (`loops≠0` en `t ≥ play_ms`): het *eindbeeld* is frame `nkf-1` als `loops==1`, anders frame 0, zonder verschuiving (`sh=0`, `we=0`).
  * `end==1`: eindbeeld op volle sterkte tot `play_ms + hold*1000` (bij `hold==0`: tot stop of het device-plafond).
  * daarna (of meteen bij `end==0`) fade-out vanaf `T0`: `g = 65536 - (t - T0) * 65536 // (fade*100)`, uitvoer = `uitvoer * g >> 16`, bij `fade==0` meteen uit. Daarna is de scène klaar.
* **Stop** (op verzoek van het device/de huis): het device fadet vanaf het huidige beeld naar zwart in `fade*100` ms (`fade==0`: meteen).

`totaal_ms` (begrensde scène) = `play_ms + (end==1 ? hold*1000 : 0) + fade*100`. Een scène is **onbegrensd** als `loops==0` of (`end==1` en `hold==0`).
Voor een onbegrensde scène is `totaal_ms` niet gedefinieerd (de implementaties geven `None` resp. `UINT64_MAX`). Bij `loops≠0`, `end==1`, `hold==0` wordt `play_ms` zelf niet aan de 24-uursgrens getoetst (alleen `span_ms`); het plafond van het device (§6) begrenst hem.

---

## 3. Positieruimte

De strip heeft `L = 256 * N` posities; led `i` beslaat `[i*256, (i+1)*256)`. Een span begint op `S0 = a * N` (zonder vlag) of `a * 256` (met vlag) en heeft lengte
`len_u = (len==0 ? 256 : len) * (vlag ? 256 : N)`, begrensd tot `L`.
Bij ledeenheden mag `a ≥ N` (en `a + len > N`): de span wikkelt dan om de strip (`S = (S0 + sh) mod L`, §4), dus `a` hoeft niet kleiner dan `N` te zijn en de validatie (§7) toetst dat niet. `len=0` met ledeenheden is 256 leds, dus begrensd tot de hele strip (`L`).

---

## 4. Frame renderen: `F(frame, sh) → P[i][c]` in Q8.8 (0..65280)

`P` begint op 0 (zwart), met per led een gedekt deel `a = 0`. Voor elke span in volgorde:

```
S = (S0 + sh) mod L ;  E = S + len_u
ovl(s,e,i) = max(0, min(e, (i+1)*256) - max(s, i*256))
cov(i)     = min(256, ovl(S, min(E,L), i) + (E > L ? ovl(0, E-L, i) : 0))      dekking 0..256
voor elke led i met cov > 0:
   d = (i*256 + 128 - S) mod L
   u = d < len_u ? d*256 // len_u : ((L - d) < (d - len_u) ? 0 : 256)          positie in het verloop, 0..256
   C[c]  = c1[c]*(256-u) + c2[c]*u                                              Q8.8
   o     = max(0, a[i] + cov - 256)                                             kleinst mogelijke overlap met wat er al ligt
   keep  = a[i] == 0 ? 256 : (a[i] - o) * 256 // a[i]                           deel van het bestaande beeld dat blijft
   P[i][c] = (P[i][c]*keep + C[c]*cov + 128) >> 8
   a[i]  = min(256, a[i] + cov)
```

Een span die een led maar deels dekt, valt over wat er al ligt (alpha-over), maar spans die **naast** elkaar liggen en samen één led delen (de naad tussen twee verlopen) tellen bij elkaar op
in plaats van elkaar te dimmen. Ligt er nog niets (`a = 0`) of is de led al helemaal gedekt (`a = 256`, bijvoorbeeld door een ondergrond over de hele strip), dan is `keep = 256 - cov` en is dit
precies de gewone alpha-over `P = (P*(256-cov) + C*cov + 128) >> 8`. (Een eerdere opzet gebruikte altijd die gewone alpha-over; dat dimde elke naad tussen twee aansluitende spans tot 25 % in code,
een zichtbare donkere stip in de regenboog.)

---

## 5. Uitvoer van de renderer: lineair Q16

Voor led `i`, kanaal `c`, met `A = F(k, sh)[i][c]`, `B = F(nxt, sh)[i][c]` en gewicht `we`:

```
X   = (A*(65536-we) + B*we + 32768) >> 16                      Q8.8
G   = ((lum[k]<<8)*(65536-we) + (lum[nxt]<<8)*we + 32768) >> 16
lin = (LUT(X) * LUT(G) + 32768) >> 16                           Q16, 0..65535
LUT(x) = (T[x>>8]*(256-(x&255)) + T[(x>>8)+1]*(x&255) + 128) >> 8        T[256] = T[255]
T[i]   = round(65535 * (i/255)^2.2)     voor i = 0..255   (de tabel staat vast in `ledscene_lut.h` en `scenes.py`)
```

**Dit `lin` (u16 per led en kanaal) is de genormeerde uitvoer** (volle schaal, 255 en `lum` 255, geeft 65534: `(65535*65535 + 32768) >> 16`) en is wat de vectoren vergelijken. Wat het device daarna doet is niet genormeerd maar
aanbevolen: vermenigvuldigen met de masterhelderheid en een stroomlimiet (Q16), en dan **sigma-delta-dither** naar 8 bit per led en kanaal:
`v = lin + res; uit = min(255, v >> 8); res = v & 255` (met `res` per led en kanaal, start 0). Zo blijft een dim bij 1-5 % vloeiend. Een scène wordt daarom
het best minstens 100 keer per seconde ververst.

---

## 6. Begrenzing en hervatten (device)

* Een **onbegrensde** scène krijgt een plafond van het device (kidswekker: instelbaar `hold_min`, standaard 60 min, gerekend vanaf de start).
* Absolute grenzen die elk device afdwingt: `span_ms ≤ 86 400 000`, `totaal_ms ≤ 86 400 000` voor een begrensde scène, `hold ≤ 43 200` s.
* **Hervatten na een herstart**: alleen als het device een geldige klok heeft (epoch > 1 700 000 000), de start (epoch) bewaard is, `verstreken = nu - start`
  niet negatief is, de scène nog loopt (`verstreken*1000 < totaal_ms`, of onder het plafond bij een onbegrensde) en `verstreken ≤ resume`. Het beeld volgt dan uit `t = verstreken*1000`.
  Anders blijft de strip uit.

## 7. Validatie (elk device, vóór opslaan of afspelen)

Afwijzen (`rejected`, met reden) als: magic of `ver` klopt niet; `nkf` buiten 1..32; `end > 1`; `tick` buiten 1..60000; gereserveerde velden ≠ 0; `end==0` met `hold≠0`;
een niet-laatste `dt == 0`; het laatste `dt` is 0 bij `loops≠1` of ≠ 0 bij `loops==1`; `ease > 2`; `n > 16`; `flags` met andere bits dan bit 0; de lengte klopt niet exact;
meer bytes dan het device toestaat; de absolute grenzen van §6; het totaal aantal spans in de scène > 128.

De volgorde waarin de controles hierboven gebeuren is in `scenes.py` en `ledscene.h` gelijk (kop eerst, dan frame voor frame, dan de lengte, dan de grenzen van §6), en ook de
redetekst is gelijk (statische Nederlandse teksten); de vectoren (`bad`-regels) toetsen beide. Alleen *dat* een scène wordt afgewezen is normatief, de tekst niet.

## 8. Voorbeelden (informatief, `N=20`, `tick=100`)

* **Nachtlampje** (warm wit 5 %): `nkf=1, loops=1, end=1, hold=0 (device-plafond), fade=20, resume=0xFFFF`; één frame `dt=0, ease=0, lum=255, scroll=0, n=1`, span `a=0,len=0 (256),flags=0, c1=c2=(65,38,10)`.
* **Wekker** (zonsopkomst 20 min, dan 20 min groen, dan uit): 12 frames op `dt ≈ 12000/11` ticks (20 min) met toenemende rode ondergrond en een groeiende zon uit het midden
  (`a=128-w, len=2w`), `end=1, hold=1200, fade=50, resume=600`; het laatste frame is effen groen (0,100,0) en heeft `dt=0` (`loops=1`).
* **Regenboog** (5 s per ronde, tot stop): `tick=20, loops=0, end=1, hold=0`. Twee frames met dezelfde zes verloop-spans
  (`a,len` = 0,43 · 43,42 · 85,43 · 128,43 · 171,42 · 213,43; R→Y, Y→G, G→C, C→B, B→M, M→R). Frame 0: `dt=250, ease=1, scroll=256`. Frame 1: `dt=1, ease=0, scroll=0`.
  Het patroon is na 256 eenheden identiek, dus de lus is naadloos en onafhankelijk van `N`.

## 9. Versies

`ledscene/2` (bijvoorbeeld RGBW of meer dan 8 bit) is een nieuw formaat met een eigen magic-versie. Een device wijst een onbekende `ver` strikt af; de descriptor meldt de ondersteunde formaten
(`"formats":["ledscene/1"]`) en de add-on compileert per device naar het formaat dat het kent.
