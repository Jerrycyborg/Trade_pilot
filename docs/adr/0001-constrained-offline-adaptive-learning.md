# ADR 0001 — A constrained offline improvement loop

**Status:** Proposed. No learner code exists, and none should be written until
the hardening acceptance criteria pass and have been reviewed.

**Date:** 2026-08-31

---

## Context

The system can now measure itself: a point-in-time archive of what it saw, an
implementation-shortfall record of what execution cost, walk-forward validation
that prices in the search that found a result, and a lifecycle roster where
evidence decides what may trade. What it cannot do is *improve*. Every
parameter, every rule and every threshold is set by a human editing code.

The obvious next step is a learning loop, and the obvious way to build one is
the dangerous way: let a model observe outcomes, adjust the strategy, and
redeploy. That is how an autonomous trading system destroys an account
overnight — not because the model is stupid, but because a fast feedback loop
with no independent check amplifies its own errors, and a model that can edit
its own safety limits has no safety limits.

The TradingAgents line of work suggests a shape worth borrowing: specialist
research roles that argue, an explicit adversarial step, and a separate risk
authority. What follows adapts that to a system where the output is a
*proposal*, never a deployment.

## Decision

Build an **offline, proposal-only** improvement loop. It may write artifacts.
It may not write code, configuration, or state.

### Roles

Typed specialists, each with a declared input scope, so a claim can be traced
to the data behind it:

| Role | Reads | Produces |
|---|---|---|
| Market | regime, volatility, breadth | regime classification with evidence |
| Technical | the point-in-time bar archive | signal-quality assessment |
| News | headline archive with observed-at times | event summary, timestamped |
| Sentiment | sentiment archive | score with provenance |
| Fundamentals | filings, estimates | valuation context |

Each reads **only through the point-in-time archive**. A specialist that can
see a revision the live system had not received is a specialist that will
produce unreproducible conclusions.

### Structured argument

Bull, bear and neutral positions are constructed as separate artifacts with
explicit claims and the evidence for each. This is not a debating exercise: the
purpose is that a later reader can see which claim turned out to be wrong and
why, which is the input to error attribution.

### Independent risk and compliance veto

A separate component with authority to reject, and no authority to approve.
It cannot be argued with by the other roles, does not see their conclusions
before forming its own, and its rejection is final within the loop. Modelled on
the reconciliation halt: a veto that can be talked out of is not a veto.

### Post-trade error attribution

For each closed position, attribute the outcome across: signal quality, entry
timing, execution cost (from the shortfall record), exit discipline, and regime
misclassification. Counterfactual analysis answers "what would this trade have
returned with a different exit rule?" — computed against the archive as it
stood, never against the corrected series.

### Challenger strategies

The loop's output is a **versioned challenger artifact**: bounded parameters,
feature weights, or a new strategy version. Never a code change.

Challengers are evaluated with the machinery that already exists: purged
walk-forward with an embargo, the deflated Sharpe ratio counting the
challengers themselves as trials, and portfolio correlation against live
sleeves. A challenger that survives is registered as a `candidate` sleeve and
climbs the same ladder as anything else — paper, evidence, human approval,
live. It gets no shortcut for having been generated rather than written.

Champion/challenger comparison runs in paper, on the same symbols and the same
window, with both sleeves' fills recorded under `environment='paper'` and
distinguished by strategy version.

## Constraints

These are the decision, not caveats to it.

1. **The learner never deploys.** It writes artifacts to
   `lifecycle.validation_artifact` and proposals to a store. Promotion remains
   a human action through the existing gates.
2. **The learner never edits production code, prompts in place, or safety
   policy.** Thresholds live in validated, versioned configuration. A proposal
   to change one is a proposal, reviewed like any other.
3. **The learner never enables live mode.** That switch is an operator row in
   `lifecycle.execution_environment` and is out of scope for any automated
   component.
4. **The learner never promotes itself.** A challenger it produced is subject
   to the same gates, including the ones it cannot influence.
5. **LLM output is never a control input.** No prompt may determine broker
   mode, lifecycle state, position limits, risk ceilings, reconciliation
   outcomes, or promotion. Model output is an *argument about data*; the
   thresholds it is judged against are configuration.
6. **Offline only.** The loop runs on archived data on a schedule. It is not in
   the order path and cannot be, so a slow or failed run degrades research and
   never trading.
7. **Bounded proposals.** Parameter proposals are clamped to ranges declared in
   configuration. A learner that can propose a 100× position size is one
   review-fatigue error away from being catastrophic.

## Roadmap

Each phase gated on the previous one being reviewed.

- **L0 — attribution only.** Post-trade attribution and counterfactuals over
  the existing archive. No proposals. Purpose: find out whether the recorded
  data is rich enough to explain outcomes. If it is not, everything after this
  is built on sand.
- **L1 — specialist artifacts.** The typed roles, producing structured
  assessments with provenance. Still no proposals. Purpose: establish whether
  the arguments are reproducible from the archive.
- **L2 — the risk veto.** Independent rejection, exercised against L1 output.
  Built before anything can propose, deliberately.
- **L3 — bounded challengers.** Parameter and weight proposals within declared
  ranges, evaluated by purged walk-forward and deflated Sharpe.
- **L4 — champion/challenger in paper.** Both running, both recorded,
  compared. Human-approved promotion only.

## Consequences

**Accepted.** The loop will be slower than a system that redeploys itself, and
will sometimes be right about a change that a human declines to approve. That
is the cost of the constraint and it is worth paying: the failure mode of the
alternative is unbounded.

**Accepted.** Attribution over a small sample will be noisy, and L0 may
conclude the archive is not yet rich enough. That is a useful finding, not a
failure.

**Risk.** Review fatigue. A loop producing many plausible proposals trains its
reviewer to approve them. Mitigation: the loop should propose rarely and
justify heavily, and proposal volume should itself be monitored.

**Risk.** The deflated Sharpe ratio's trial count already under-counts human
iterations. A generator that produces hundreds of challengers makes that worse
unless every one is counted. Every challenger evaluated must increment the
trial count for every other, or the statistic becomes decorative.

**Open question.** Whether specialist roles should be LLM-backed at all, or
whether typed deterministic analysers would produce the same arguments more
cheaply and reproducibly. L1 is where that gets answered, and answering it
honestly means being willing to conclude "no".
