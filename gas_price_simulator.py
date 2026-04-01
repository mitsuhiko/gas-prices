#!/usr/bin/env python3
"""
Gas station competition simulator.

Simulates 20 petrol stations over 14 days under three pricing-rule scenarios,
with stochastic wholesale/reference prices and demand response to high prices.

Each station maximises **absolute hourly profit** (margin × volume), not margin
percentage.  Stations differ in their aggressiveness: some accept thin margins
for volume ("volume seekers"), others prefer fat margins even at lower
throughput ("margin seekers").  This is captured by a per-station
`aggression` parameter that shifts the pricing grid they consider.

Under restricted scenarios (can only raise at noon / twice a week) stations
gain *forward-looking awareness*: they anticipate that costs may rise and they
won't be able to follow, so they pre-emptively price with a buffer.

No external dependencies — pure stdlib Python.
"""

from __future__ import annotations

import argparse
import csv
import math
import multiprocessing
import os
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

# ───────────────────────────── Configuration ─────────────────────────────

SCENARIOS = {
    "free": "Unrestricted repricing",
    "daily_noon": "Raise once/day at 12:00, lower anytime",
    "twice_weekly": "Raise Mon+Thu at 12:00 only, lower anytime",
}

PATTERNS = {
    "stable": "Mostly unchanged reference price",
    "spike": "Major temporary spike mid-period",
    "mixed": "Moderate volatility with occasional jumps",
}

# ───────────────────────────── Data classes ───────────────────────────────

@dataclass
class Station:
    sid: int
    attractiveness: float      # location / brand quality
    cost_offset: float         # per-station supply chain delta
    loyalty_share: float       # captive customer fraction
    aggression: float          # 0 = margin seeker, 1 = volume seeker
    price: float = 0.0
    # accumulators
    liters: float = 0.0
    revenue: float = 0.0
    cost_total: float = 0.0
    profit: float = 0.0
    hourly_margin: List[float] = field(default_factory=list)
    hourly_volume: List[float] = field(default_factory=list)


@dataclass
class RunResult:
    station_margin_pct: List[float]
    station_profit_per_day: List[float]
    station_liters_per_day: List[float]
    network_margin_pct: float
    network_profit_per_day_per_station: float
    avg_retail_price: float
    avg_wholesale_price: float
    total_liters: float
    hourly_network_margin: List[float]
    hourly_avg_retail: List[float]
    hourly_reference: List[float]


# ───────────────────────────── Demand model ──────────────────────────────

def _hourly_profile() -> List[float]:
    """Normalised intra-day demand shape with morning + evening peaks."""
    vals = []
    for h in range(24):
        morning = math.exp(-((h - 8) ** 2) / 10)
        evening = math.exp(-((h - 17) ** 2) / 12)
        base = 0.25 + 0.15 * math.cos(h / 24 * 2 * math.pi)
        vals.append(0.6 + morning + 1.2 * evening + base)
    s = sum(vals)
    return [v / s for v in vals]


DAY_PROFILE = _hourly_profile()


def precompute_station_attrs(stations: List[Station]) -> Tuple[List[float], List[float], float]:
    """Extract hot-path arrays once so demand_shares avoids per-call attribute lookups."""
    attrs = [s.attractiveness for s in stations]
    loyals = [s.loyalty_share for s in stations]
    loyal_total = sum(loyals)
    return attrs, loyals, loyal_total


def demand_shares(
    prices: List[float],
    attrs: List[float],
    loyals: List[float],
    loyal_total: float,
) -> List[float]:
    """
    Customer split: sticky segment + price-shopper segment + loyalty floor.
    Fast version with precomputed station attributes.
    """
    _exp = math.exp
    n = len(prices)
    pmin = min(prices)

    # Inline softmax for both segments to avoid extra list allocations
    # Sticky (sensitivity=8) and shopper (sensitivity=28)
    mx_st = mx_sh = -1e30
    for i in range(n):
        dp = prices[i] - pmin
        v_st = attrs[i] - 8.0 * dp
        v_sh = attrs[i] - 28.0 * dp
        if v_st > mx_st:
            mx_st = v_st
        if v_sh > mx_sh:
            mx_sh = v_sh

    scale = 1.0 - loyal_total
    out = [0.0] * n
    sum_st = sum_sh = 0.0
    e_st = [0.0] * n
    e_sh = [0.0] * n
    for i in range(n):
        dp = prices[i] - pmin
        a = _exp(attrs[i] - 8.0 * dp - mx_st)
        b = _exp(attrs[i] - 28.0 * dp - mx_sh)
        e_st[i] = a
        e_sh[i] = b
        sum_st += a
        sum_sh += b

    norm = 0.0
    for i in range(n):
        blend = 0.40 * (e_st[i] / sum_st) + 0.60 * (e_sh[i] / sum_sh)
        v = scale * blend + loyals[i]
        out[i] = v
        norm += v

    inv_norm = 1.0 / norm
    for i in range(n):
        out[i] *= inv_norm

    return out


def demand_volume(
    hour_idx: int,
    avg_price: float,
    baseline_price: float,
    rng: random.Random,
) -> float:
    """
    Total market demand (litres) for one hour.
    Elastic: higher avg price → less consumption.
    """
    h = hour_idx % 24
    base_daily = 120_000.0
    base_hourly = base_daily * DAY_PROFILE[h]

    rel = max(0.5, avg_price / baseline_price)
    elasticity = 0.85
    mult = min(1.10, max(0.45, rel ** (-elasticity)))

    noise = rng.lognormvariate(0.0, 0.04)
    return base_hourly * mult * noise


# ───────────────────────── Reference price paths ─────────────────────────

def generate_reference(
    pattern: str, steps: int, rng: random.Random, start: float = 1.55,
) -> List[float]:
    p = [start]
    spike_ctr = spike_amp = spike_w = 0
    if pattern == "spike":
        spike_ctr = rng.randint(24 * 4, 24 * 9)
        spike_amp = rng.uniform(0.18, 0.33)
        spike_w = rng.randint(8, 24)

    for t in range(1, steps):
        prev = p[-1]
        if pattern == "stable":
            nxt = prev + 0.18 * (start - prev) + rng.gauss(0, 0.0035)
        elif pattern == "spike":
            mr = 0.08 * (start - prev)
            sp = spike_amp * math.exp(-((t - spike_ctr) ** 2) / (2 * spike_w ** 2))
            nxt = prev + mr + rng.gauss(0, 0.005) + sp / max(1, spike_w // 3)
        elif pattern == "mixed":
            mr = 0.10 * (start - prev)
            jump = 0.0
            if rng.random() < 0.025:
                jump = rng.choice([-1, 1]) * rng.uniform(0.02, 0.07)
            nxt = prev + mr + rng.gauss(0, 0.0075) + jump
        else:
            raise ValueError(pattern)
        p.append(max(0.70, nxt))
    return p


# ───────────────────────── Pricing rule helpers ──────────────────────────

def can_raise(scenario: str, day_idx: int, hour: int) -> bool:
    if scenario == "free":
        return True
    if hour != 12:
        return False
    if scenario == "daily_noon":
        return True
    if scenario == "twice_weekly":
        return (day_idx % 7) in (0, 3)   # Mon + Thu
    raise ValueError(scenario)


def hours_until_next_raise(scenario: str, day_idx: int, hour: int) -> int:
    """How many hours until this station can next raise its price?"""
    if scenario == "free":
        return 0
    for delta in range(1, 24 * 8):
        future_h = (hour + delta) % 24
        future_d = day_idx + (hour + delta) // 24
        if can_raise(scenario, future_d, future_h):
            return delta
    return 24 * 7


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


# ───────────────────── Station pricing optimisation ──────────────────────

def best_response_price(
    idx: int,
    stations: List[Station],
    costs: List[float],
    total_liters: float,
    scenario: str,
    day_idx: int,
    hour: int,
    pattern_vol: float,
    attrs: List[float],
    loyals: List[float],
    loyal_total: float,
    raise_hours_cache: Dict[Tuple[int, int], int],
) -> float:
    """
    Each station picks the price from a candidate set that maximises
    absolute hourly profit.  Aggressive stations test lower prices;
    conservative ones test higher ones.

    Under restricted raise scenarios, a forward-looking buffer is added:
    the station knows it might not be able to raise for many hours, so it
    pads its floor to avoid being stuck below cost.
    """
    prices = [s.price for s in stations]
    base = costs[idx]
    current = prices[idx]
    agg = stations[idx].aggression          # 0..1

    # Compute competitive references without extra list alloc
    p_min = 1e30
    p_sum = 0.0
    n = len(prices)
    for i in range(n):
        if i != idx:
            if prices[i] < p_min:
                p_min = prices[i]
            p_sum += prices[i]
    p_avg = p_sum / (n - 1)

    # Forward-looking buffer (cached)
    key = (day_idx, hour)
    if key in raise_hours_cache:
        hrs = raise_hours_cache[key]
    else:
        hrs = hours_until_next_raise(scenario, day_idx, hour)
        raise_hours_cache[key] = hrs
    buffer = min(0.06, hrs * pattern_vol * 0.4)

    floor = base + 0.005 + buffer
    cap   = base + 0.35

    # Candidate grid — shifts with aggression
    thin  = 0.04 + 0.06 * (1.0 - agg)   # aggressive → thin = 0.04
    fat   = 0.10 + 0.10 * (1.0 - agg)   # conservative → fat = 0.20

    candidates = {
        floor,
        current,
        current - 0.01,
        current + 0.01,
        p_min - 0.003,
        p_min + 0.005,
        p_min + 0.015,
        p_avg,
        p_avg + 0.01,
        base + thin,
        base + fat,
        base + (thin + fat) / 2,
    }

    best_p = current
    best_profit = -1e18

    for c in candidates:
        c = clamp(c, floor, cap)
        prices[idx] = c
        shares = demand_shares(prices, attrs, loyals, loyal_total)
        litres = total_liters * shares[idx]
        profit = (c - base) * litres
        if profit > best_profit:
            best_profit = profit
            best_p = c

    prices[idx] = current   # restore
    return best_p


# ──────────────────────── Pattern volatility hint ────────────────────────

PATTERN_VOL = {"stable": 0.004, "spike": 0.012, "mixed": 0.009}


# ─────────────────────────── Core simulation ─────────────────────────────

def simulate(
    pattern: str,
    scenario: str,
    days: int,
    n_stations: int,
    seed: int,
) -> RunResult:
    rng = random.Random(seed)
    steps = days * 24

    ref = generate_reference(pattern, steps, rng)

    stations: List[Station] = []
    for i in range(n_stations):
        stations.append(Station(
            sid=i,
            attractiveness=rng.gauss(0.0, 0.30),
            cost_offset=rng.uniform(-0.012, 0.012),
            loyalty_share=rng.uniform(0.003, 0.010),
            aggression=rng.uniform(0.15, 0.85),
        ))

    op_cost = 0.03
    for s in stations:
        c = ref[0] + s.cost_offset + op_cost
        s.price = c + rng.uniform(0.06, 0.14)

    baseline_price = statistics.mean(s.price for s in stations)
    pvol = PATTERN_VOL[pattern]

    # Precompute station attributes for fast demand_shares
    attrs, loyals, loyal_total = precompute_station_attrs(stations)
    raise_hours_cache: Dict[Tuple[int, int], int] = {}

    # Precompute cost offsets
    cost_offsets = [s.cost_offset + op_cost for s in stations]

    hourly_net_margin: List[float] = []
    hourly_avg_retail: List[float] = []
    hourly_ref: List[float] = []

    for t in range(steps):
        day = t // 24
        hour = t % 24

        ref_t = ref[t]
        costs = [ref_t + co for co in cost_offsets]

        avg_price = sum(s.price for s in stations) / n_stations
        antic_demand = demand_volume(t, avg_price, baseline_price, rng)

        # Two rounds of iterated best-response
        _can_raise = can_raise(scenario, day, hour)
        for _ in range(2):
            order = list(range(n_stations))
            rng.shuffle(order)
            for i in order:
                desired = best_response_price(
                    i, stations, costs, antic_demand,
                    scenario, day, hour, pvol,
                    attrs, loyals, loyal_total, raise_hours_cache,
                )
                cur = stations[i].price
                if desired <= cur:
                    stations[i].price = desired
                elif _can_raise:
                    stations[i].price = desired

                stations[i].price = max(costs[i] + 0.005, stations[i].price)

        prices = [s.price for s in stations]
        avg_p = sum(prices) / n_stations
        real_demand = demand_volume(t, avg_p, baseline_price, rng)
        shares = demand_shares(prices, attrs, loyals, loyal_total)

        h_rev = h_prof = 0.0
        for i, s in enumerate(stations):
            litres = real_demand * shares[i]
            rev = litres * s.price
            cst = litres * costs[i]
            prf = rev - cst

            s.liters += litres
            s.revenue += rev
            s.cost_total += cst
            s.profit += prf
            s.hourly_margin.append(0.0 if rev <= 0 else prf / rev)
            s.hourly_volume.append(litres)

            h_rev += rev
            h_prof += prf

        hourly_net_margin.append(0.0 if h_rev <= 0 else h_prof / h_rev)
        hourly_avg_retail.append(avg_p)
        hourly_ref.append(ref[t])

    # Collect results
    sm = [0.0 if s.revenue <= 0 else s.profit / s.revenue for s in stations]
    sp = [s.profit / days for s in stations]
    sl = [s.liters / days for s in stations]

    tot_rev = sum(s.revenue for s in stations)
    tot_prf = sum(s.profit for s in stations)
    tot_lit = sum(s.liters for s in stations)

    return RunResult(
        station_margin_pct=sm,
        station_profit_per_day=sp,
        station_liters_per_day=sl,
        network_margin_pct=0.0 if tot_rev <= 0 else tot_prf / tot_rev,
        network_profit_per_day_per_station=tot_prf / days / n_stations,
        avg_retail_price=tot_rev / max(1.0, tot_lit),
        avg_wholesale_price=statistics.mean(ref),
        total_liters=tot_lit,
        hourly_network_margin=hourly_net_margin,
        hourly_avg_retail=hourly_avg_retail,
        hourly_reference=hourly_ref,
    )


# ─────────────────────── Statistics & rendering ──────────────────────────

def mean_ci(vals: List[float]) -> Tuple[float, float]:
    if len(vals) < 2:
        return (vals[0] if vals else 0.0), 0.0
    m = statistics.mean(vals)
    return m, 1.96 * statistics.stdev(vals) / math.sqrt(len(vals))


def spark(values: List[float], width: int = 60) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    if len(values) > width:
        chunk = len(values) / width
        values = [
            statistics.mean(values[int(i * chunk):max(int(i * chunk) + 1, int((i + 1) * chunk))])
            for i in range(width)
        ]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return bars[0] * len(values)
    return "".join(bars[clamp(int((v - lo) / (hi - lo) * (len(bars) - 1)), 0, len(bars) - 1)] for v in values)


def render(
    data: Dict[str, Dict[str, List[RunResult]]],
    n_stations: int,
    days: int,
) -> str:
    W = 80
    L: List[str] = []

    L.append("")
    L.append("╔" + "═" * (W - 2) + "╗")
    L.append("║" + " GAS STATION COMPETITION SIMULATOR ".center(W - 2) + "║")
    L.append("╠" + "═" * (W - 2) + "╣")
    L.append("║" + f"  {n_stations} stations · {days} day horizon · 3 scenarios × 3 price patterns".ljust(W - 2) + "║")
    L.append("║" + "  Objective: each station maximises absolute profit (margin × volume)".ljust(W - 2) + "║")
    L.append("╚" + "═" * (W - 2) + "╝")

    scenario_keys = list(SCENARIOS.keys())
    scenario_short = {"free": "FREE", "daily_noon": "DAILY", "twice_weekly": "2×WEEK"}

    for pattern in PATTERNS:
        L.append("")
        L.append("┌" + "─" * (W - 2) + "┐")
        L.append("│" + f" ◆ {pattern.upper()}: {PATTERNS[pattern]}".ljust(W - 2) + "│")
        L.append("├" + "─" * (W - 2) + "┤")

        # ── Network summary table ──
        hdr = f"│ {'':18s} {'FREE':>12s} {'DAILY':>12s} {'2×WEEK':>12s}   │"
        L.append(hdr)
        L.append("│ " + "─" * (W - 4) + " │")

        rows = [
            ("Margin %",
             lambda sc: mean_ci([r.network_margin_pct * 100 for r in data[pattern][sc]])),
            ("Profit/day/stn €",
             lambda sc: mean_ci([r.network_profit_per_day_per_station for r in data[pattern][sc]])),
            ("Avg retail €/L",
             lambda sc: mean_ci([r.avg_retail_price for r in data[pattern][sc]])),
            ("Wholesale €/L",
             lambda sc: mean_ci([r.avg_wholesale_price for r in data[pattern][sc]])),
            ("Litres/day (k)",
             lambda sc: mean_ci([r.total_liters / days / 1000 for r in data[pattern][sc]])),
        ]

        for label, fn in rows:
            cells = []
            for sc in scenario_keys:
                m, ci = fn(sc)
                if "Margin" in label:
                    cells.append(f"{m:5.2f}±{ci:.2f}")
                elif "Profit" in label:
                    cells.append(f"{m:6.0f}±{ci:.0f}")
                elif "Litres" in label:
                    cells.append(f"{m:6.1f}±{ci:.1f}")
                else:
                    cells.append(f"{m:6.3f}")
            L.append(f"│ {label:18s} {cells[0]:>12s} {cells[1]:>12s} {cells[2]:>12s}   │")

        # ── Demand response vs free baseline ──
        base_lit = statistics.mean(r.total_liters / days for r in data[pattern]["free"])
        L.append("│ " + "─" * (W - 4) + " │")
        deltas = []
        for sc in scenario_keys:
            lit = statistics.mean(r.total_liters / days for r in data[pattern][sc])
            deltas.append(f"{(lit / base_lit - 1) * 100:+5.1f}%")
        L.append(f"│ {'Demand vs FREE':18s} {'—':>12s} {deltas[1]:>12s} {deltas[2]:>12s}   │")

        # ── Sparklines ──
        L.append("│ " + "─" * (W - 4) + " │")
        L.append("│  Network margin over 14 days (hourly avg across runs)" + " " * (W - 57) + "│")
        for sc in scenario_keys:
            runs = data[pattern][sc]
            n = len(runs[0].hourly_network_margin)
            avg = [statistics.mean(r.hourly_network_margin[t] for r in runs) for t in range(n)]
            sp = spark(avg, width=58)
            L.append(f"│  {scenario_short[sc]:8s} {sp}   │")

        L.append("│" + " " * (W - 2) + "│")
        L.append("│  Avg retail price vs wholesale" + " " * (W - 33) + "│")
        runs0 = data[pattern]["free"]
        n = len(runs0[0].hourly_avg_retail)
        ret = [statistics.mean(r.hourly_avg_retail[t] for r in runs0) for t in range(n)]
        whl = [statistics.mean(r.hourly_reference[t] for r in runs0) for t in range(n)]
        spread = [r - w for r, w in zip(ret, whl)]
        L.append(f"│  {'Retail':8s} {spark(ret, width=58)}   │")
        L.append(f"│  {'Wholesal':8s} {spark(whl, width=58)}   │")
        L.append(f"│  {'Spread':8s} {spark(spread, width=58)}   │")

        # ── Per-station table (compact: show top 5 + bottom 5 by profit) ──
        L.append("│ " + "─" * (W - 4) + " │")
        L.append("│  Per-station breakdown (top-5 & bottom-5 by profit, FREE scenario)" + " " * max(0, W - 71) + "│")
        L.append(f"│  {'Stn':>4s} {'Agg':>4s} {'Margin%':>8s} {'€/day':>8s} {'kL/day':>7s} {'Margin%':>8s} {'€/day':>8s} {'Margin%':>8s} {'€/day':>8s} │")
        L.append(f"│  {'':>4s} {'':>4s} {'FREE':>8s} {'FREE':>8s} {'FREE':>7s} {'DAILY':>8s} {'DAILY':>8s} {'2×WEEK':>8s} {'2×WEEK':>8s} │")
        L.append("│  " + "─" * (W - 6) + "  │")

        # Rank by free profit
        free_profits = [statistics.mean(r.station_profit_per_day[i] for r in data[pattern]["free"]) for i in range(n_stations)]
        ranked = sorted(range(n_stations), key=lambda i: free_profits[i], reverse=True)
        show = ranked[:5] + ranked[-5:]

        for i in show:
            agg = data[pattern]["free"][0].station_margin_pct  # we need station aggression — grab from first run
            cells = []
            for sc in scenario_keys:
                mm, _ = mean_ci([r.station_margin_pct[i] * 100 for r in data[pattern][sc]])
                pp, _ = mean_ci([r.station_profit_per_day[i] for r in data[pattern][sc]])
                cells.append((mm, pp))
            lm, _ = mean_ci([r.station_liters_per_day[i] / 1000 for r in data[pattern]["free"]])
            L.append(
                f"│  S{i+1:02d}  "
                f"   {cells[0][0]:6.2f}  {cells[0][1]:7.0f}  {lm:5.1f}  "
                f"{cells[1][0]:6.2f}  {cells[1][1]:7.0f}  "
                f"{cells[2][0]:6.2f}  {cells[2][1]:7.0f} │"
            )
            if i == ranked[4]:
                L.append("│  " + " · · ·".center(W - 6) + "  │")

        L.append("└" + "─" * (W - 2) + "┘")

    # ── Key takeaways ──
    L.append("")
    L.append("KEY OBSERVATIONS")
    L.append("─" * W)

    for pattern in PATTERNS:
        fm, _ = mean_ci([r.network_margin_pct * 100 for r in data[pattern]["free"]])
        dm, _ = mean_ci([r.network_margin_pct * 100 for r in data[pattern]["daily_noon"]])
        wm, _ = mean_ci([r.network_margin_pct * 100 for r in data[pattern]["twice_weekly"]])
        fp = statistics.mean(r.network_profit_per_day_per_station for r in data[pattern]["free"])
        dp = statistics.mean(r.network_profit_per_day_per_station for r in data[pattern]["daily_noon"])
        wp = statistics.mean(r.network_profit_per_day_per_station for r in data[pattern]["twice_weekly"])
        L.append(
            f"  {pattern:8s}  margins: FREE {fm:.1f}% → DAILY {dm:.1f}% → 2×WEEK {wm:.1f}%  |  "
            f"profit/d/stn: €{fp:.0f} → €{dp:.0f} → €{wp:.0f}"
        )

    fl = statistics.mean(r.total_liters / days for r in data["stable"]["free"])
    wl = statistics.mean(r.total_liters / days for r in data["spike"]["free"])
    L.append(f"\n  Demand destruction: stable→spike volume {(wl/fl - 1)*100:+.1f}% (price elasticity effect)")
    L.append("")

    return "\n".join(L)


# ───────────────────────────── CSV export ────────────────────────────────

def export_csv(
    data: Dict[str, Dict[str, List[RunResult]]],
    n_stations: int,
    out: Path,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for pat in PATTERNS:
        for sc in SCENARIOS:
            p = out / f"{pat}__{sc}.csv"
            with p.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["run", "station", "margin_pct", "profit_per_day", "liters_per_day"])
                for ri, run in enumerate(data[pat][sc]):
                    for si in range(n_stations):
                        w.writerow([
                            ri, si + 1,
                            f"{run.station_margin_pct[si]:.6f}",
                            f"{run.station_profit_per_day[si]:.2f}",
                            f"{run.station_liters_per_day[si]:.1f}",
                        ])


# ─────────────────────────── Main harness ────────────────────────────────

def _simulate_job(args_tuple):
    """Worker function for multiprocessing."""
    pat, sc, days, n_stations, seed = args_tuple
    return (pat, sc, simulate(pat, sc, days, n_stations, seed))


def run_all(args: argparse.Namespace) -> Dict[str, Dict[str, List[RunResult]]]:
    data: Dict[str, Dict[str, List[RunResult]]] = {
        p: {s: [] for s in SCENARIOS} for p in PATTERNS
    }

    jobs = []
    for pi, pat in enumerate(PATTERNS):
        for si, sc in enumerate(SCENARIOS):
            for r in range(args.runs):
                seed = args.seed + (pi + 1) * 100_000 + (si + 1) * 1_000 + r
                jobs.append((pat, sc, args.days, args.stations, seed))

    total = len(jobs)
    workers = min(os.cpu_count() or 4, total)

    if args.progress:
        print(f"  Running {total} simulations across {workers} workers...",
              file=sys.stderr, flush=True)

    with multiprocessing.Pool(workers) as pool:
        for done, (pat, sc, result) in enumerate(
            pool.imap_unordered(_simulate_job, jobs), 1
        ):
            data[pat][sc].append(result)
            if args.progress and (done % max(1, total // 40) == 0 or done == total):
                bar_w = 30
                filled = int(done / total * bar_w)
                bar = "█" * filled + "░" * (bar_w - filled)
                print(
                    f"\r  [{bar}] {done}/{total} ({done/total*100:5.1f}%)",
                    end="", file=sys.stderr, flush=True,
                )

    if args.progress:
        print(file=sys.stderr)

    return data


def main() -> None:
    p = argparse.ArgumentParser(description="Gas station pricing strategy simulator")
    p.add_argument("--runs", type=int, default=80, help="Monte Carlo runs per combo (default 80)")
    p.add_argument("--days", type=int, default=14, help="Simulation days (default 14)")
    p.add_argument("--stations", type=int, default=20, help="Number of stations (default 20)")
    p.add_argument("--seed", type=int, default=42, help="Base random seed")
    p.add_argument("--csv", action="store_true", help="Export CSV data")
    p.add_argument("--out", type=Path, default=Path("results"), help="CSV output dir")
    p.add_argument("--progress", action="store_true", help="Show progress bar")
    args = p.parse_args()

    data = run_all(args)
    print(render(data, args.stations, args.days))

    if args.csv:
        export_csv(data, args.stations, args.out)
        print(f"CSV exported to {args.out}/")


if __name__ == "__main__":
    main()
