"""Compare expected contribution for two subscription offers before sizing a pilot."""

import argparse
from decimal import Decimal, InvalidOperation, localcontext


ONE = Decimal(1)
ZERO = Decimal(0)
PERCENT = Decimal(100)
RATE_STEPS = 10_000


def amount(raw):
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("укажите число с точкой") from exc
    if not number.is_finite() or number < 0:
        raise argparse.ArgumentTypeError("нужно конечное неотрицательное число")
    return number


def rate(raw):
    number = amount(raw)
    if number > PERCENT:
        raise argparse.ArgumentTypeError("удержание указывается в процентах от 0 до 100")
    return number / PERCENT


def contribution(months, renewal, margin, upfront=ZERO):
    # Month 1 is the first renewal after assignment. Renewal is conditional on
    # still being subscribed. All assigned subscribers are in the denominator.
    return margin * sum((renewal ** month for month in range(1, months + 1)), ZERO) - upfront


def break_even(months, control, test_margin, upfront):
    """Return the feasible renewal range, or None if parity is impossible."""
    at_zero = contribution(months, ZERO, test_margin, upfront)
    at_one = contribution(months, ONE, test_margin, upfront)
    if control > max(at_zero, at_one):
        return None
    if control <= min(at_zero, at_one):
        return ZERO, ONE
    # Search the displayed precision (0.01 p.p.), so an exact decimal
    # threshold is never rounded an extra step by a binary approximation.
    low, high = 0, RATE_STEPS
    increasing = at_one > at_zero
    while high - low > 1:
        middle = (low + high) // 2
        renewal = Decimal(middle) / RATE_STEPS
        if (contribution(months, renewal, test_margin, upfront) >= control) == increasing:
            high = middle
        else:
            low = middle
    return (Decimal(high) / RATE_STEPS, ONE) if increasing else (ZERO, Decimal(low) / RATE_STEPS)


def evaluate(months, control_price, test_price, control_renewal, control_cost=None,
             test_cost=None, test_upfront=None, test_renewal=None):
    """Return the result for a horizon starting at the first renewal after assignment."""
    if not 1 <= months <= 120:
        raise ValueError("--months должен быть от 1 до 120")
    costs_known = control_cost is not None and test_cost is not None
    if not costs_known and any(x is not None for x in (control_cost, test_cost, test_upfront)):
        raise ValueError("для расчёта вклада задайте переменные затраты для обеих групп")
    if test_renewal and test_renewal[0] > test_renewal[1]:
        raise ValueError("--test-renewal: нижняя граница выше верхней")
    with localcontext() as ctx:
        ctx.prec = 45
        control_margin = control_price - (control_cost or ZERO)
        test_margin = test_price - (test_cost or ZERO)
        upfront = test_upfront or ZERO
        control = contribution(months, control_renewal, control_margin)
        test_at_zero = contribution(months, ZERO, test_margin, upfront)
        test_at_full = contribution(months, ONE, test_margin, upfront)
        bounds = break_even(months, control, test_margin, upfront)
        differences = sorted(contribution(months, renewal, test_margin, upfront) - control
                             for renewal in test_renewal or ())
        return dict(control=control, test_at_full=test_at_full, test_max=max(test_at_zero, test_at_full),
                    bounds=bounds, differences=differences, costs_known=costs_known,
                    passes_at_full=test_at_full >= control)


def result_lines(result):
    control, test_at_full, test_max = (result[k] for k in ("control", "test_at_full", "test_max"))
    yield (f"- Контроль — {control:.2f}. Тест при 100% продления — {test_at_full:.2f}. "
           f"Максимум теста — {test_max:.2f}. Все суммы в единицах входных цен.")
    bounds = result["bounds"]
    if bounds is None:
        yield "- Паритет в этом сценарии недостижим при продлении от 0 до 100%."
    elif bounds == (ZERO, ONE):
        yield "- Паритет или превышение достигается при любом продлении от 0 до 100%."
    else:
        low, high = bounds
        threshold = low * PERCENT if high == ONE else high * PERCENT
        direction = "не ниже" if high == ONE else "не выше"
        yield f"- Для паритета нужно продление {direction} {threshold:.2f}%."
    values = result["differences"]
    if values:
        yield f"- Разница теста и контроля в заданном диапазоне — от {values[0]:+.2f} до {values[1]:+.2f}."


def evaluate_saved(terms, renewal_pct):
    parameters = {key: amount(terms[key]) if terms[key] is not None else None for key in
                  ("control_price", "test_price", "control_cost", "test_cost", "test_upfront")}
    scenarios = [rate(value) for value in terms["test_renewal"] or []]
    return evaluate(terms["months"], control_renewal=rate(renewal_pct),
                    test_renewal=scenarios, **parameters)


def main():
    parser = argparse.ArgumentParser(description="Предварительная экономика ценового пилота подписки")
    parser.add_argument("--months", type=int, required=True, help="число продлений в горизонте")
    parser.add_argument("--control-price", type=amount, required=True)
    parser.add_argument("--test-price", type=amount, required=True)
    parser.add_argument("--control-renewal", type=rate, required=True, help="базовое продление, в процентах")
    parser.add_argument("--control-cost", type=amount, help="переменные затраты на оплаченный месяц")
    parser.add_argument("--test-cost", type=amount, help="переменные затраты на оплаченный месяц")
    parser.add_argument("--test-upfront", type=amount,
                        help="дополнительные затраты на назначенного в тест пользователя")
    parser.add_argument("--test-renewal", type=rate, nargs=2, metavar=("LOW", "HIGH"),
                        help="сценарный диапазон продления, в процентах")
    args = parser.parse_args()
    try:
        result = evaluate(**vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    metric = "вклад" if result["costs_known"] else "подписочная выручка (затраты не заданы)"
    print(f"# Экономика пилота — {metric} на назначенного пользователя")
    print(f"Число продлений в горизонте — {args.months}. Вероятность продления постоянна в каждом сценарии. "
          "Первое продление происходит после назначения. Другие денежные потоки исключены.")
    print("\n".join(result_lines(result)))


if __name__ == "__main__":
    main()
