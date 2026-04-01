# Gas Station Pricing Simulator

A small Monte Carlo simulator for retail fuel pricing competition.

The model simulates **20 petrol stations** competing over a **14-day horizon**
under different rules for when stations are allowed to **raise** prices.
Stations can observe the current market, choose a best-response price, and try
to maximise **absolute profit**:

> **profit = margin per litre x litres sold**

That matters.  Stations do **not** optimise for margin percentage.  A station
may happily accept a lower margin if it expects to gain enough volume.

## What the Model Assumes

Each run creates a stylised market with these features:

- **20 competing stations**
- **Perfect current information** about rivals' prices
- **Best-response pricing** in a Nash-style competitive setting
- Heterogeneous stations with differences in:
  - **location attractiveness**
  - **supply cost offset**
  - **customer loyalty / captive demand**
  - **aggression**
    - low aggression = margin seeker
    - high aggression = volume seeker
- **Elastic market demand**: when the average pump price rises, the market consumes less fuel
- **Customer mix**:
  - **40% sticky customers** with lower price sensitivity
  - **60% shoppers** with higher price sensitivity
  - plus **loyalty floors** that guarantee each station some baseline share

Demand elasticity is approximately **0.85**, so lower average retail prices
create a modest but real uplift in total litres sold.

## Main Findings

Across **80 Monte Carlo runs** with tight **95% confidence intervals**, the
central result is consistent:

> **Restricting price raises compresses margins and profits.**

### Summary

- **Stable pattern**
  - `free`: **4.1% margin / EUR 409 per station-day**
  - `daily_noon`: **3.6% / EUR 360**
  - `twice_weekly`: **3.5% / EUR 347**
  - demand effect: **+1.9% volume**
- **Spike pattern**
  - `free`: **4.0% margin / EUR 394 per station-day**
  - `daily_noon`: **3.2% / EUR 319**
  - `twice_weekly`: **2.9% / EUR 290**
  - demand effect: **+2.1% volume**
- **Mixed pattern**
  - `free`: **4.1% margin / EUR 407 per station-day**
  - `daily_noon`: **3.0% / EUR 303**
  - `twice_weekly`: **2.6% / EUR 256**
  - demand effect: **+2.6% volume**

### Headline Takeaway

- The unrestricted market (`free`) produces the **highest margins and profits** in every price environment.
- Restricting price raises **reduces average margins materially**.
- The harshest regime, `twice_weekly`, can produce roughly **37% lower profit** in the `mixed` pattern versus `free`.
- Consumers buy **slightly more fuel** under the restricted regimes because average prices are lower.
- The **spike** environment is especially painful because costs can move up faster than stations are allowed to restore their preferred markup.

## Real-World Context

This model is stylised, but it is motivated by real policy regimes.  A pricing
system of this general kind has been active in Austria for years, and a similar
retail fuel pricing system is also active in Germany now.  Austria recently
moved to a **twice-weekly** system, which makes the `twice_weekly` scenario
especially interesting as a policy case.

For real-world evidence from Austria, see this study:

- [Real-world study from Austria](https://www.sciencedirect.com/science/article/abs/pii/S0140988321001122)

## Scenarios

The simulation compares three pricing-rule regimes:

- `free`: price raises allowed anytime
- `daily_noon`: once per day at 12:00 only.  Price cuts allowed anytime.
- `twice_weekly`: Monday and Thursday at 12:00 only.  Price cuts allowed anytime.

In the restricted scenarios, stations are also **forward-looking**.  If the next
allowed raise is far away, they add a **protective buffer** to today's price to
reduce the risk of getting stuck with too-low prices after a wholesale move.

In plain English, if a station knows it may be unable to raise again for many
hours, it prices a bit more defensively.

## Wholesale Price Patterns Tested

The model uses three wholesale/reference price patterns:

- `stable`: mostly unchanged wholesale/reference price
- `spike`: large temporary spike mid-period
- `mixed`: moderate volatility with occasional jumps

These patterns matter because the harm from raise restrictions depends heavily
on whether costs are calm or moving quickly.

## How to Interpret the Findings

Disclaimer: this is making some assumptions about how this makes sense.

### Why Do Raise Restrictions Reduce Profit?

Because the restriction is **asymmetric**:

- stations can **cut** prices anytime
- but cannot always **raise** them back

That creates a one-way competitive pressure.

If a station lowers its price to defend share or steal volume, it may then be
**stuck** with that lower price until the next permitted raise window.
Competitors face the same problem.  The result is a market that can still move
down quickly, but cannot move back up efficiently.

So even when stations behave rationally, the market spends more time at
**thinner markups**.

### Why Does the Protective Buffer Not Fully Solve the Problem?

Stations do try to anticipate the constraint.

Under `daily_noon` and `twice_weekly`, they add a forward-looking buffer based on:

- time until the next allowed raise
- expected market volatility

This helps, but only partially.

Why?  Because adding too much buffer makes a station overpriced versus nearby
competitors and it loses volume.  So each station faces a trade-off:

- **buffer too little** -> risk being trapped at too-low margins later
- **buffer too much** -> lose customers now

Competition limits how much precautionary pricing can be sustained.

### Why Does Total Demand Go Up When Margins Go Down?

Because the demand model is **price-elastic**.

Restricted scenarios produce lower average retail prices, and lower prices
stimulate consumption.  That effect is not huge, but it is consistent: roughly
**+2% to +3% litres sold** depending on the wholesale pattern.

So the policy trade-off in the model is:

- **lower industry margins / profits**
- **slightly lower consumer prices**
- **slightly higher fuel consumption**

### Why Is the Spike Pattern So Damaging?

The `spike` regime features a strong upward wholesale move in the middle of the
simulation.

When costs jump suddenly:

- unrestricted stations can reprice immediately
- restricted stations cannot always restore their desired markup fast enough

They are not necessarily forced into outright losses, but they are often pushed
close to the minimum viable spread for some period.  In other words, they get
caught with prices chosen for yesterday's cheaper cost environment.

That is why margins fall hardest in spiky conditions.

### Why Is the Mixed Pattern Worst for Twice-Weekly Profits?

`mixed` volatility is especially punishing for the most restrictive regime because it combines:

- repeated cost moves
- uncertainty about direction
- long gaps before the next permitted raise

That combination makes it hard to keep the right markup.  Stations either:

- underprice and get stuck with weak margins, or
- over-buffer and lose share

With only two raise windows per week, those mistakes last longer.  That is why
the `twice_weekly` regime shows the largest profit loss in the mixed-volatility
setting.

## Economic Intuition in One Paragraph

This model suggests that **limiting upward price flexibility does not simply cap
margins**.  It changes the whole competitive dynamic.  Since downward moves
remain easy but upward corrections are delayed, firms compete away margin faster
than they can rebuild it.  Consumers benefit somewhat through lower average
prices, but stations absorb the cost through lower profitability, especially
when wholesale prices are volatile.

## Important Caveats

This is a deliberately simplified model, not a literal forecast of any real country or retail chain.

Some important simplifications:

- no explicit inventory management
- no branded vs unbranded strategic differences beyond simple attractiveness/loyalty parameters
- no local geography or traffic network
- no coordination, collusion, or tacit signalling
- no multi-product economics (shop sales, car wash, loyalty apps, etc.)
- no long-run entry/exit response from stations

## Running it

Basic run:

```bash
python3 gas_price_simulator.py --progress
```

Export per-run per-station CSVs:

```bash
python3 gas_price_simulator.py --progress --csv --out results
```

Useful flags:

- `--runs 80` - Monte Carlo runs per scenario/pattern combination
- `--days 14` - simulation horizon
- `--stations 20` - number of stations
- `--seed 42` - base RNG seed

Default settings already match the reported experiment:

- **80 Monte Carlo runs**
- **14 days**
- **20 stations**
- **3 wholesale price patterns x 3 pricing scenarios**
