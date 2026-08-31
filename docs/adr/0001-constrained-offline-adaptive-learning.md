# ADR 0001 — A constrained offline improvement loop

**Status:** Partially implemented. L0 (attribution), L1 (specialist artifacts)
and L2 (the risk veto) are built. All three are read-only: nothing among them
proposes a change, and the veto can only refuse. L3 (bounded challengers) is
the first phase that would propose anything and is not started. Each remaining
phase is gated on review of the previous one.

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

- **L0 — attribution only. Implemented** (`libs/attribution`,
  `scripts/attribute_trades.py`). Post-trade attribution and counterfactuals
  over the existing archive. No proposals, no writes back, no path to changing
  what the system trades.

  The decomposition is an exact identity — signal + entry execution + exit
  execution reconstructs the realised result — so the components can be argued
  with rather than merely believed. Anything approximate (excursions, capture
  ratio, exit reason, regime) is reported as a diagnostic instead of being
  folded in, because folding an approximation into an identity is how the
  identity stops being one.

  Counterfactuals read `Journal.bars_as_of(exit)`, so a revision the live
  system never received cannot decide that a different exit was better.

  What it found on the first real run: coverage is the deliverable, not
  performance. An attribution that cannot be computed names the field it is
  missing rather than substituting a zero, and the report's verdict is written
  so a low number reads as a finding rather than a failure. On a representative
  paper archive it separated "the signal earned X, execution took most of it"
  from "the strategy lost money" — a distinction the realised number alone
  cannot make, and the reason this phase comes first.

  Regime attribution (`attribution.regime`) closes the one thing L0 shipped
  without. The price decomposition cannot distinguish a wrong rule from a right
  rule applied in the wrong conditions, and those imply different work: a rule
  to change, or a filter to add. Each trade now carries the regime at both
  ends, classified from `bars_as_of` at the moment being classified — the entry
  from the entry-time series, since a revision that arrived during the hold was
  not knowable when the entry was decided — and results are grouped by the
  regime each trade was entered into. On the demonstration archive the headline
  read `+335 realised` while the slices read `+603` in a trend and `-268` in a
  range.

  This is also the gate on L1 in a way worth stating plainly. L1's specialists
  are supposed to argue about market conditions; if the archive cannot classify
  a condition well enough to say which regime a trade was taken in, an LLM
  narrating one is generating text, not reading data. Regime slices are the
  cheap version of that check, and they had to work first.
- **L1 — specialist artifacts. Implemented** (`libs/specialists`,
  `scripts/specialist_report.py`). The typed roles, producing structured
  assessments with provenance. No proposals, and no component reads the output
  to decide anything.

  Two constraints are enforced in code rather than documented. A specialist is
  handed a `PointInTimeArchive` pinned to a moment, never the journal, and that
  object has no method returning the corrected series — so a role cannot
  consult one even by mistake. And a `Claim` constructed without evidence
  raises, rather than being filtered out later by something that might not run.

  **Reproducibility is measured, not asserted**, since establishing it is the
  phase's entire purpose. Determinism is the easy half and a deterministic
  analyser gets it free. The half that bites is point-in-time isolation: a role
  can be perfectly deterministic and still silently improve every time the
  archive is corrected, which makes every historical conclusion unfalsifiable,
  because re-running it never reproduces what was originally said. A test
  records a series, assesses at T, stores a revision that would flip the
  classification, re-assesses at T, and requires an identical digest — with a
  second test requiring that an assessment made *after* the revision does see
  it, so the first cannot pass by ignoring revisions altogether.

  **The finding: two of the five specified roles have an archive to read.**
  News has no headline store with observed-at times. Sentiment is computed on
  request into a process-local dict and never persisted, so no past score is
  recoverable. Fundamentals has a research table, but it is a TTL cache keyed
  by symbol — it holds the current answer rather than the sequence of answers,
  which is the opposite of a point-in-time archive. All three are kept in the
  roster reporting `unavailable` with the specific storage each needs, because
  a missing role is a gap someone has to close and an absent one is a gap
  nobody can see. None was built against its live source: an assessment "as of"
  a past moment constructed from today's data is precisely the leakage the
  archive exists to prevent, and it would not be visible in the output.

  On a two-symbol archive the roles disagreed usefully. For a ranging symbol
  the technical role reported a bullish average cross and positive MACD while
  the market role reported no directional trend — "a trend-following entry here
  is being taken in conditions it is not built for". That is the same finding
  L0's regime slices produced from realised losses, recovered from the archive
  *before* a trade rather than after it, which is the first evidence that these
  arguments are worth anything.
- **L2 — the risk veto. Implemented** (`libs/veto`, composed into
  `scripts/specialist_report.py`). Independent rejection, exercised against L1
  output, built before anything can propose.

  Three properties are arranged rather than promised. **Independence** is a
  signature: `review()` takes a journal and a subject and has no parameter for
  a specialist argument, so it cannot be handed one. **Rejection-only** is a
  type: there is no `approved`, `ok` or `passed` field, and
  `VetoDecision.__bool__` raises, because the way this authority normally gets
  lost is a caller writing `if veto_ok(x):` — after which `not rejected` and
  `approved` are the same bit and a reader believes a green light means the
  subject was endorsed. **Finality** is a frozen dataclass with no override,
  and the CLI has no flag to skip the veto: a veto with a `--force` is not one.

  Its scope is deliberately narrow — whether the subject can be *reasoned
  about* at all: enough archived history, a series that is current, not so
  gapped that continuity claims are meaningless, and an instrument whose orders
  are not being persistently rejected. Nothing here judges merit. A veto with
  merit criteria is an approver with a negative sign, which is the thing
  separating it was meant to prevent, and `VetoPolicy` is asserted to carry no
  field expressing one.

  Rules that could not run are reported in `unchecked`. A veto that skipped
  half its checks and said nothing is indistinguishable from one that ran them
  all and found nothing, and only one of those is worth having.

  Running it found a defect in its own staleness rule. `Journal.completeness`
  reports `stale_minutes=None` when the window contains no bars at all — which
  is exactly the case the rule exists for. A symbol whose series stopped three
  days ago has an empty 48-hour window, so the deadest symbol in the archive
  drew no objection while a merely-late one was caught. Staleness now comes
  from the freshest bar actually held rather than from the windowed view.
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

**Answered at L1: no, not for the roles that are buildable today.** The two
roles with an archive — market and technical — make claims that are arithmetic
over an archived series: an ADX reading against a threshold, an average cross,
a histogram sign. A model restating those would add a paraphrase and remove the
property the phase was built to establish, since the same prompt and the same
data need not produce the same words, and a digest that changes between runs
cannot distinguish "the market changed" from "the model did".

That is not an argument that models have no place here. It is an argument that
the place is not this one. The three blocked roles are exactly the ones whose
input is unstructured text — headlines, filings, commentary — where a model
would do work no threshold can. They cannot be built at all until their
archives exist, so the question of how to make a model-backed role reproducible
does not arise yet, and answering it early would have meant answering it
speculatively.

Two things should be settled before it does. A model-backed role needs its
output pinned to a recorded prompt, model version and seed, or its assessments
are not reproducible in the sense L1 established and every historical claim it
makes is unfalsifiable. And constraint 5 still binds: whatever such a role
emits is an argument about data, never a control input.
