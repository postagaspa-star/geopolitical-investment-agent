# Geopolitical Investment Agent

A multi-agent system that reads geopolitical and macroeconomic events, estimates
what they do to markets, and runs a **simulated** portfolio. No real money moves
through it.

## Why it exists

I built this to answer one question: what does it actually take to get an LLM to
make repeatable decisions rather than merely plausible ones?

Ask a model to reason about a news story and it will always hand back something
convincing, even when the data underneath is stale, partial, or wrong. So most of
the work here isn't in the prompts. It's in the scaffolding around the model, and
specifically in the parts that are allowed to say no.

There are no performance figures in this repo, and that's deliberate. It's an
experiment in decision architecture. Returns from a simulator wouldn't tell you
anything honest.

## How it works

The pipeline runs in stages, and the model tier matches what each stage is worth:
a cheap model filters and structures the raw news flow, Claude handles synthesis
and the decision itself. Above that sit separate agents with distinct jobs — one
proposes candidates, one decides, one audits after the fact — plus a layer of
deterministic rules that can veto an outcome but never invent one.

Position exits are code, not judgement. A stop loss can tighten. It can never
widen. That single rule closes off the most common failure mode there is, which
is talking yourself into holding a loser.

## Stack

Python and FastAPI on the backend, React with TypeScript and Vite on the front,
Postgres via Supabase, deployed on Render. Operational alerts go out over
Telegram. Claude (Anthropic) for the expensive reasoning, a cheaper model for the
high-volume stages.

## Things worth knowing

**Corrupted data that looked like a rounding bug.** Some price series were coming
back deformed. The arithmetic was fine — the real cause was parallel fetching,
which blew past the provider's rate limit and got truncated responses back with
no error to signal it. Serializing the fetch fixed it.

**Decision paralysis.** The system worked out that never trading produces no
measurable mistakes, and drifted toward sitting in cash. Swapping the hard
confidence threshold for an expected-value gate made inaction cost something
again.

**Vendors break.** Three outages from external dependencies inside ten days: an
endpoint removed, credit exhausted, models retired by the provider. There's now
an hourly health check across every provider, because you want to hear about that
when it happens rather than days later.

**A blackout nobody noticed.** Price polling stopped without crashing — coroutines
hung with no timeout, so from the outside the job looked alive. Explicit timeouts
now, plus a single-instance lock on the periodic jobs.

## Disclaimer

Personal project, built to learn. Not financial advice and not a recommendation
to buy or sell anything.
