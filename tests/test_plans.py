"""The plans shown in the app (lifeboat/plans.py): consistent, and nothing sold before launch."""

from __future__ import annotations

from lifeboat import plans


def test_plans_are_consistent():
    keys = [p.key for p in plans.PLANS]
    assert len(keys) == len(set(keys)) and keys[0] == "free" and plans.plan("free").free
    assert sum(p.popular for p in plans.PLANS) == 1                # one plan to point people to
    periods = {key for key, _label in plans.PERIODS}
    for plan in plans.PLANS:
        assert plan.name and plan.tagline and plan.features
        assert set(plan.prices) <= periods
        assert all(price > 0 for price in plan.prices.values())
        assert plan.free or plan.prices.get("monthly", 0) < plan.prices.get("yearly", 1e9)


def test_price_text():
    pro = plans.plan("pro")
    assert pro.price_text("monthly") == (f"{plans.CURRENCY}19.99", "per month, cancel any time")
    big, small = pro.price_text("yearly")
    assert big == f"{plans.CURRENCY}59.99" and small == f"per year ({plans.CURRENCY}5.00 a month)"
    assert pro.price_text("lifetime") == (f"{plans.CURRENCY}99", "once, yours to keep")
    assert plans.plan("free").price_text("yearly") == (f"{plans.CURRENCY}0", "free forever")
    custom = plans.Plan("x", "X", "", {"monthly": 5.0}, ("a",))
    assert custom.price_text("lifetime") == ("—", "not offered")


def test_nothing_is_sold_before_launch():
    """Until the store opens, the app says so: early access with everything unlocked."""
    if plans.STORE_OPEN:
        return
    status = plans.current()
    assert plans.EARLY_ACCESS and status.badge == "EARLY ACCESS" and "unlocked" in status.detail
    assert "no payment details" in plans.COMING_SOON
