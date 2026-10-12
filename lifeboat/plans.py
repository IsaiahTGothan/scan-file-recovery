"""Plans, prices and the free trial, as the app shows them. Change them here: nothing else in the
app hard-codes a plan, a price or a feature list.

Selling isn't built yet. While ``STORE_OPEN`` is False every buy, subscribe and trial button says
"coming soon" and does nothing else: no payment details are asked for, nothing is sent anywhere.
While ``EARLY_ACCESS`` is True every feature is unlocked for everyone, whatever the plans say.

The prices below are placeholders, to be set before launch.
"""

from __future__ import annotations

from dataclasses import dataclass

STORE_OPEN = False            # True once checkout and licence keys exist (on zays.us)
EARLY_ACCESS = True           # every feature unlocked until launch
TRIAL_DAYS = 7
CURRENCY = "$"
PRICES_FINAL = False          # shown as "prices may change before launch" until True

PERIODS = (("monthly", "Monthly"), ("yearly", "Yearly"), ("lifetime", "Lifetime"))


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    tagline: str
    prices: dict[str, float]          # period -> price; a plan without a period doesn't offer it
    features: tuple[str, ...]
    trial: bool = False               # offers the free trial
    popular: bool = False             # the plan to point people to

    @property
    def free(self) -> bool:
        return not self.prices

    def price_text(self, period: str) -> tuple[str, str]:
        """(big price, small print) for a billing period."""
        if self.free:
            return f"{CURRENCY}0", "free forever"
        price = self.prices.get(period)
        if price is None:
            return "—", "not offered"
        amount = f"{CURRENCY}{price:,.2f}".replace(".00", "")
        if period == "monthly":
            return amount, "per month, cancel any time"
        if period == "yearly":
            return amount, f"per year ({CURRENCY}{price / 12:,.2f} a month)"
        return amount, "once, yours to keep"


# PLACEHOLDER prices and limits: set them before launch.
PLANS: tuple[Plan, ...] = (
    Plan("free", "Free", "See what can be saved",
         {},
         ("Scan and Deep scan any drive",
          "Deleted files, lost partitions, files found by signature",
          "Preview photos, documents and text",
          "Recover up to 1 GB",
          "Drive check and disk images")),
    Plan("pro", "Pro", "For your own drives",
         {"monthly": 19.99, "yearly": 59.99, "lifetime": 99.0},
         ("Unlimited recovery",
          "Mac drives: HFS+, APFS and encrypted volumes",
          "Split a big recovery across several drives",
          "Every copy verified, full HTML and CSV reports",
          "Email support"),
         trial=True, popular=True),
    Plan("technician", "Technician", "For repair shops and IT",
         {"monthly": 49.0, "yearly": 299.0, "lifetime": 599.0},
         ("Everything in Pro",
          "Recover your customers' drives (commercial use)",
          "Command line for scripts and batch jobs",
          "Use on up to 3 computers",
          "Priority support"),
         trial=True),
)


@dataclass(frozen=True)
class Status:
    """What this copy of Lifeboat is entitled to (shown in the header, Settings and About)."""

    name: str
    detail: str
    badge: str


def current() -> Status:
    """The plan in use. Until licences exist, everyone is on early access with everything unlocked."""
    if EARLY_ACCESS:
        return Status("Early access", "Every feature is unlocked for free until launch.", "EARLY ACCESS")
    return Status("Free", f"Scan, preview and recover up to 1 GB. Start a {TRIAL_DAYS}-day free trial of Pro "
                          "to recover everything.", "FREE")


def plan(key: str) -> Plan:
    return next(p for p in PLANS if p.key == key)


COMING_SOON = ("Free trials and subscriptions open at launch. Until then every feature is unlocked, and "
               "nothing is charged: no payment details are asked for.")
