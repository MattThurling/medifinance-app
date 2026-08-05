"""Pure pricing math, shared between the internal Quote model and the public
quote API. Matches the broker's spreadsheet — see `monthly_payment` for the
exact Excel formula. Every helper rounds at the END only; rounding earlier
amplifies the error by amount/1000 and breaks parity on large loans."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .models import RateBand


def cheapest_rate_band(*, term_months: int, amount: int | Decimal) -> RateBand | None:
    """Lowest-yield active band whose [min, max] range covers `amount` at the
    given term. Returns None if no band matches.

    Sorted by `yield_percent` ascending — for the same term, lowest yield
    means lowest rate-per-thousand means cheapest monthly payment."""
    return (
        RateBand.objects.active()
        .select_related("organisation")
        .filter(
            term_months=term_months,
            min_amount__lte=amount,
            max_amount__gte=amount,
        )
        .order_by("yield_percent")
        .first()
    )


def monthly_payment(
    *,
    principal: Decimal,
    rate_band: RateBand,
    term_months: int,
    commission_percent: Decimal | None = None,
) -> Decimal | None:
    """Monthly payment, matching the broker's spreadsheet:

        rpt    = -PMT(yield/100/12, term, 1000, 0, 1)   # full precision
        figure = rpt + (commission% / 100) · rpt         # commission on the RPT
        monthly = figure · principal / 1000              # rounded 2dp at the end

    The rate-per-thousand is used at FULL precision (Excel holds the -PMT cell
    at full precision and only displays it to 2dp). Rounding it first would
    amplify the error by principal/1000 — noticeably wrong on large loans.
    Only the final monthly is rounded, half-up like Excel. Returns None if any
    required input is missing.
    """
    if principal is None or rate_band is None or not term_months:
        return None
    rpt = rate_band.rate_per_thousand_exact()
    if rpt is None:
        return None

    comm = (commission_percent or Decimal("0")) / Decimal("100")
    figure = rpt + comm * rpt
    monthly = figure * Decimal(principal) / Decimal("1000")
    return monthly.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def implied_monthly_rate(
    *,
    principal: Decimal,
    monthly_payment: Decimal,
    term_months: int,
) -> Decimal | None:
    """The monthly rate `i` that discounts the *ordinary* annuity (payments at
    period end, Excel RATE's `type=0`) to the advance — kept at full precision;
    `apr` rounds it, `repayment_schedule` amortises with it. Decimal("0") when
    the payments don't exceed the advance (no interest). None if inputs are
    missing."""
    if monthly_payment is None or principal is None or not term_months or monthly_payment <= 0:
        return None
    n = int(term_months)
    pmt = Decimal(monthly_payment)
    advance = Decimal(principal)
    if pmt * n <= advance:           # no positive-rate solution (no interest)
        return Decimal("0")

    def pv(i: Decimal) -> Decimal:   # PV of the ordinary annuity at rate i
        return pmt * (Decimal(1) - (Decimal(1) + i) ** (-n)) / i

    # PV decreases as i rises; bracket the root and bisect.
    lo, hi = Decimal("0.000000001"), Decimal("1")
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if pv(mid) > advance else (lo, mid)
    return (lo + hi) / 2


def apr(
    *,
    principal: Decimal,
    monthly_payment: Decimal,
    term_months: int,
) -> Decimal | None:
    """Annual cost rate (%) implied by the monthly payment — matches the
    broker's spreadsheet's `RATE(term, -monthly, advance) * 12`:

    back-solve the monthly rate `i` (see `implied_monthly_rate`), then
    annualise nominally (× 12, no compounding). Returns None if inputs are
    missing.

    Note the payment is computed annuity-*due* (start of period); the
    spreadsheet still back-solves the APR as an ordinary annuity, so we
    mirror that here. Commission raises the payment, so the implied rate —
    and the APR — rise with it."""
    i = implied_monthly_rate(
        principal=principal, monthly_payment=monthly_payment, term_months=term_months,
    )
    if i is None:
        return None
    rate = i * Decimal("12") * Decimal("100")
    return rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def flat_rate(
    *,
    principal: Decimal,
    monthly_payment: Decimal,
    term_months: int,
) -> Decimal | None:
    """Annual flat interest rate (%): total interest over the term as a yearly
    percentage of the advance — (monthly·term − advance) / advance / years.
    None if inputs missing."""
    if monthly_payment is None or principal is None or not term_months or principal == 0:
        return None
    advance = Decimal(principal)
    total = Decimal(monthly_payment) * int(term_months)
    years = Decimal(term_months) / Decimal("12")
    flat = (total - advance) / advance / years * Decimal("100")
    return flat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def add_months(d: date, months: int) -> date:
    """`d` shifted forward by `months`, clamping to the last day of the target
    month (31 Jan + 1 month = 28/29 Feb)."""
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def repayment_schedule(
    *,
    principal: Decimal,
    monthly_payment: Decimal,
    term_months: int,
    first_payment_date: date,
) -> list[dict] | None:
    """Amortisation schedule — one row per month starting at
    `first_payment_date`, each a dict of `number`, `due_date`, `payment`,
    `interest`, `principal` and closing `balance`.

    Interest each period is balance × the implied monthly rate (the same rate
    the APR back-solve produces), so the split is consistent with the quoted
    APR. Amounts are rounded per row; the FINAL payment clears the remaining
    balance exactly, absorbing the pennies that per-row rounding leaves behind
    (so it can differ from the regular payment by a few pence). Returns None
    if any input is missing."""
    if first_payment_date is None:
        return None
    i = implied_monthly_rate(
        principal=principal, monthly_payment=monthly_payment, term_months=term_months,
    )
    if i is None:
        return None

    penny = Decimal("0.01")
    balance = Decimal(principal)
    rows = []
    n = int(term_months)
    for number in range(1, n + 1):
        interest = (balance * i).quantize(penny, rounding=ROUND_HALF_UP)
        payment = (balance + interest) if number == n else Decimal(monthly_payment)
        balance = balance + interest - payment
        rows.append({
            "number": number,
            "due_date": add_months(first_payment_date, number - 1),
            "payment": payment,
            "interest": interest,
            "principal": payment - interest,
            "balance": balance,
        })
    return rows


def total_interest(
    *,
    principal: Decimal,
    monthly_payment: Decimal,
    term_months: int,
) -> Decimal | None:
    """Total interest paid over the life of the loan: monthly·term − advance.
    Clamps to zero (never negative — would indicate bad inputs)."""
    if monthly_payment is None or principal is None or not term_months:
        return None
    total = Decimal(monthly_payment) * int(term_months) - Decimal(principal)
    if total < 0:
        total = Decimal("0")
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
