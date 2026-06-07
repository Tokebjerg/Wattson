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

<!-- Nye dage indsættes herunder af den daglige kørsel. -->
