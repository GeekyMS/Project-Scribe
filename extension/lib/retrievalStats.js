// Reads the v2 enrichment cache (lib/enrichmentCache.js's "papers" store)
// and reports resolution/abstract/full-text health. Nothing computed this
// way before existed anywhere in the codebase — run_validation_campaign.py's
// evidence_coverage was the closest proxy, and it conflates "no abstract
// found" with "judge couldn't decide" (see the retrieval-rebuild plan).
//
// The two headline numbers are rate_limited_count and
// unresolved_incomplete_count: in a healthy run they're near zero. Before
// the outcome-tracking rewrite of resolvers.js, a rate limit was silently
// indistinguishable from a genuine miss, so this histogram literally could
// not have been computed — every cached attempt looked clean.

(() => {
  function summarize(papers) {
    const stats = {
      unique_papers: papers.length,
      abstract_state: {},
      status: {},
      full_text_status: {},
      backfilled_from: {},
      attempt_outcomes_by_provider: {},
      rate_limited_count: 0,
      unresolved_incomplete_count: 0,
      had_inconclusive_attempt_count: 0,
    };

    for (const p of papers) {
      const ev = p.evidence || {};
      const abstractState = ev.abstract_state || "unknown";
      const status = ev.status || "not_attempted";
      const ftStatus = (ev.full_text || {}).status || "not_attempted";

      stats.abstract_state[abstractState] = (stats.abstract_state[abstractState] || 0) + 1;
      stats.status[status] = (stats.status[status] || 0) + 1;
      stats.full_text_status[ftStatus] = (stats.full_text_status[ftStatus] || 0) + 1;
      if (ev.abstract_backfilled_from) {
        stats.backfilled_from[ev.abstract_backfilled_from] = (stats.backfilled_from[ev.abstract_backfilled_from] || 0) + 1;
      }
      if (status === "unresolved_incomplete") stats.unresolved_incomplete_count += 1;
      if (ev.had_inconclusive_attempt) stats.had_inconclusive_attempt_count += 1;

      for (const a of ev.attempts || []) {
        const provider = a.provider || "unknown";
        const outcome = a.outcome || "unknown";
        if (!stats.attempt_outcomes_by_provider[provider]) stats.attempt_outcomes_by_provider[provider] = {};
        stats.attempt_outcomes_by_provider[provider][outcome] = (stats.attempt_outcomes_by_provider[provider][outcome] || 0) + 1;
        if (outcome === "rate_limited") stats.rate_limited_count += 1;
      }
    }

    return stats;
  }

  async function computeStats() {
    const papers = await self.ScribeEnrichmentCache.getAll();
    return summarize(papers);
  }

  self.ScribeRetrievalStats = { computeStats, summarize };
})();
