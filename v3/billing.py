import os
"""v3 billing policy. Money itself lives in v3.store.

Policy, carried over from v2: prepaid BDT wallets, ৳0.50 per done ID,
deducted only when actually submitted. Failed IDs are never charged.
Admin is unlimited; bypass ids are exempt.
"""

import v3.config as config

BKASH_NAGAD_NUMBER = os.getenv("V3_BKASH_NAGAD_NUMBER", "01XXXXXXXXX")
BINANCE_PAY_ID = os.getenv("V3_BINANCE_PAY_ID", "0000000000")

KIND_LABELS = {
    "topup": "Top-up",
    "deduct": "ID submitted",
    "adjust": "Correction",
    "reserve": "Reserved",
    "refund": "Refunded",
}


def kind_label(kind):
    return KIND_LABELS.get(kind, kind)


def billable(owner):
    """True when the owner pays per ID. Admin and bypass ids do not."""
    if owner == "admin":
        return False
    if owner.startswith("user:"):
        try:
            if int(owner.split(":", 1)[1]) in config.BILLING_BYPASS_IDS:
                return False
        except ValueError:
            pass
    return True


def format_bdt(poisha):
    sign = "-" if poisha < 0 else ""
    return f"{sign}৳{abs(poisha) / 100:.2f}"


def ids_left(balance):
    return balance // config.COST_PER_ROW_POISHA


def topup_instructions(user_id):
    return (
        "Top up to submit IDs:\n"
        f"bKash / Nagad (Send Money): {BKASH_NAGAD_NUMBER}\n"
        f"USDT: Binance Pay → Pay ID {BINANCE_PAY_ID}\n"
        "Minimum top-up: $1 (≈120–125 BDT, as per current rate). "
        "Send your TrxID/TxID + phone + Telegram ID to the admin; "
        "the admin activates within 1–5 minutes.\n"
        f"Your Telegram ID: {user_id}"
    )


def buy_subscription_text(user_id):
    return (
        "💳 Buy Subscription\n\n"
        "Every submitted ID costs ৳0.50, deducted only when an ID is "
        "actually submitted — failed IDs are never charged.\n"
        f"bKash / Nagad (Send Money): {BKASH_NAGAD_NUMBER}\n"
        f"USDT: Binance Pay → Pay ID {BINANCE_PAY_ID}\n"
        "Minimum top-up: $1 (≈120–125 BDT, as per current rate). "
        "Send your TrxID/TxID + phone + Telegram ID to the admin; "
        "the admin activates within 1–5 minutes.\n"
        f"Your Telegram ID: {user_id}\n\n"
        "The Worker Board then opens automatically."
    )


def gate_text():
    return (
        "🔒 An active subscription is required to use the bot.\n\n"
        "Every submitted ID costs ৳0.50, deducted only when an ID is "
        "actually submitted — failed IDs are never charged. Top up "
        "through the admin to keep running."
    )

