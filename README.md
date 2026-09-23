# Mining Pearl (PRL) pe SaladCloud — setup de test

**Versiunea 1 · 23 septembrie 2026 · pentru un test plătit cu 1 GPU, nu pentru scalare.**

Acest folder conține tot ce trebuie ca să rulezi un miner Pearl pe GPU-uri închiriate prin **SaladCloud Container Engine**: imaginea Docker, scriptul de pornire cu watchdog, workflow-ul de build, un helper pentru API-ul Salad și un șablon de jurnal de test. Metoda urmează [ghidul de cercetare](../RESEARCH_GUIDE.md): semnal → verificare → execuție → cost real → dovada rezultatului.

## 0. Ce am verificat și ce nu

| Verificat | Rezultat |
| --- | --- |
| Salad permite mining pe Container Engine | Da. Salad publică o [rețetă oficială lolMiner](https://docs.salad.com/container-engine/reference/recipes/lolminer); [termenii SaladCloud](https://salad.com/terms/saladcloud) nu menționează miningul. |
| Miner CUDA pentru Linux, fără OpenCL | [krig-miner 1.5.2](https://github.com/kryptex/krig-miner) (Kryptex, 0% devfee). Binar dinamic, cere glibc ≥ 2.35, încarcă doar `libcuda.so.1` din driverul gazdei. Merge pe imagini `nvidia/cuda:*-base`. |
| Pool | [Kryptex PRL](https://pool.kryptex.com/prl): PPS+ 2%, SOLO 1%, payout orar de la 1 PRL, fără taxă de transfer. Endpointuri regionale, SSL pe 8048, TCP pe 7048. Latență de aici: EU 105 ms, global 116 ms, US 180 ms. |
| Preț și adâncime PRL | CoinEx PEARL/USDT la 23 sept 2026: ultimul preț 1,517; best bid 1,517 pentru ~47 PRL, apoi ~29 PRL la fiecare treaptă de ~1% în jos. |
| Rețea Pearl ([PearlTrack](https://pearltrack.io/)) | 41,72 EH/s, recompensă 2315,7 PRL/bloc, bloc ~166 s observat (194 s țintă). |

| Neverificat | De ce contează |
| --- | --- |
| Imaginea nu a fost construită și rulată pe Salad | Pe mașina asta nu există Docker. Primul build îl face GitHub Actions; prima rulare o faci tu, cu 1 replică. |
| Formatul liniei de share acceptat la krig | Rulare de 20 s pe RTX 5080 local (23 sept 2026): krig scrie la 5 s o linie `[stats] ... hashes/s=213 TH/s ... shares=N`. Watchdog-ul folosește contorul `shares=`, deci nu depinde de textul liniei de share. Hashrate măsurat: ~208–213 TH/s. Al doilea watchdog, pe utilizarea GPU, e independent de log. |
| SRBMiner pe Salad | Inclus ca fallback (`MINER=srb`, 2% devfee). Kryptex îl listează ca suportat, dar nu l-am testat în container. |
| Prețurile Salad | Cifrele din secțiunea 5 vin dintr-o [recenzie terță](https://gpupicks.com/salad-review/). Citește prețul real în portal sau cu `./salad-api.sh gpu-classes` înainte de deploy. |
| WildRig / PearlHash pe Salad | Nodurile Salad sunt PC-uri Windows cu containere sub WSL2/Hyper-V. NVIDIA nu oferă OpenCL în WSL2, iar [alți utilizatori raportează](https://github.com/pvandenburg1234-eng/pearl-salad-nvidia) că WildRig dă erori OpenCL acolo. De aceea setupul folosește un miner CUDA și poolul Kryptex, nu PearlHash + WildRig din ghid. |

## 1. Fișierele din folder

| Fișier | Rol |
| --- | --- |
| `Dockerfile` | Imagine pe `nvidia/cuda:12.8.1-base-ubuntu24.04`. Descarcă krig-miner 1.5.2 și SRBMiner 3.6.9 cu SHA256 fixat. Rulează ca utilizator neprivilegiat. |
| `entrypoint.sh` | Validează walletul, alege regiunea Kryptex cu latența cea mai mică, pornește minerul, pune timestamp pe fiecare linie, numără shares, scrie o linie `STATUS` la 10 minute și iese cu cod 3 când nodul nu mai produce, ca Salad să realoce instanța. |
| `.github/workflows/build-image.yml` | Construiește imaginea pe GitHub și o publică în GHCR. Nu ai nevoie de Docker local. |
| `container-group.example.json` | Corpul cererii pentru crearea container group-ului prin API. |
| `salad-api.sh` | Helper: listă clase GPU cu prețuri, disponibilitate, creare, start, stop, status, instanțe. |
| `docker-compose.yml` + `.env.example` | Test local pe GPU-ul tău, dacă instalezi Docker Desktop. |
| `test-log.csv` | Jurnalul testului plătit, cu coloanele cerute de ghid. |

## 2. Pașii

### Pasul 1 — Wallet PRL

1. Descarcă Pearl Wallet din [release-urile oficiale](https://github.com/pearl-research-labs/pearl/releases) (Windows, macOS, Linux). Ghidul Kryptex cu capturi: [How to create a Pearl wallet](https://pool.kryptex.com/articles/pearl-wallet-en).
2. Creează walletul, scrie fraza de recuperare pe hârtie, verifică fraza.
3. Apasă **Receive** și copiază adresa. Adresa mainnet începe cu `prl1p`.
4. Nu folosi adresa de depozit a unui exchange ca adresă de mining. Exchange-urile schimbă uneori adresele, iar poolul nu poate redirecționa plățile.

### Pasul 2 — Test gratuit pe RTX 5080 local (recomandat înainte de a plăti)

Ai un RTX 5080 cu driver 610.88 pe mașina asta. Același miner rulează nativ pe Windows, deci poți valida walletul, poolul și hashrate-ul cu cost zero.

1. Descarcă [krig-miner-1.5.2-win-x64.zip](https://github.com/kryptex/krig-miner/releases/download/v1.5.2/krig-miner-1.5.2-win-x64.zip) și dezarhivează. Antivirusul poate marca minerii; verifică doar că fișierul vine din release-ul oficial GitHub.
2. Rulează în PowerShell, din folderul dezarhivat:

```powershell
.\krig-miner.exe --coin pearl --url stratum+ssl://prl-eu.kryptex.network:8048 --user prl1p_ADRESA_TA/test5080 --no-tui --log-level debug
```

3. Notează: ora pornirii, timpul până la primul share acceptat, hashrate-ul afișat (TH/s), textul exact al liniei de share acceptat.
4. După 10–15 minute, deschide [pool.kryptex.com/prl](https://pool.kryptex.com/prl) și caută adresa walletului. Workerul `test5080` trebuie să apară cu hashrate raportat.
5. Textul liniei de share acceptat îl folosești ca `SHARE_REGEX` în container dacă nu conține cuvântul `accept`.

Orientativ, krig raportează ~292 TH/s pe RTX 4090 și ~385 TH/s pe RTX 5090; alți utilizatori au măsurat ~183 TH/s pe RTX 5080.

### Pasul 3 — Construiește imaginea

**Varianta A, fără Docker local (GitHub Actions → GHCR):**

1. Creează un repo nou pe GitHub, de exemplu `pearl-salad-miner`.
2. Pune conținutul acestui folder `salad/` ca **rădăcină** a repo-ului. Workflow-ul trebuie să fie la `.github/workflows/build-image.yml` în rădăcină.
3. Push pe `main`. Workflow-ul construiește imaginea și o publică la `ghcr.io/<user>/pearl-salad-miner:latest` și `:sha-xxxxxxx`.
4. Fă pachetul public: GitHub → profil → Packages → pachetul → Package settings → Change visibility → Public. Salad poate trage imaginea fără credențiale. Alternativ, lasă pachetul privat și dă Salad-ului un [PAT cu `read:packages`](https://docs.salad.com/container-engine/how-to-guides/registries/github-ghcr).

**Varianta B, cu Docker local:** instalează Docker Desktop cu backend WSL2, apoi:

```powershell
copy .env.example .env      # completează WALLET
docker compose up --build   # rulează pe RTX 5080, în același tip de mediu WSL2 ca nodurile Salad
```

Dacă imaginea merge local, o publici cu `docker tag` și `docker push` către GHCR sau Docker Hub.

### Pasul 4 — Deploy pe Salad

Înainte: cont SaladCloud cu credit preplătit ([billing](https://docs.salad.com/general/explanation/billing)), un proiect creat, o cheie API (Portal → API Keys) dacă folosești helperul.

**Prin portal:**

| Câmp | Valoare pentru test |
| --- | --- |
| Container group name | `pearl-test-1` |
| Image source | `ghcr.io/<user>/pearl-salad-miner:latest` |
| Replicas | `1` |
| vCPU / RAM | `2` / `4 GB` (minimul acceptat de clasa GPU, dacă e mai mare) |
| GPU | o singură clasă: **RTX 4090 (24 GB)** sau **RTX 5090 (32 GB)**. Poți bifa și ambele; Salad alocă prima disponibilă. |
| Priority | **Batch / Lowest** pentru test. Se poate întrerupe oricând, dar e cel mai ieftin și nu plătești pornirea. |
| Environment variables | `WALLET=prl1p...`, `MINER=krig`, `LOG_LEVEL=debug`, `STATUS_EVERY=600` |
| Health probes, Container Gateway | niciuna |
| Autostart | oprit; pornești manual după ce verifici configurația |

Apasă **Deploy**, apoi **Start**. Facturarea începe abia când instanța ajunge în starea *running*.

**Prin API:**

```bash
export SALAD_API_KEY=...  SALAD_ORG=org-ul-tau  SALAD_PROJECT=proiectul-tau
./salad-api.sh gpu-classes                      # copiază UUID-ul clasei și prețul batch
./salad-api.sh availability <uuid>              # câte noduri sunt libere pe fiecare prioritate
# editează container-group.example.json: image, gpu_classes, WALLET
./salad-api.sh create container-group.example.json
./salad-api.sh start pearl-test-1
./salad-api.sh instances pearl-test-1
```

### Pasul 5 — Primele 15 minute

Deschide **Container Logs** în portal. Ordinea așteptată:

1. `gpu: NVIDIA GeForce RTX 4090, 5xx.xx, 24564 MiB, ...` — placa și driverul nodului.
2. `probe prl-eu.kryptex.network 23 ms` … `selected pool host ...` — regiunea aleasă.
3. `cmd: /opt/miners/krig/krig-miner --coin pearl --url stratum+ssl://...` — comanda exactă.
4. Liniile minerului cu timestamp, apoi **`FIRST ACCEPTED SHARE after N s`**. Notează N în jurnal.
5. La fiecare 10 minute: `STATUS uptime=... accepted=... rejected=... last_accept_age=...` și `STATUS gpu util,power,temp`.

Dacă în 10 minute nu apare `FIRST ACCEPTED SHARE` dar vezi în log linii de share acceptat cu alt text, regexul e greșit: oprește grupul, setează `SHARE_REGEX` la un fragment din acea linie, repornește. Watchdog-ul pe GPU (`MIN_GPU_UTIL`) te protejează între timp: dacă GPU-ul stă sub 20% timp de 15 minute, containerul iese și Salad realocă.

Pe [pool.kryptex.com/prl](https://pool.kryptex.com/prl), caută walletul. Statisticile apar după 10–15 minute. Workerul are numele `s` + primele 10 caractere din `SALAD_MACHINE_ID`, ca să poți lega fiecare nod de linia lui din log.

### Pasul 6 — După 24 de ore: reconciliere

Completează `test-log.csv` cu:

- din log: `start_utc`, `first_share_utc`, `accepted_shares`, `rejected_shares`, numărul de realocări (câte porniri ale scriptului vezi);
- din Salad, Billing / Usage: orele facturate și suma;
- din Kryptex: hashrate mediu raportat, PRL pending și PRL plătit pentru workerul respectiv;
- din CoinEx: prețul la care ai putea vinde efectiv cantitatea rezultată, minus comision.

Regulile ghidului: nu aduna pending cu paid, nu trata o cotație ca pe o vânzare, iar o fereastră fără date din pool sau din Salad e „necunoscut”, nu zero.

## 2b. Monitorizare: `monitor.py`

Adună la fiecare 10 minute (sau cât setezi) datele din trei surse și le scrie în `data/`:

| Sursă | Ce citește | Fișier |
| --- | --- | --- |
| SaladCloud API | starea grupului, instanțele (mașină, stare, clasă GPU, preț), disponibilitatea live | `data/instances.csv`, `data/snapshots.csv` |
| Kryptex API (fără cheie) | sold pending/confirmat/plătit, fiecare worker cu hashrate 30m/3h/24h și shares valid/stale/invalid | `data/workers.csv`, `data/snapshots.csv` |
| CoinGecko | prețul PRL/USDT pe SafeTrade, volumul pe 24 h, bidurile din 2% sub preț | `data/snapshots.csv` |

Din ele calculează costul estimat (instanțe *running* × prețul clasei × timp), PRL pe zi din hashrate-ul măsurat, venitul și marja pe zi. Recompensele Kryptex se confirmă după `maturation_time` (~5,4 ore), deci `prl_confirmed` rămâne 0 în primele ore chiar dacă `prl_unconfirmed` crește.

```powershell
cd "C:\Users\suntu\Desktop\VisualStudio projects\Pearl Mining\salad"
python monitor.py --once            # o citire
python monitor.py --interval 300    # buclă la 5 minute, Ctrl+C oprește
python monitor.py --report          # rezumat pe tot ce s-a strâns: TH/s pe worker, cost, PRL, marjă realizată
```

Are nevoie de `.env` cu `SALAD_API_KEY`, `SALAD_ORG`, `SALAD_PROJECT`, `WALLET`, `GROUP`. Fișierul e ignorat de git. Workerii Kryptex se leagă de instanțele Salad prin nume: `s` + primele 10 caractere din `SALAD_MACHINE_ID`.

Endpointuri Kryptex folosite (descoperite din aplicația lor web, nedocumentate oficial, pot fi schimbate fără preaviz):
`/prl/api/v1/miner/balance/<adresă>`, `/prl/api/v3/miner/workers/<adresă>`, `/prl/api/v1/miner/payouts/<adresă>/stats`, `/api/v1/rates`.

## 3. Variabile de mediu

| Variabilă | Implicit | Ce face |
| --- | --- | --- |
| `WALLET` | obligatoriu | Adresa `prl1p...`. Prefix `solo:` pentru SOLO. |
| `MINER` | `krig` | `krig` (CUDA, 0% devfee, doar Kryptex) sau `srb` (SRBMiner, 2% devfee). |
| `POOL` | gol | URL explicit. Gol = alegere automată din regiunile Kryptex. |
| `POOL_AUTO` | `1` | Sondează regiunile pe TCP 7048 și alege latența minimă. |
| `POOL_REGIONS` | `prl prl-eu prl-us prl-br prl-sg prl-hk prl-ru prl-ae` | Prefixele regiunilor sondate. |
| `WORKER` | derivat din `SALAD_MACHINE_ID` | Doar litere și cifre; Kryptex nu acceptă altceva. |
| `LOG_LEVEL` | `debug` | Pentru krig, `debug` afișează fiecare share. `info` e mai liniștit, dar watchdog-ul pe shares poate rămâne fără semnal. |
| `SHARE_COUNTER_REGEX` | `shares=([0-9]+)` | Contorul cumulativ de shares din linia `[stats]` a krig (scrisă la 5 s). Când crește, e semnal de producție. Verificat pe RTX 5080 local. |
| `HASHRATE_REGEX` | `hashes/s=([0-9.]+) TH/s` | Hashrate-ul din aceeași linie; apare în `STATUS` ca `hashrate_ths=`. |
| `SHARE_REGEX` | `accept` | Regex de rezervă (case-insensitive) pentru o linie de share acceptat, folosit dacă contorul lipsește (de exemplu cu SRBMiner). |
| `REJECT_REGEX` | `reject\|stale\|invalid` | Regex pentru shares respinse. |
| `STARTUP_GRACE` | `600` | Secunde permise până la primul share acceptat. |
| `NO_SHARE_TIMEOUT` | `900` | Secunde fără share acceptat (sau cu GPU sub prag) după care containerul iese cu cod 3. |
| `MIN_GPU_UTIL` | `20` | Prag de utilizare GPU (%) pentru al doilea watchdog. `0` îl dezactivează. |
| `WATCHDOG` | `1` | `0` dezactivează ambele watchdog-uri. |
| `STATUS_EVERY` | `600` | Secunde între liniile `STATUS`. |
| `MAX_RUNTIME` | `0` | Secunde după care scriptul iese cu cod 0. Cu `restart_policy=always` Salad îl repornește, deci pentru un test limitat oprești grupul manual. |
| `EXTRA_ARGS` | gol | Argumente suplimentare pentru miner, adăugate verbatim. |
| `API_PORT` / `API_DUMP` | `12000` / `1` | API-ul HTTP al krig pe localhost; `STATUS api:` include un fragment brut, util ca să aflăm formatul. |

## 4. Cum ies banii: Salad → Kryptex → wallet → CoinEx

1. Salad facturează **pe secundă, doar cât instanța rulează**, din creditul preplătit. Descărcarea imaginii și pornirea nu se plătesc.
2. Kryptex PPS+ plătește pe share, indiferent de norocul poolului. Payout automat **orar** când soldul depășește **1 PRL**, fără taxă de tranzacție. Un RTX 4090 face ~0,25 PRL/oră, deci prima plată vine după ~4 ore.
3. PRL ajunge în walletul tău. Vânzarea: **SafeTrade PRL/USDT**, la 23 sept 2026 cu ~6,6 M USD volum pe 24 h, spread 0,67% și ~26.000 USD în biduri la 2% sub preț (CoinGecko). CoinEx are volum de ~230 de ori mai mic și nu e o ieșire utilă. Verifică în contul SafeTrade că depunerile de PRL sunt deschise înainte de prima plată.

## 5. Economia la 23 septembrie 2026

Formula din ghid: **PRL/zi = H × Y**, **marjă/zi = H × Y × P − 24 × C**.

Randamentul Y din datele de rețea: 2315,7 PRL/bloc × 445–520 blocuri/zi (194 s țintă, 166 s observat) = 1,03–1,21 M PRL/zi, împărțit la 41,72 M TH/s = **0,0247–0,0289 PRL/TH/zi brut**. După fee-ul PPS+ de 2%: 0,0242–0,0283. Tabelul folosește mijlocul, **Y = 0,0263**, și **P = 1,50 USD net/PRL** (bid 1,517 minus comision). Prețurile Salad sunt cele raportate de o recenzie terță; verifică-le în portal.

| GPU | TH/s așteptat | PRL/zi | Venit USD/zi | Salad Batch USD/h | Marjă Batch USD/zi | Salad Standard USD/h | Marjă Standard USD/zi | Break-even TH/s (Batch / Standard) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| RTX 5090 | 320–385 | 8,4–10,1 | 12,6–15,2 | 0,294 | +5,6 … +8,1 | 0,45 | +1,8 … +4,4 | 179 / 274 |
| RTX 4090 | 230–292 | 6,0–7,7 | 9,1–11,5 | 0,204 | +4,2 … +6,6 | 0,34 | +0,9 … +3,4 | 124 / 207 |
| RTX 3090 | 120–160 | 3,2–4,2 | 4,7–6,3 | 0,150 | +1,1 … +2,7 | 0,22 | −0,5 … +1,0 | 91 / 134 |
| RTX 3080 | 105–136 | 2,8–3,6 | 4,1–5,4 | 0,080 | +2,2 … +3,4 | 0,11 | +1,5 … +2,7 | 49 / 67 |

Surse pentru TH/s: [krig README](https://github.com/kryptex/krig-miner) (4090 ~292, 5090 ~385), [măsurători pe Salad ale altui utilizator](https://github.com/pvandenburg1234-eng/pearl-salad-nvidia) (4090 230–290, 5090 ~320, 3080 ~105), [benchmark Pearl Fortune](https://github.com/pearlfortune/pearl-miner#measured-gpu-performance) pentru 3090.

Ce nu e în tabel și reduce marja: minutele facturate până la primul share la fiecare realocare, shares stale pe noduri cu latență mare, noduri cu driver prea vechi care pornesc și mor, ore în care poolul sau RPC-ul nu răspund. Ce ar crește-o: prețul PRL. Ce ar scădea-o: dificultatea, care a crescut constant de la lansare.

**Adâncimea vânzării.** Un GPU produce 6–10 PRL/zi, sub cei ~47 PRL disponibili la best bid, deci ieșirea e realistă pentru un test. La 50 de GPU-uri ar fi ~400 PRL/zi; vândute dintr-o dată, ar coborî prețul cu ~13% pe cartea de ordine de azi.

## 6. Condiții de oprire a testului

Automat, prin script: fără share acceptat în 10 minute de la pornire, fără share 15 minute după, sau GPU sub 20% timp de 15 minute. Containerul iese cu cod 3 și Salad realocă.

Manual, prin tine: hashrate raportat de Kryptex sub break-even-ul clasei GPU după prima oră; mai mult de 3 realocări în 24 h; rejected peste 5% din shares; PRL pending după 6 ore sub 60% din valoarea calculată cu Y; facturarea Salad depășește venitul estimat. În toate cazurile: **Stop** la container group, apoi completezi jurnalul.

## 7. Probleme probabile

| Simptom în log | Cauză probabilă | Ce faci |
| --- | --- | --- |
| `CUDA driver version is insufficient` sau containerul nu pornește | Driverul nodului e mai vechi decât cere krig / imaginea CUDA 12.8 | Nimic. Salad realocă. Dacă se repetă des, alege clase RTX 40/50, unde driverele sunt noi. |
| `nvidia-smi not found` | Salad nu montează utilitarul | Minerul poate merge oricum; watchdog-ul pe GPU se dezactivează singur, rămâne cel pe shares. |
| Linii de share vizibile, dar niciodată `FIRST ACCEPTED SHARE` | `SHARE_REGEX` nu se potrivește | Setează regexul după textul real și repornește. |
| `probe ... unreachable` la toate regiunile | Nodul nu are ieșire pe portul 7048 | Scriptul cade pe hostul global; dacă nici SSL 8048 nu merge, nodul e inutilizabil și watchdog-ul îl închide. |
| Multe `stale` | Latență mare spre pool | Lasă `POOL_AUTO=1`; opțional restrânge `country_codes` în JSON la țări europene. |
| Workerul nu apare pe Kryptex după 15 minute | Wallet greșit sau shares neacceptate | Verifică adresa în log (`cmd:` linia) și numărul `accepted=` din `STATUS`. |

## 8. Surse

- SaladCloud: [rețeta lolMiner](https://docs.salad.com/container-engine/reference/recipes/lolminer), [billing](https://docs.salad.com/container-engine/explanation/billing-pricing/billing), [priorități](https://docs.salad.com/container-engine/explanation/billing-pricing/priority-pricing), [variabile de mediu](https://docs.salad.com/container-engine/how-to-guides/environment-variables), [GHCR](https://docs.salad.com/container-engine/how-to-guides/registries/github-ghcr), [API create container group](https://docs.salad.com/reference/saladcloud-api/container-groups/create-container-group), [API GPU classes](https://docs.salad.com/reference/saladcloud-api/organizations/list-gpu-classes), [termeni](https://salad.com/terms/saladcloud)
- Kryptex: [pool PRL](https://pool.kryptex.com/prl), [how to mine Pearl](https://pool.kryptex.com/articles/how-to-mine-pearl-en), [wallet](https://pool.kryptex.com/articles/pearl-wallet-en), [krig-miner](https://github.com/kryptex/krig-miner)
- Pearl: [monorepo și wallet](https://github.com/pearl-research-labs/pearl), [PearlTrack](https://pearltrack.io/), [CoinEx PEARL/USDT depth](https://api.coinex.com/v2/spot/depth?market=PEARLUSDT&limit=50&interval=0)
- Alte mineri: [SRBMiner-Multi](https://github.com/doktor83/SRBMiner-Multi/releases), [Pearl Fortune miner](https://github.com/pearlfortune/pearl-miner), [container similar al altui utilizator](https://github.com/pvandenburg1234-eng/pearl-salad-nvidia)
