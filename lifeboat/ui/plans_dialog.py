"""Plans, the free trial and subscriptions: shown now, sold at launch.

What the plans are and cost lives in ``lifeboat/plans.py``. Until the store opens, every trial,
subscribe and buy button here only says "coming soon": nothing asks for payment details and
nothing is sent anywhere.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import plans
from . import icons
from .theme import current
from .widgets import pill

log = logging.getLogger("lifeboat.ui")

LICENSE_RESULT = 2      # PlansDialog closed with "Enter a license key": open Settings > Account


class _PlanCard(QFrame):
    """One plan: name, price for the chosen period, what it includes, and its buttons."""

    chosen = Signal(str, str)        # plan key, "trial" / "subscribe" / "buy"

    def __init__(self, plan: plans.Plan) -> None:
        super().__init__()
        self.plan = plan
        self.period = "yearly"
        self.setObjectName("PlanCard")
        self.setProperty("popular", plan.popular)
        t = current()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(6)
        top = QHBoxLayout()
        name = QLabel(plan.name)
        name.setObjectName("PlanName")
        top.addWidget(name)
        top.addStretch(1)
        if plan.popular:
            top.addWidget(pill("MOST POPULAR", "PopularPill"))
        layout.addLayout(top)
        tagline = QLabel(plan.tagline)
        tagline.setProperty("muted", True)
        layout.addWidget(tagline)
        layout.addSpacing(8)
        self.price = QLabel()
        self.price.setObjectName("PlanPrice")
        self.small = QLabel()
        self.small.setProperty("muted", True)
        layout.addWidget(self.price)
        layout.addWidget(self.small)
        layout.addSpacing(10)
        for feature in plan.features:
            row = QHBoxLayout()
            row.setSpacing(8)
            tick = QLabel()
            tick.setPixmap(icons.pixmap("check", 16, t.accent if plan.popular else t.ok))
            tick.setAlignment(Qt.AlignmentFlag.AlignTop)
            text = QLabel(feature)
            text.setWordWrap(True)
            row.addWidget(tick, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            layout.addLayout(row)
        layout.addStretch(1)
        layout.addSpacing(12)
        self.trial: QPushButton | None = None
        self.subscribe: QPushButton | None = None
        if plan.free:
            included = QPushButton("Free forever")
            included.setEnabled(False)
            included.setToolTip("Scanning, previews and small recoveries stay free after launch.")
            included.setMinimumHeight(38)
            layout.addWidget(included)
        else:
            if plan.trial:
                self.trial = QPushButton(f"Start {plans.TRIAL_DAYS}-day free trial")
                icons.put(self.trial, "gift", "white" if plan.popular else "")
                self.trial.setProperty("primary", plan.popular)
                self.trial.setMinimumHeight(38)
                self.trial.clicked.connect(self._trial)
                layout.addWidget(self.trial)
            self.subscribe = QPushButton()
            self.subscribe.setMinimumHeight(38)
            self.subscribe.clicked.connect(self._subscribe)
            layout.addWidget(self.subscribe)
        self.set_period(self.period)

    def set_period(self, period: str) -> None:
        self.period = period
        big, small = self.plan.price_text(period)
        self.price.setText(big)
        self.small.setText(small)
        if self.subscribe is not None:
            offered = period in self.plan.prices
            self.subscribe.setEnabled(offered)
            self.subscribe.setText(("Buy once" if period == "lifetime" else "Subscribe") if offered else "Not offered")

    def _trial(self) -> None:
        self.chosen.emit(self.plan.key, "trial")

    def _subscribe(self) -> None:
        self.chosen.emit(self.plan.key, "buy" if self.period == "lifetime" else "subscribe")


class PlansDialog(QDialog):
    """The plans side by side, with Monthly / Yearly / Lifetime prices."""

    LICENSE = LICENSE_RESULT

    def __init__(self, parent: QWidget | None, period: str = "yearly") -> None:
        super().__init__(parent)
        self.setWindowTitle("Lifeboat plans")
        self.setMinimumSize(1000, 660)
        self.last_notice = ""
        status = plans.current()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(56))
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = QLabel("Choose your plan")
        title.setObjectName("BigTitle")
        sub = QLabel(f"<b>{status.name}:</b> {status.detail} Plans and the free trial start at launch.")
        sub.setWordWrap(True)
        sub.setProperty("muted", True)
        texts.addWidget(title)
        texts.addWidget(sub)
        head.addLayout(texts, 1)
        head.addWidget(pill(status.badge), 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        switch = QFrame()
        switch.setObjectName("Segmented")
        row = QHBoxLayout(switch)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        self.periods: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, label in plans.PERIODS:
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setProperty("period", key)
            button.toggled.connect(self._period_toggled)
            group.addButton(button)
            row.addWidget(button)
            self.periods[key] = button
        centred = QHBoxLayout()
        centred.addStretch(1)
        centred.addWidget(switch)
        centred.addStretch(1)
        layout.addLayout(centred)

        cards = QHBoxLayout()
        cards.setSpacing(16)
        self.cards: dict[str, _PlanCard] = {}
        for plan in plans.PLANS:
            card = _PlanCard(plan)
            card.chosen.connect(self._chosen)
            cards.addWidget(card, 1)
            self.cards[plan.key] = card
        layout.addLayout(cards, 1)

        self.notice = QFrame()
        self.notice.setObjectName("Notice")
        line = QHBoxLayout(self.notice)
        line.setContentsMargins(14, 10, 14, 10)
        line.setSpacing(10)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("sparkle", 22, current().info))
        line.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        self.notice_text = QLabel()
        self.notice_text.setWordWrap(True)
        self.notice_text.setTextFormat(Qt.TextFormat.RichText)
        line.addWidget(self.notice_text, 1)
        self.notice.setVisible(False)
        layout.addWidget(self.notice)

        foot = QHBoxLayout()
        license_link = QPushButton("Have a license key?")
        license_link.setObjectName("LinkButton")
        icons.put(license_link, "key", "info")
        license_link.setCursor(Qt.CursorShape.PointingHandCursor)
        license_link.clicked.connect(self._license)
        fine = QLabel("Prices in US dollars" + ("." if plans.PRICES_FINAL else "; they may change before launch."))
        fine.setProperty("muted", True)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        foot.addWidget(license_link)
        foot.addSpacing(12)
        foot.addWidget(fine)
        foot.addStretch(1)
        foot.addWidget(close)
        layout.addLayout(foot)
        self.periods.get(period, self.periods["yearly"]).setChecked(True)

    def _period_toggled(self, on: bool) -> None:
        if not on:
            return
        period = next(k for k, b in self.periods.items() if b.isChecked())
        for card in self.cards.values():
            card.set_period(period)

    def _chosen(self, key: str, what: str) -> None:
        plan = plans.plan(key)
        action = {"trial": f"the {plans.TRIAL_DAYS}-day free trial of {plan.name}",
                  "subscribe": f"{plan.name} subscriptions",
                  "buy": f"{plan.name} for life"}.get(what, plan.name)
        log.info("Plans: %s chosen (%s) while the store is closed", plan.name, what)
        self.show_notice(f"<b>Coming soon: {action}.</b> {plans.COMING_SOON}")

    def _license(self) -> None:
        self.done(self.LICENSE)

    def show_notice(self, html: str) -> None:
        self.last_notice = html
        self.notice_text.setText(html)
        self.notice.setVisible(True)


__all__ = ["LICENSE_RESULT", "PlansDialog"]
