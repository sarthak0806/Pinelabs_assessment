# Demo script: PayInsight Analytics (about 6 minutes)

**Open:** https://modanwalsarthak0806--payinsight-analytics-ui.us-east.modal.run, or run `.\run_app.bat` locally.

**Sidebar defaults:**
- role **Payments Analyst**;
- window **October 2026 vs September 2026**;
- LLM card **OpenRouter · nvidia/nemotron-3.5-super-vl:free** (green dot, no key needed).

All data is synthetic. Every number below is the same with or without an LLM.

---

## 0. Opening (30s)
- Point to the header badge **"Synthetic data"** and the LLM status card.
- KPI cards: October **84.95%**, **−7.85pt** vs September (92.80%); 2,000 vs 2,167 transactions; data-quality score.
- Message: *"Leaders see the drop. Analysts usually spend hours explaining it."*

## 1. Success-rate decline investigation (2 min)
**Click the card** 📉 *Sep → Oct decline*.

1. **Headline:** "Success rate fell from 92.8% in Sep 2026 to 85.0% in Oct 2026 (−7.85pt)", with the Nemotron-written summary underneath ("passed grounding check").
2. **Metric strip:** baseline, current, rate effect, mix effect, and **top driver UPI @ Cobalt Bank (68% of the change)**.
3. **Charts:**
   - **Waterfall:** 92.8%, then the large UPI @ Cobalt bar, the other segments and mix, ending at 85.0%.
   - **Trend:** monthly line with Sep and Oct highlighted.
   - **Contribution bars:** rate vs mix effect by category.
   - **Bank comparison:** Cobalt collapses.
   - **Failure reasons donut.**
4. **Findings:**
   - Facts, each with an evidence line (tables · filters · window · n).
   - **Hypothesis**, labelled: mostly "Technical Error", so likely a gateway issue.
   - **Recommendation:** pull Cobalt UPI gateway logs and open an incident with the PSP.
5. **🧭 Query Flow tab** (last tab):
   - the step strip from *Understand question* to *Narrate*;
   - **Draft SQL** shows the exact SQL Nemotron wrote and "✅ accepted by validator";
   - **Validate SQL** shows the checklist and the LIMIT applied;
   - **Every query executed** lists Q1–Q3 with parameters, rows and timings.
6. **🔎 Drill-down:** switch dimensions and point to **"Exact-sum check ✓"**.

## 2. Mix-shift example (1 min)
**Click** 🔀 *Jul → Aug mix shift*.
- 92.85% → 89.86% (−2.99pt). By merchant category: **mix effect −2.62pt (88%)**, rate effect −0.37pt.
- Recommendation: *"Do not escalate as a system failure"*; review the Utilities portfolio instead.
- Point: the same kind of drop, but a different action, because the decomposition separates **performance** from **composition**.

## 3. Data quality and access control (1.5 min)
**Click** 🏦 *By bank · data quality*.
- Bars with 95% confidence whiskers and a dashed overall-average line. Cobalt Bank is the worst. "Unknown bank" is greyed with ⚠ and pushed to the bottom.
- **🩺 Data Quality tab:**
  - 5 duplicate IDs removed;
  - rows with no bank;
  - orphan merchant `M999`;
  - invalid payment method `Crypto`;
  - `food`/`FOOD`/`Misc` category spellings;
  - `tech_error` reason variants;
  - 3 undated rows excluded.

**Switch the sidebar role to "Merchant Analyst"** and ask the same question:
- **"⛔ Access denied: query blocked before execution"**.
- The Query Flow shows it stopped at **Validate SQL**, and the blocked SQL was never executed.
- Mention the second layer: the database authorizer would refuse the read even if the validator were bypassed.

## 4. Two sources disagree (45s)
Switch back to Payments Analyst. **Click** 🧾 *Settlement mismatch*.
- Gateway: 1,699 successes (84.95%). Settlement system: 1,661 settled (83.05%). **Gap of 38**:
  - 30 successes have no settlement record;
  - 20 are still pending;
  - 12 failed transactions were settled anyway.

## 5. Ambiguity and model choice (45s)
- **Click** ❓ *Problematic banks?* → "'Problematic' is ambiguous…", with 3 one-click options. The Query Flow shows **no SQL was run**.
- **Click** ⏱️ *Dropped 'recently'?* → a time-window clarification.
- Open **⚙ Change model / use your own API key**:
  - Claude and Kimi ask for the visitor's own key, which stays in their session only;
  - Nemotron needs none, and its model is locked so nobody can spend the app's credits on paid models.
- **🛡️ Audit & Evaluation → Run benchmark:** 13/13 known-answer questions pass, with zero unsafe SQL executed.

## Closing line
*"Every number comes from validated, read-only SQL plus deterministic math. The LLM only helps understand the question and word the answer, and the Query Flow shows exactly what it did."*
