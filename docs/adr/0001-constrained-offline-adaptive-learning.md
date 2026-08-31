# ADR 0001 — A constrained offline improvement loop

**Status:** Partially implemented. L0 (attribution), L1 (specialist artifacts)
L2 (the risk veto), L3 (bounded challengers) and L4 (champion/challenger in
paper) are built. L0-L2 are read-only. L3 proposes and can do nothing else. L4
is the only phase that writes: it persists proposals and registers a challenger
as a *paper* sleeve, which a categorical barrier prevents from ever reaching
live without a named person adopting it. Real-money execution remains disabled
by default and no automated component can enable it.

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
- **L3 — bounded challengers. Implemented** (`libs/challengers`). The first
  phase that proposes anything. What keeps that safe is not the generator being
  careful — it is that a proposal has nowhere to go. A `Challenger` is frozen,
  carries no lifecycle state, no sleeve id and no environment, and has no
  method that writes; there is nothing on it for a promotion path to read, and
  a test asserts the package never imports the lifecycle authority.

  **Clamping, not validation.** A validator rejects and lets the caller retry,
  which under a generator means it eventually proposes whatever it wanted. An
  out-of-range value is pulled to the bound and the adjustment is recorded, so
  a challenger that kept pressing against a limit is visible as exactly that
  rather than arriving looking like it chose the boundary on merit. A parameter
  with *no* declared bound is refused outright rather than defaulted —
  otherwise the bounds only constrain the fields somebody remembered to list.
  Position sizing and risk ceilings are absent from the bounds by design: those
  are safety policy, and constraint 2 puts them out of reach of anything
  automated. A `max_size_pct` field would be the first step to it being
  fillable.

  **The trial count is pooled across the campaign**, which is the consequence
  this ADR named and the way this phase would otherwise go wrong. A
  walk-forward deflates its winner against the configurations *that run* tried.
  Run it eight times over eight challengers and report each winner's own
  deflated ratio, and every one of those numbers still answers the
  one-run question — while the search actually performed was eight times
  larger. `evaluate_campaign` pools every trial Sharpe from every challenger
  and re-deflates each result against the pooled set. Both figures are
  reported, along with the gap between them, because that gap is the size of
  the error pooling corrects. The gate reads the pooled one, and there is no
  fallback to the per-run figure when pooling cannot be computed: substituting
  it would put the overstated number in the one field that decides.

  Challengers are content-addressed, so re-proposing an identical
  configuration cannot inflate the trial count its siblings are judged against.
  A challenger that failed to evaluate contributes no trials, because it
  searched nothing — but it is reported rather than dropped.

  On a synthetic series, four one-axis perturbations produced deflated ratios
  of 0.73–0.89 against their own grids and 0.68–0.86 pooled — the correction
  is real and in the right direction — and nothing cleared 0.95. That is the
  expected outcome of most campaigns and is reported as a result, since the
  alternative is a search that always finds something.

  **Deliberately stricter than this ADR permits, and one gap it leaves.**
  Constraint 1 allows the learner to write validation artifacts. This
  implementation writes nothing at all, which is safer but means a campaign
  result is not persisted — so L4's champion/challenger comparison, which needs
  a durable record of what was proposed and when, has that to build first.
- **L4 — champion/challenger in paper. Implemented** (migration 0003,
  `lifecycle.store`, `challengers.compare`). Both running, both recorded,
  compared. Human-approved promotion only.

  The two questions this entry previously said should be settled first were
  settled as follows.

  **A proposal is persisted in `lifecycle.challenger_proposal`, not in
  `validation_artifact`.** A challenger is a proposal; an artifact is a
  measurement; promotion reads artifacts. Putting something a generator
  produced into the table the promotion gate trusts is the one place it must
  never appear. The table is append-only and stores both deflated figures —
  per-run and pooled — so a reviewer months later can see they differ, and by
  how much, without reconstructing the campaign.

  **A challenger cannot reach live at all, and the barrier is categorical.**
  `sleeve.origin` marks a roster row derived from a proposal, and
  `store.transition(..., "live")` refuses such a sleeve regardless of evidence.
  This was the concern this entry raised: once a proposal becomes a sleeve, its
  safety stops being structural and starts depending on gates it is designed to
  pass. A refusal it *cannot satisfy* restores the stronger property. The check
  reads `origin` from the row under lock rather than from the caller's `Sleeve`,
  because a snapshot is something an in-memory edit walks past — a test forges
  exactly that and still gets refused. `resolve_route` refuses a live route for
  a challenger too; the store barrier should make that unreachable, which is
  why it is worth having on the one row where being wrong costs real money.
  Neither check touches exits: a challenger that somehow holds a live position
  must still be able to close it, or a safety check becomes a trapped position.

  Clearing the barrier is `adopt_challenger`, which requires a **named human
  actor** — "system", "learner", "auto" are refused — takes a reason, and is
  recorded as a transition from the sleeve's state to itself. Nothing moved,
  but a person accepted a sleeve the system had refused, and that belongs in
  the same append-only record as every other decision. Adoption does not
  promote: it only stops the refusal, and the sleeve still has to earn live
  through the ordinary gates on its own evidence.

  **One deviation from this ADR.** It says champion and challenger are
  "distinguished by strategy version". The roster's identity constraint is
  `UNIQUE (strategy_id, symbol, account_id)` and does not include the version,
  so two such sleeves cannot coexist. Widening that constraint would permit two
  roster rows for one (strategy, symbol, account) while `store.get`/`require`
  return a single row — and the invariant that a sleeve has exactly one state
  is what every gate rests on. So a challenger runs under a derived strategy id
  (`champion@chal-abc123`) instead. The constraint stays intact, every existing
  lookup keeps working, and the pairing is visible in the journal.

  `compare` reports both sides over the same window from realised round trips
  and **declares no winner** — no `winner` field, no score. Picking one from a
  paper comparison is the step where a promotion gate gets bypassed by
  arithmetic. It flags a thin sample, mismatched spans, and a side with no
  trades, and it never mixes environments: live fills cannot enter a paper
  comparison.

  End to end on a demonstration archive: a challenger doubled the champion's
  paper return over 24 trades each, was refused live anyway, resisted an
  automated adoption attempt, and became promotable only after a named person
  adopted it — with their name on the transition.

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

**Risk, addressed at L3.** The deflated Sharpe ratio's trial count already
under-counts human iterations. A generator that produces hundreds of
challengers makes that worse unless every one is counted.
`evaluate_campaign` pools every trial from every challenger and re-deflates
each result against the pooled set, and the campaign size is capped in the
bounds — an unbounded generator does not find more good ideas, it makes all of
them statistically indefensible. The under-counting of *human* iterations
before any of this remains, and no formula fixes it.

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
