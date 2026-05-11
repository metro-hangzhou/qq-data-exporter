# CodeStrict_AGENTs.md

> Last updated: 2026-03-15
> Scope: third-party production review, harsh-environment design critique, and path/risk-oriented recommendations before shipping to external testers.

## Purpose

This handbook exists to review the repository as if it were being handed to a skeptical external production reviewer.

It also exists to review the repository as if it were:

- being tried by a cautious first-time user
- being supported remotely by a non-developer friend
- being evaluated by a product-minded reviewer who cares about trust, clarity, and operator confidence

It is intentionally not the same as:

- feature planning
- exporter fidelity tuning
- NapCat documentation study
- local maintainer convenience

Important scope distinction:

- release review and debug-branch review are not identical
- for the dedicated debug branch, the dominant goal is to reduce rerun count and maximize evidence per scarce remote test run
- in that branch, product polish and privacy minimization are secondary unless they directly affect test throughput or correctness

Its job is to ask harder questions:

- what breaks first under `100k+` records
- what fails noisily vs silently
- what becomes expensive only at large scale
- what is hard for remote testers to diagnose
- what is coupled in ways that will make future fixes risky
- what makes ordinary users feel safe vs afraid to touch the tool again
- what looks "technically fine" to a developer but feels brittle or hostile to a non-developer

Use this document when the task is:

- code review from a third-party or production-hardening perspective
- identifying structural risk rather than fixing one observed bug
- prioritizing what must be changed before a harsh real-world rollout
- deciding whether a current design is acceptable, fragile, or a future liability
- reviewing product shape, CLI behavior, or error UX from the perspective of actual users rather than maintainers

## Review Standard

CodeStrict review is stricter than normal repository review.

The baseline question is not:

- "does it work on the maintainer machine?"

The baseline question is:

- "would we be comfortable shipping this to a giant active group, a remote tester, and a machine we cannot inspect directly?"
- "would an ordinary user feel oriented, safe, and in control while using this?"

When there is a tradeoff, prefer:

- observability over hidden cleverness
- bounded cost over best-case speed
- explicit degradation over silent fallback
- restartable flows over all-or-nothing flows
- narrow failure domains over giant batch failure domains
- user trust over terse but scary failure modes
- input forgiveness over parser purity where the user intent is obvious
- recognizable UI labels over technically accurate but visually ambiguous output
- one dense remote-debug run over many sparse under-instrumented reruns
- for debug-branch work, evidence density over surface polish

## Reviewer Personas

Use one or more of these personas when reviewing.

### 1. Production Reliability Reviewer

Focus:

- failure domains
- timeout behavior
- retry safety
- silent degradation
- restart and resume semantics

Questions:

- does one failing page or batch poison the whole export
- do we know exactly which step failed and why
- can remote testers recover without maintainer intervention

### 2. Scale and Throughput Reviewer

Focus:

- `10k`, `100k`, and larger exports
- O(N) duplication
- memory spikes
- large batch request strategy
- repeated work across assets

Questions:

- are we constructing giant in-memory lists we do not need
- are we paying the same NapCat cost repeatedly for the same asset/bucket
- is there a hidden cliff where one batch becomes too large

### 3. Remote Support Reviewer

Focus:

- what friends/testers can report back
- whether packaged paths expose enough trace information
- whether CLI output is actionable

Questions:

- can a non-developer send back enough evidence for diagnosis
- do CLI modes have parity in logging/perf traces
- do exported manifests distinguish true QQ expiry from exporter failure

### 4. Data Integrity Reviewer

Focus:

- whether message count, ordering, and asset linkage remain trustworthy
- whether optimizations can silently change semantics

Questions:

- can pagination, sorting, or dedupe alter the selected slice
- can retries or bucket skipping misclassify still-recoverable assets
- are content and asset summaries still authoritative after trimming/profile filters

### 4b. Failure Explanation Reviewer

Focus:

- whether the exporter explains failure well enough to deserve continuing
- whether a `missing` result is actually justified
- whether contradictory or weakly-explained evidence is being normalized as routine

Questions:

- if an asset is missing, do we know why in a concrete, believable way
- are we collapsing "unknown" into a generic missing bucket
- if local path hints and actual disk state disagree, do we escalate or shrug
- would a skeptical third-party reviewer accept this missing classification as evidenced

### 5. Operability Reviewer

Focus:

- everyday maintainability
- whether design complexity is justified
- whether future contributors can safely change the code

Questions:

- is the route selection logic legible
- are fallback paths explicit and bounded
- do docs explain why a seemingly odd rule exists

### 6. Product and UX Reviewer

Focus:

- whether the CLI feels trustworthy to ordinary users
- whether errors help users recover instead of making them anxious
- whether names, targets, and states are visually understandable
- whether the tool communicates "what happens next"

Questions:

- if the user mistypes one character, do we guide them or punish them
- if a target name is blank-like or weird, can the user still identify it safely
- if a command fails, does the message suggest a recovery path
- do progress lines help a user understand that the program is working

### 7. First-Time User Anxiety Reviewer

Focus:

- the moment where a non-developer decides whether the tool feels scary
- accidental input mistakes
- unclear command syntax
- crash wording and emotional tone

Questions:

- would this output make a cautious user stop using the tool
- do we expose raw internal exceptions where a calmer explanation would work better
- are we depending too much on the user already knowing how the CLI behaves
- does the UI distinguish "you typed something slightly wrong" from "the program is broken"

## Review Categories

Every strict review should classify findings into these categories.

### Production correctness

- wrong counts
- wrong interval/tail semantics
- silent data loss
- bad missing classification

### Performance cliffs

- giant all-at-once staging
- overbroad scans
- oversized NapCat batch requests
- expensive first-hit paths repeated too often

### Failure isolation

- single bad asset causing long stalls
- one route failure disabling the wrong scope
- retries that are too broad or too quiet

### Observability

- missing trace data
- hidden phases
- packaged CLI path not emitting enough detail
- progress output that hides the true bottleneck
- insufficient route-attempt evidence to explain why a missing asset should be trusted

### Remote tester ergonomics

- vague completion output
- no clear trace file path
- manifests that do not explain missing reasons
- paths that require local maintainer intuition to debug

### Product trust and user confidence

- invisible or ambiguous target labels
- raw exceptions that sound catastrophic for recoverable mistakes
- syntax that is easy to break with one character
- state transitions that feel like the tool froze or ignored input

### Input forgiveness

- unmatched quotes crashing command handling
- numeric shortcuts bypassing helpful validation
- whitespace or unusual Unicode names behaving inconsistently
- completion and execution states disagreeing about what "Enter" should do

### Code design debt

- duplicated loops over large datasets
- route selection spread across too many places
- big mutable caches with unclear scope/lifetime
- helper names that hide production-critical behavior

### Failure explanation quality

- generic `missing_after_napcat` used where route-specific reason should exist
- contradictory path/name evidence not escalated
- timeout with no precise substep attribution
- exporter continuing after an unexplained failure that should have been treated as a diagnostic incident

## Strict Review Rules

When writing a strict review:

1. Findings come first.
2. Order them by production risk, not by code location.
3. Prefer concrete "if this scales to 100k" reasoning over abstract style complaints.
4. Explicitly separate:
   - proven issue
   - likely risk
   - open question
5. If a behavior is acceptable only because of current scale, call that out.
6. If a fix trades a little speed for bounded behavior, say so plainly.
7. Do not stop at "works for developers"; explicitly ask how this feels to a cautious non-developer.
8. For CLI/product review, treat fear-inducing or confusing UX as a real defect, not a cosmetic one.
9. Treat unexplained failure as a defect in its own right. A result is not production-acceptable just because the process completed.
10. If a missing asset is not well-explained, recommend fail-fast plus forensic capture rather than silently accepting the ambiguity.
11. For remote tester workflows with scarce reruns, prefer "collect all distinct incidents in one run" over "abort on first incident" unless early abort materially protects data integrity or safety.
12. In the dedicated debug branch, do not optimize first for ordinary-user emotional comfort; optimize first for evidence capture, rerun reduction, and correction speed.
13. In the dedicated debug branch, privacy minimization may be deferred when the tester has explicitly agreed to provide full local QQ/NapCat evidence.

## Current High-Risk Themes

As of the current repository state, the strongest CodeStrict concerns are:

- whole-export in-memory staging across messages, candidates, and asset lists even after manifest streaming landed
- one-shot bulk `hydrate_media_batch` requests with silent full-batch degradation
- one-shot bulk recent-tail plugin requests that improve speed but concentrate timeout/body-size failure domains
- observability mismatch between root REPL `/export` and packaged `app.py export-history`
- hidden performance cliffs that only appear on old buckets or stale-forward residuals
- insufficient remote-tester diagnostics when the non-REPL CLI path is used
- root REPL startup work that still pays legacy/local-discovery cost before the user has chosen a path that needs it
- route/retry policy asymmetry where `image` has a more mature second-pass strategy than `file` / `video`
- exporter behavior still treats some unknown or weakly-explained `missing` outcomes as normal completion instead of investigative failure
- strict investigative failure design must balance two goals:
  - stop pretending unknown missing is acceptable
  - avoid wasting scarce remote-test opportunities by aborting before enough distinct evidence has been collected
- terminal compatibility work that can detect risky hosts earlier than it can yet guarantee a truly simplified low-risk UI surface

These are not all confirmed bugs, but they are valid production-hardening targets.

## Current Known Examples

### Example: stale forward route timeout

A real `2000`-message export trace showed a perceived stall near `399/564`, but per-asset tracing proved:

- step `399`: fast
- step `400`: fast
- step `401`: old blank-source forwarded image
- actual delay source: plugin `/hydrate-forward-media` timeout

Meaning:

- the perceived issue was not broad local search
- the route selection was wrong for that stale forwarded asset class

CodeStrict lesson:

- always demand per-step evidence before optimizing "search"

### Example: old-image misses vs QQ expiry

Repeated manual QQ checks on sampled residual images showed many "missing" assets were actually:

- expired in QQ itself

CodeStrict lesson:

- exporter output must distinguish:
  - unresolved exporter failure
  - QQ-side expiry

Otherwise operators will chase phantom fidelity bugs.

### Example: packaged CLI observability gap

The repository originally had much better export diagnostics in root REPL `/export` than in packaged `app.py export-history`.

That meant:

- maintainers could inspect page scans and asset timing
- remote testers often could not

Current state after the first hardening pass:

- `app.py export-history` now writes perf traces
- it emits staged progress for tail scan, data write, prefetch, and materialization
- it prints the final trace path to the operator

CodeStrict lesson:

- the most common user-facing path must never be less diagnosable than the maintainer path

### Example: targeted forward hydration beats full-bundle hydration

A real forwarded-media gap on a remote friend sample showed that formal export could still miss forwarded `video` / `file` assets even after most top-level image gaps were closed.

The strict-review finding was not just "forward hydration exists, so use it more." It was:

- the old plugin `/hydrate-forward-media` behavior recursively hydrated the entire forward bundle
- exporter usually only needed one target asset
- that widened the timeout risk and made one miss more expensive than it needed to be

Current state after hardening:

- plugin forward hydration now accepts target hints
- it tries to match and hydrate only the requested asset first
- only then does it fall back to whole-bundle collection

CodeStrict lesson:

- when a fallback must stay, prefer "target first, broad fallback second" over "hydrate everything and hope"

### Example: fast bulk tail can move the bottleneck, not remove it

Bulk recent-tail history aggregation inside the fast plugin materially reduced maintainer-side `2000`-message export time, but it also changed the failure shape:

- fewer Python-to-NapCat round-trips
- bigger single request/response
- larger single-plugin work window

CodeStrict lesson:

- a speedup that collapses many small requests into one large request is also a failure-domain redesign
- performance wins still need chunk boundaries and observability, not just lower wall time

### Example: special-looking names and operator fear

A technically valid friend target can still be operationally bad if its rendered name looks blank or nearly blank.

Meaning:

- target resolution may be correct
- but the user can still be unsure whether they selected the right chat
- a later error will feel arbitrary and frightening

CodeStrict lesson:

- "correct target identity" and "recognizable target identity" are not the same requirement

## Preferred Recommendation Shapes

When proposing fixes from this handbook, favor these shapes:

- chunk or stream giant work units instead of building them all at once
- cache old-bucket or shared-asset results explicitly
- expose trace files and progress for every user-facing export path
- avoid silent fallback where practical; at least emit one scoped warning
- prefer narrow route-disable scopes over global feature disablement
- keep recent-asset behavior conservative; apply aggressive shortcuts only to clearly old buckets

Also prefer:

- not building legacy-search structures at all in `napcat_only` formal export paths
- chunk-level degradation over whole-prefetch silent fallback
- shared formatting helpers for operator-visible labels so blank-like or unsafe names are normalized consistently
- recovery-oriented CLI wording that points to the next action instead of only echoing the exception text
- lazy startup work over eager heavyweight discovery in user-facing shells
- centralized retry-policy decisions over asset-type-specific second-pass logic scattered across multiple branches
- explicit debug presets whose behavior is tuned for remote forensic density, not just normal release ergonomics

## Anti-Patterns

Treat these as CodeStrict smells:

- one giant batch request for the whole export
- building multiple full parallel representations of the same export set
- swallowing exceptions without leaving enough trace evidence
- relying on maintainer-only REPL output for performance diagnosis
- adding another fallback route without documenting its bounded scope and failure mode
- silently downgrading to a slower path that remote users cannot detect
- raw parser exceptions escaping from user input paths
- invisible or nearly invisible target labels in lists, completions, or headers
- generic `error: ...` output that leaves an anxious user unsure whether they broke the program
- "compat mode" that mostly changes messaging but does not yet materially reduce risky rendering features on hostile terminal hosts

## Relationship To Other Handbooks

- `AGENTS.md` remains the repository-wide ruleset.
- `NapCat_AGENTs.md` and children remain the NapCat truth-source handbooks.
- `TODOs.export-performance.md` remains the focused performance investigation log.
- this file is the harsh external-review lens that should feed those documents, not replace them

## Current Review Workflow

When a strict review is requested:

1. read the relevant subsystem docs first
2. inspect current code and latest live traces
3. write findings from the third-party perspective
4. convert accepted findings into:
   - subsystem AGENT updates when they change stable understanding
   - `TODOs.production-review.md` when they imply future hardening work
