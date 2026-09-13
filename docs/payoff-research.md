# Payoff research design (specified before implementation)

The existing baseline buys the executable ask, freezes an approximately 10% lower
premium stop and 20% higher premium target, sizes using fee-adjusted planned loss,
and applies its existing 240-minute hold / 120-minute settlement buffer. Its gate
optimizes an assumed terminal reward/risk ratio, not expected return or probability
of reaching the target. Spread, both fees, gaps and depth make nominal 2:1 different
from executable reward/risk. The stop cannot cap loss in a discontinuous market.

For 1–3 DTE options, gamma can produce valuable convex upside and rapid reversals.
Theta consumes time value, and a correct directional view can lose money after IV
falls. A premium-only threshold conflates these causes. Wide spreads and thin bids
can make apparent mark-price gains untradeable. Remaining time matters especially
near settlement. These are economic mechanisms, not evidence that any threshold
is optimal. Greeks and IV are diagnostics here: vendor units, accuracy, and
predictive usefulness have not been established well enough to drive new exits.

The approved hypothesis uses the same entry and initial planned monetary risk R:
activate at executable net P&L >= R, then protect estimated net break-even and trail
the best executable net P&L by R. No fixed experimental target. Stops only tighten.
Keep baseline time limits. A gap fills only at the current available bid, never at
the trigger. Stale/future/out-of-order quotes cannot update excursions, stops or
exit state. Missing full bid depth cannot produce a simulated fill.

Implementation scope: a selectable research companion, baseline_v34 by default;
astra_payoff_experimental opts into a paired baseline/experimental cohort. The
primary paper account always retains its existing baseline execution. Both research
profiles share each admitted primary entry, size, fees and subsequent ticker
observation, and continue observing independently after either exits. This isolates
conditional exit outcomes. It does NOT estimate an independent experimental
portfolio: different holding times would change cash availability, correlation
admission and subsequent opportunities. Report paired cohort results separately
from the primary ledger and never double-count research copies toward 20 trades.

R is the stored fee-adjusted planned loss at admission, held constant. Let q be
contracts times contract multiplier, A the entry debit, c(b,s) the estimated exit
fee at bid b and spot s, and p the configured bid penalty. Net liquidation P&L is
N(b)=q*max(0,b-p)-c(max(0,b-p),s)-A. After activation, high-water H and floor
F=max(0,H-R) are in account currency. Convert F to a raw-bid stop using the current
fee schedule; round upward to the contract tick and never reduce the stop.
Fees follow the existing min(notional fee, premium cap)*(1+GST) model.

Only fresh, full-size executable observations update MFE/MAE or H. A thin bid
records a deferred condition; it cannot tighten a trail. The inherited baseline
does not latch stop crossings during insufficient depth; the experiment also
avoids adding an unapproved liquidation rule. Time remains due at later quotes.
After actual settlement, record unresolved exposure until verified reconciliation;
never infer a zero-price fill.

No fitting to historical P&L. 1R activation/trail is a hypothesis. The configurable
nonnegative fixed price penalty defaults to zero to preserve baseline behavior;
run predeclared symmetric stressed scenarios separately rather than changing costs
after viewing results. Without measured latency/depth depletion, displayed bid
fills remain an approximation.

## Opt-in and measurement

Set V34_PAYOFF_PROFILE=astra_payoff_experimental only for this experimental branch
to collect BOTH research profiles. Omission selects baseline_v34. A companion
database defaults to <primary database>.payoff.sqlite; V34_PAYOFF_DATABASE can
select a dedicated path. The profile and penalty are immutable within a cohort DB.
Use a fresh primary validation ledger: legacy entries without persisted entry
observations cannot be reconstructed honestly. Retain both SQLite databases.

GET /api/payoff-research reports the selected profile, companion counts and
observation count. The events table contains ordered JSON payloads; entry rows
include the original ticker (including IV, Greeks, spread inputs and depth), size,
fees and initial levels. Observation rows record DTE, quote data, stop changes,
activation, executable fee-adjusted P&L, MFE/MAE, and actual exit trigger.
Theoretical exit price means the stop/target trigger, not the mark.
Executable exit price means displayed bid less the configured price penalty.
Estimated fees do not imply verified exchange billing. Both profiles use identical
penalties. The initial ask, size and admission risk/target gate remain inherited
from the primary baseline entry even though the experiment has no exit target.

Research states and events update in one SQLite transaction. Entry market context
is stored with the primary ledger entry, allowing idempotent companion recovery.
Do not infer missing observations during downtime. Entry journal timestamps allow
delayed attachment to be detected. The two databases are not a distributed
transaction; recovery reconciles primary entries to companion cohorts. A research
failure is surfaced in health and does not suppress a primary exit. Extra research
contracts are requested after primary exits; monitor total loop duration because
additional requests can still lengthen the next polling interval.

Record before evaluating:
- paired net P&L difference per admitted entry and in units of fixed initial R;
- activation frequency, profit surrendered from observed MFE, realized MAE,
  gap beyond trigger, holding time and exit cause;
- fees, full-size depth, spread, quote age, missing quotes, stale rejections,
  deferred exits, settlement exceptions and loop duration;
- DTE and entry/exit IV/Greeks, BTC/ETH and CALL/PUT strata as diagnostics;
- pair completion rate and all unclosed/unresolved outcomes, not only winners;
- primary-account realized P&L and conservatively marked exposure separately.

Twenty genuine primary trades are an operational validation sample, not reliable
proof of expectancy. Do not count paired copies or synthetic fixtures as trades.
Use a predeclared longer out-of-sample window and uncertainty estimates; account
for dependence across contemporaneous BTC/ETH observations. Compare costs under
predeclared symmetric stressed scenarios. Do not tune 1R on these outcomes.

Intentionally not implemented: directional changes; Greek-unit-driven theta
liquidations; fitted volatility/DTE/asymmetric thresholds; new max-hold values;
partial-fill or exchange queue models; automatic official settlement; portfolio
simulation with independent experimental admissions; or real order transport.
Expired research positions remain unresolved and require reconciliation; they
must not be treated as successful exits. Primary baseline behavior is preserved;
the companion rejects post-settlement quotes and therefore does not promise
native-baseline equivalence outside fresh, operational, pre-settlement markets.
