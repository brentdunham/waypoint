# Grounded calibration: labels and rationale (agreed 2026-10-09)

Scale: 4 every claim supported and cited to a source that carries it; 3 a minor detail uncited, or right finding but a less specific source; 2 one clear unsupported claim or a citation to the wrong source, or mostly uncited; 1 several unsupported claims or wrong citations.

| # | id | label | why |
|---|---|---|---|
| 1 | `grounded-q-fuel-real` | **3** | Real briefing text. Every claim is in finding 1, but both sentences cite [8], whose only quote is the ~900 kg/yr figure; the Centrus licence, the 2029 date and TerraPower's slip are quoted from [7]. Right finding, less specific source. |
| 2 | `grounded-q-fuel-fixed` | **4** | The real paragraph with citations corrected to [7][8] and [7]: every claim is in finding 1 and each cited source's quotes carry it. |
| 3 | `grounded-q-fuel-invented` | **2** | Corrected paragraph plus one invented sentence: the finding says 2029, and [9] is about DOE funding, not a Centrus date. One clear unsupported claim. |
| 4 | `grounded-q-aioffice-real` | **4** | Real briefing text. Powers and the absence of reported enforcement are both in finding 4; [18][19][20] quote the powers, and the 'no fine reported' caveat is the finding's own. |
| 5 | `grounded-q-aioffice-wrongcite` | **2** | Real paragraph with the second sentence's citation changed to [21]: same finding, but [21] is Meta declining the Code, nothing about enforcement. A citation to the wrong source. |
| 6 | `grounded-q-aioffice-invented` | **1** | Two unsupported claims: the finding gives 3% (not 7%), and it says no investigation has been reported; [19] supports neither. Several unsupported claims. |
| 7 | `grounded-q-bechtel-real` | **3** | Real briefing text. All in finding 6, but the 'questions about cost and the 2030 schedule' come from [35], which is not cited; [32][33] cover the split and rebid. Right finding, less specific source. |
| 8 | `grounded-q-bechtel-fixed` | **4** | Real paragraph with the schedule clause cited to [35]: each claim now points at the source that quotes it. |
| 9 | `grounded-q-bechtel-uncited` | **2** | The corrected paragraph with every [n] removed. Supported by the findings, but no citations at all: at most 2 by the rubric. |
| 10 | `grounded-q-xid-real` | **2** | Real briefing text. 'Catastrophic data loss ... bytes remain on disk' is cited to [1][3]; that is quoted from [2], and [3]'s quote is about refusing writes at 100% utilisation, a different claim. A citation to the wrong source. |
| 11 | `grounded-q-xid-fixed` | **4** | Real paragraph with citations corrected: data loss to [1][2], the two-billion rule to [2], the older-docs note to [2][4]. |
| 12 | `grounded-q-xid-invented` | **1** | Corrected paragraph plus an invented claim that contradicts finding 0 (32-bit XIDs, vacuum required) and cites [1], whose quote says the opposite. |

## The paragraphs

### 1. `grounded-q-fuel-real`, proposed 3

> Fuel supply is the binding physical constraint. The US has only one NRC-licensed HALEU producer (Centrus), current output is roughly 900 kg/year against the gigawatt-scale needs of designs from TerraPower, X-energy, Oklo and NuScale, and meaningful capacity (12 metric tons/year) isn't expected until 2029 [8]. TerraPower's own commercial-operation target slipped from 2028 to 2030-31 largely because of fuel availability [8].

- [8] State of Small Modular Reactors 2026 — Annual Intelligence Report: _Current US Production ~900 kg/yr_

### 2. `grounded-q-fuel-fixed`, proposed 4

> Fuel supply is the binding physical constraint. The US has only one NRC-licensed HALEU producer (Centrus), current output is roughly 900 kg/year against the gigawatt-scale needs of designs from TerraPower, X-energy, Oklo and NuScale, and meaningful capacity (12 metric tons/year) isn't expected until 2029 [7][8]. TerraPower's own commercial-operation target slipped from 2028 to 2030-31 largely because of fuel availability [7].

- [7] BASELOAD SPECIAL: SMALL MODULAR REACTORS -- Safety, Hazards, Applications: _## Fuel supply Centrus Energy, the only NRC-licensed HALEU producer, finished its demonstration production under a DOE contract in June 2026 and is ta... / The commercial-operation target moved from 2028 to 2030-31 primarily because of fuel availability. / NuScale’s Utah project saw its target power price climb from $55 per megawatt-hour to $89 before members of the Utah Associated Municipal Power_
- [8] State of Small Modular Reactors 2026 — Annual Intelligence Report: _Current US Production ~900 kg/yr_

### 3. `grounded-q-fuel-invented`, proposed 2

> Fuel supply is the binding physical constraint. The US has only one NRC-licensed HALEU producer (Centrus), current output is roughly 900 kg/year against the gigawatt-scale needs of designs from TerraPower, X-energy, Oklo and NuScale, and meaningful capacity (12 metric tons/year) isn't expected until 2029 [7][8]. TerraPower's own commercial-operation target slipped from 2028 to 2030-31 largely because of fuel availability [7]. DOE now expects Centrus to reach the full 12-tonne output by 2027 [9].

- [7] BASELOAD SPECIAL: SMALL MODULAR REACTORS -- Safety, Hazards, Applications: _## Fuel supply Centrus Energy, the only NRC-licensed HALEU producer, finished its demonstration production under a DOE contract in June 2026 and is ta... / The commercial-operation target moved from 2028 to 2030-31 primarily because of fuel availability. / NuScale’s Utah project saw its target power price climb from $55 per megawatt-hour to $89 before members of the Utah Associated Municipal Power_
- [8] State of Small Modular Reactors 2026 — Annual Intelligence Report: _Current US Production ~900 kg/yr_
- [9] Financing the Nuclear Renaissance, Part II : How SMRs Will Actually Be Paid For : _**Designs dependent on HALEU with no diversified supply route** When DOE itself says HALEU shortages “threaten timelines for NuScale, Oklo, TerraPowe... / **The HALEU Funding Push: Billions To Solve One Problem** DOE’s January 2026 announcement — awarding $2.7 billion in HALEU and LEU enrichment task or..._

### 4. `grounded-q-aioffice-real`, proposed 4

> On enforcement, the AI Office (not national authorities) directly oversees GPAI compliance, with power to demand documentation, run evaluations, and impose fines or market withdrawal [18][19][20]. As of early October 2026, no fine, investigation, or withdrawal order against a GPAI provider has been reported, though this is based on limited commentary rather than an official register [18][19].

- [18] EU begins enforcing AI Act, putting AI models under the microscope - Help Net Se: _The AI Office handles general-purpose AI models directly, with power to request technical documentation, run evaluations, demand corrective steps and ... / National competent authorities take on other AI systems operating within their borders._
- [19] EU AI Act Enforcement Phase Begins: _These powers are significant and include requesting information and documentation, obtaining access to models for evaluation, requiring corrective or ... / The substantive obligations for providers of GPAI models placed on the market on or after August 2, 2025, have been applicable since that date, althou..._
- [20] Frequently Asked Questions: _The powers include requesting information, requesting access to a model for evaluations, requiring risk mitigation measures, and issuing fines of up t... / **The AI Office may use these powers in cases where technical compliance dialogues are not sufficient._

### 5. `grounded-q-aioffice-wrongcite`, proposed 2

> On enforcement, the AI Office (not national authorities) directly oversees GPAI compliance, with power to demand documentation, run evaluations, and impose fines or market withdrawal [18][19][20]. As of early October 2026, no fine, investigation, or withdrawal order against a GPAI provider has been reported, though this is based on limited commentary rather than an official register [21].

- [18] EU begins enforcing AI Act, putting AI models under the microscope - Help Net Se: _The AI Office handles general-purpose AI models directly, with power to request technical documentation, run evaluations, demand corrective steps and ... / National competent authorities take on other AI systems operating within their borders._
- [19] EU AI Act Enforcement Phase Begins: _These powers are significant and include requesting information and documentation, obtaining access to models for evaluation, requiring corrective or ... / The substantive obligations for providers of GPAI models placed on the market on or after August 2, 2025, have been applicable since that date, althou..._
- [20] Frequently Asked Questions: _The powers include requesting information, requesting access to a model for evaluations, requiring risk mitigation measures, and issuing fines of up t... / **The AI Office may use these powers in cases where technical compliance dialogues are not sufficient._
- [21] Meta declines to sign EU AI code of practice: _We have carefully reviewed the European Commission's Code of Practice for general-purpose AI (GPAI) models and Meta won't be signing it," Kaplan said ... / OpenAI and Mistral have already signed the code._

### 6. `grounded-q-aioffice-invented`, proposed 1

> On enforcement, the AI Office (not national authorities) directly oversees GPAI compliance, with power to demand documentation, run evaluations, and impose fines of up to 7% of global turnover [20]. As of early October 2026, no fine, investigation, or withdrawal order against a GPAI provider has been reported, though this is based on limited commentary rather than an official register [18][19]. The AI Office opened its first formal investigation into a GPAI provider in September 2026 [19].

- [18] EU begins enforcing AI Act, putting AI models under the microscope - Help Net Se: _The AI Office handles general-purpose AI models directly, with power to request technical documentation, run evaluations, demand corrective steps and ... / National competent authorities take on other AI systems operating within their borders._
- [19] EU AI Act Enforcement Phase Begins: _These powers are significant and include requesting information and documentation, obtaining access to models for evaluation, requiring corrective or ... / The substantive obligations for providers of GPAI models placed on the market on or after August 2, 2025, have been applicable since that date, althou..._
- [20] Frequently Asked Questions: _The powers include requesting information, requesting access to a model for evaluations, requiring risk mitigation measures, and issuing fines of up t... / **The AI Office may use these powers in cases where technical compliance dialogues are not sufficient._

### 7. `grounded-q-bechtel-real`, proposed 3

> Execution risk compounds the above: Bechtel's exit from the Natrium EPC contract in 2025/26, with the companies unable to agree on next-phase terms, forces TerraPower to rebid a ~$4 billion contract, raising unresolved questions about cost and the 2030 schedule [32][33].

- [32] TerraPower, Bechtel part ways on the Kemmerer Natrium project - ExchangeMonitor: _after failing to come to an agreement on the next stages, according to public disclosure papers filed in Virginia._
- [33] TerraPower Reopens EPC Competition for $4B Natrium Nuclear Project: _Bechtel spokeswoman Molly Edwards said the contractor's work to date was performed under an earlier phase of its agreement with TerraPower, before the... / # TerraPower Reopens EPC Competition for $4B Natrium Nuclear Project ## Longtime contractor Bechtel exits full phase work five months after nuclear co..._

### 8. `grounded-q-bechtel-fixed`, proposed 4

> Execution risk compounds the above: Bechtel's exit from the Natrium EPC contract in 2025/26, with the companies unable to agree on next-phase terms, forces TerraPower to rebid a ~$4 billion contract [32][33], raising unresolved questions about cost and the 2030 schedule [35].

- [32] TerraPower, Bechtel part ways on the Kemmerer Natrium project - ExchangeMonitor: _after failing to come to an agreement on the next stages, according to public disclosure papers filed in Virginia._
- [33] TerraPower Reopens EPC Competition for $4B Natrium Nuclear Project: _Bechtel spokeswoman Molly Edwards said the contractor's work to date was performed under an earlier phase of its agreement with TerraPower, before the... / # TerraPower Reopens EPC Competition for $4B Natrium Nuclear Project ## Longtime contractor Bechtel exits full phase work five months after nuclear co..._
- [35] Bechtel Exits TerraPower, Holtec Delays IPO: Nuclear Sector: _Bechtel's departure raises hard questions about Natrium's 2030 timeline and whether costs will balloon as TerraPower hunts for a replacement contracto..._

### 9. `grounded-q-bechtel-uncited`, proposed 2

> Execution risk compounds the above: Bechtel's exit from the Natrium EPC contract in 2025/26, with the companies unable to agree on next-phase terms, forces TerraPower to rebid a ~$4 billion contract, raising unresolved questions about cost and the 2030 schedule.


### 10. `grounded-q-xid-real`, proposed 2

> The core problem: PostgreSQL's XIDs are 32-bit counters underpinning MVCC visibility, and after ~4 billion transactions they wrap around; without intervention, old committed rows would appear to be from the future and effectively vanish from query results — the official docs call this catastrophic data loss, even though the underlying bytes remain on disk [1][3]. The fix is freezing: VACUUM marks old tuples as permanently visible to all transactions, and documentation has long required every table to be vacuumed at least once every two billion transactions [2][4] (note: older 8.2/9.4 docs describe slightly different wraparound arithmetic, so exact wording has evolved) [2][4].

- [1] PostgreSQL: Documentation: 19: 67.1. Transactions and Identifiers: _Transactions and Identifiers # The internal transaction ID type xid is 32 bits wide and wraps around every 4 billion transactions. / Xids are used as the basis for PostgreSQL's MVCC concurrency mechanism and streaming replication._
- [2] PostgreSQL: Documentation: 9.4: Routine Vacuuming: _In short, catastrophic data loss. / (Actually the data is still there, but that's cold comfort if you cannot get at it.) / To avoid this, it is necessary to vacuum every table in every database at least once every two billion transactions._
- [3] Prevent transaction ID wraparound: _Once the transaction ID utilization percentage reaches 100%, PostgreSQL stops accepting write queries._
- [4] PostgreSQL: Documentation: 8.2: Routine Vacuuming: _Normal XIDs are compared using modulo-2^31 arithmetic._

### 11. `grounded-q-xid-fixed`, proposed 4

> The core problem: PostgreSQL's XIDs are 32-bit counters underpinning MVCC visibility, and after ~4 billion transactions they wrap around; without intervention, old committed rows would appear to be from the future and effectively vanish from query results — the official docs call this catastrophic data loss, even though the underlying bytes remain on disk [1][2]. The fix is freezing: VACUUM marks old tuples as permanently visible to all transactions, and documentation has long required every table to be vacuumed at least once every two billion transactions [2] (note: older 8.2/9.4 docs describe slightly different wraparound arithmetic, so exact wording has evolved) [2][4].

- [1] PostgreSQL: Documentation: 19: 67.1. Transactions and Identifiers: _Transactions and Identifiers # The internal transaction ID type xid is 32 bits wide and wraps around every 4 billion transactions. / Xids are used as the basis for PostgreSQL's MVCC concurrency mechanism and streaming replication._
- [2] PostgreSQL: Documentation: 9.4: Routine Vacuuming: _In short, catastrophic data loss. / (Actually the data is still there, but that's cold comfort if you cannot get at it.) / To avoid this, it is necessary to vacuum every table in every database at least once every two billion transactions._
- [4] PostgreSQL: Documentation: 8.2: Routine Vacuuming: _Normal XIDs are compared using modulo-2^31 arithmetic._

### 12. `grounded-q-xid-invented`, proposed 1

> The core problem: PostgreSQL's XIDs are 32-bit counters underpinning MVCC visibility, and after ~4 billion transactions they wrap around; without intervention, old committed rows would appear to be from the future and effectively vanish from query results — the official docs call this catastrophic data loss, even though the underlying bytes remain on disk [1][2]. The fix is freezing: VACUUM marks old tuples as permanently visible to all transactions, and documentation has long required every table to be vacuumed at least once every two billion transactions [2] (note: older 8.2/9.4 docs describe slightly different wraparound arithmetic, so exact wording has evolved) [2][4]. Since PostgreSQL 13, XIDs are 64-bit on disk, so anti-wraparound vacuums are no longer needed [1].

- [1] PostgreSQL: Documentation: 19: 67.1. Transactions and Identifiers: _Transactions and Identifiers # The internal transaction ID type xid is 32 bits wide and wraps around every 4 billion transactions. / Xids are used as the basis for PostgreSQL's MVCC concurrency mechanism and streaming replication._
- [2] PostgreSQL: Documentation: 9.4: Routine Vacuuming: _In short, catastrophic data loss. / (Actually the data is still there, but that's cold comfort if you cannot get at it.) / To avoid this, it is necessary to vacuum every table in every database at least once every two billion transactions._
- [4] PostgreSQL: Documentation: 8.2: Routine Vacuuming: _Normal XIDs are compared using modulo-2^31 arithmetic._
