# Wattson — selvlærings-log

Akkumulerende log for den daglige autonome selvlærings-loop (kl. ~21:00).
Hver dag tilføjer én sektion. Processen er defineret i `selvlaering_policy.md`.
Nyeste øverst.

Program: 21 dage, start 2026-06-08. Sikkerhedsgulv + kill-switch
(`input_boolean.wattson_selvlaering`) gælder altid.

---

## Dag 0 — 2026-06-08 (baseline / opsætning)
**Status:** Loop opsat. Sikkerhedsgulv + kill-switch + daglig cron (~21:00) etableret.
**Udgangspunkt (v0.7.9):** Fase A+B+C+D live, peak-sol-eksport, timed override (Fase E del 1),
master-lock + cooldowns (Fase E del 2, verificeret ren på live hardware).
**Næste:** Dag 1 (2026-06-08 21:00) kører første rigtige 24t-analyse.

### Åben observation (fra bruger 2026-06-08) — PRIORITÉR i næste kørsel
Brugeren synes Energiflow-kortet "ser helt forkert ud" og nævner grid-tegn. Hurtig-tjek
2026-06-08 morgen viste dog at `sensor.wattson_grid_power` og rå `out_of_grid_total_power`
følges ad og har korrekt tegn (positiv=køb ved import om natten, negativ=eksport midt på
dagen), og effektbalancen `load = pv + grid + batteri` holdt. Mulig reststøj: 5-min-spikes
ved sky/PV-transienter + batteri-skift. **Dagens fokus i næste kørsel:** lav et formelt
energiflow-/tegn-audit (import OG eksport-perioder, balance-tjek ved flere tidspunkter,
power-flow-kortets batteri/EV-retning) og konkludér om noget skal rettes i mapping.py
tegn-logik eller dashboard-kortet.

### Åben observation (2026-06-08) — EV-strøm-stabilitet, vurderes m. en hel dags data
Efter v0.7.12 (EV-flap/master-lock/override-fix) er Wattson-siden stabil, MEN Easee'en/bilen
rapporterer stadig `awaiting_start`↔`charging` på egen hånd (~hvert 20-60s). Nu uskadeligt
(destabiliserer ikke batteri/inverter/master-lås; bilen trækker ~3,7 kW i snit). Mistanke:
EV dynamic-current re-sends ved små sol-udsving får bilen til at genforhandle.
**LØST (v0.7.13 + v0.7.14):** Deadband alene (v0.7.13) hjalp ikke — live-data viste at den
offerede `dynamic_circuit_limit` bouncede 16A↔6-8A hvert ~15s (sol-tracking-oscillation, for
store udsving til deadband). v0.7.14 tilføjede EV_CURRENT_RETUNE_SECONDS=90 der rate-begrænser
hvor ofte den offerede strøm må ændre sig. VERIFICERET: offeret strøm ændrer sig nu ~hvert
90s; bilen lod uafbrudt 'charging' i ~4 min (før: cyklede hvert 20-45s). Rest: bilen (Niro)
kan stadig selv tapere/trække lidt (car-side, uden for Wattsons kontrol).
**Opgave i 21:00-kørslen:** bekræft over en hel dag at cyklingen er væk; vurder om 90s er den
rette værdi (kortere = mere sol-responsiv, længere = roligere bil).

<!-- Nye dage indsættes herunder af den daglige kørsel. -->
