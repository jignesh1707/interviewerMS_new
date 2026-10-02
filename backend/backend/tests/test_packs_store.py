"""The pack ledger: purchased minutes with an expiry, stacking, idempotent purchases, debits, refunds."""

import threading

import pytest

from app.services.storage import Store

DAY = 86400
T0 = 1_800_000_000  # an arbitrary "purchase" moment, in epoch seconds
KEY = ("t1", "stu-1", "economy")


@pytest.fixture()
def store(tmp_path):
    store = Store(database_path=tmp_path / "packs.db")
    yield store
    store.close()


def activate(store, payment_id="pay-1", minutes=150, days=30, at=T0, key=KEY):
    return store.pack_activate(*key, payment_id=payment_id, minutes=minutes, days=days, purchased_at=at)


def wallet(store, key=KEY):
    return store.pack_get(*key)


# ----------------------------------------------------------------------------- activation


def test_no_pack_means_no_wallet(store):
    assert wallet(store) is None


def test_activation_creates_a_wallet_with_minutes_and_an_expiry(store):
    assert activate(store) is True
    row = wallet(store)
    assert row["minutes_total"] == 150
    assert row["minutes_used"] == 0
    assert row["interviews_started"] == 0
    assert row["expires_at"] == T0 + 30 * DAY


def test_the_same_payment_is_applied_only_once(store):
    assert activate(store, payment_id="evt_1") is True
    assert activate(store, payment_id="evt_1") is False  # a replayed Stripe event
    assert wallet(store)["minutes_total"] == 150


def test_concurrent_replays_of_one_payment_apply_once(store):
    results = []
    lock = threading.Lock()

    def go():
        ok = activate(store, payment_id="evt_race")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=go) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert wallet(store)["minutes_total"] == 150


def test_buying_again_while_active_stacks_minutes_and_extends_expiry(store):
    activate(store, payment_id="a", at=T0)
    store.pack_debit(*KEY, 15, now=T0 + 5 * DAY)
    activate(store, payment_id="b", at=T0 + 10 * DAY)
    row = wallet(store)
    assert row["minutes_total"] == 300
    assert row["minutes_used"] == 15  # usage carries over
    assert row["expires_at"] == T0 + 60 * DAY  # old expiry plus another 30 days, no days are lost


def test_buying_after_expiry_starts_a_fresh_pack(store):
    activate(store, payment_id="a", at=T0)
    store.pack_debit(*KEY, 15, now=T0 + DAY)
    activate(store, payment_id="b", at=T0 + 40 * DAY)  # the first pack expired on day 30
    row = wallet(store)
    assert row["minutes_total"] == 150  # leftover minutes were forfeited
    assert row["minutes_used"] == 0
    assert row["interviews_started"] == 0
    assert row["expires_at"] == T0 + 70 * DAY


def test_buying_after_using_everything_starts_a_fresh_pack(store):
    activate(store, payment_id="a", at=T0)
    for _ in range(10):
        assert store.pack_debit(*KEY, 15, now=T0 + DAY)
    activate(store, payment_id="b", at=T0 + 5 * DAY)  # all minutes gone, though the old pack had days left
    row = wallet(store)
    assert row["minutes_total"] == 150 and row["minutes_used"] == 0
    assert row["expires_at"] == T0 + 35 * DAY  # counted from this purchase, not stacked


def test_plans_students_and_tenants_have_separate_wallets(store):
    activate(store)
    assert wallet(store, ("t1", "stu-1", "premium")) is None
    assert wallet(store, ("t1", "stu-2", "economy")) is None
    assert wallet(store, ("t2", "stu-1", "economy")) is None
    activate(store, payment_id="p2", minutes=250, key=("t1", "stu-1", "premium"))
    assert wallet(store, ("t1", "stu-1", "premium"))["minutes_total"] == 250
    assert wallet(store)["minutes_total"] == 150


# ----------------------------------------------------------------------------- debit and credit


def test_debit_succeeds_until_the_minutes_run_out(store):
    activate(store)
    for _ in range(10):  # 10 x 15 = 150
        assert store.pack_debit(*KEY, 15, now=T0 + DAY) is True
    assert store.pack_debit(*KEY, 15, now=T0 + DAY) is False
    row = wallet(store)
    assert row["minutes_used"] == 150
    assert row["interviews_started"] == 10


def test_a_failed_debit_changes_nothing(store):
    activate(store)
    assert store.pack_debit(*KEY, 140, now=T0 + DAY) is True
    assert store.pack_debit(*KEY, 15, now=T0 + DAY) is False
    row = wallet(store)
    assert row["minutes_used"] == 140 and row["interviews_started"] == 1
    assert store.pack_debit(*KEY, 10, now=T0 + DAY) is True  # exactly what is left


def test_no_pack_cannot_be_debited(store):
    assert store.pack_debit(*KEY, 15, now=T0) is False


def test_an_expired_pack_cannot_be_debited(store):
    activate(store)
    assert store.pack_debit(*KEY, 15, now=T0 + 30 * DAY - 1) is True
    assert store.pack_debit(*KEY, 15, now=T0 + 30 * DAY) is False  # the expiry instant itself is too late


def test_credit_undoes_a_debit_and_never_goes_negative(store):
    activate(store)
    store.pack_debit(*KEY, 15, now=T0)
    store.pack_credit(*KEY, 15)
    row = wallet(store)
    assert row["minutes_used"] == 0 and row["interviews_started"] == 0
    store.pack_credit(*KEY, 15)  # a double refund must not create free minutes
    row = wallet(store)
    assert row["minutes_used"] == 0 and row["interviews_started"] == 0


def test_concurrent_bookings_cannot_overspend(store):
    activate(store)
    results = []
    lock = threading.Lock()

    def book():
        ok = store.pack_debit(*KEY, 15, now=T0 + DAY)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=book) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 10
    assert wallet(store)["minutes_used"] == 150


# ----------------------------------------------------------------------------- refunds

RULE = dict(max_interviews_started=1, max_minutes_used=15, rule="any")


def revoke(store, payment_id="pay-1", key=KEY, **rule):
    return store.pack_revoke(*key, payment_id=payment_id, **(rule or RULE))


def test_unused_pack_can_be_refunded(store):
    activate(store)
    assert revoke(store) == "revoked"
    row = wallet(store)
    assert row["minutes_total"] == 0
    assert row["expires_at"] == T0  # the 30 days came off again
    assert store.pack_debit(*KEY, 15, now=T0 - 1) is False


def test_refund_is_allowed_while_only_the_first_interview_has_started(store):
    activate(store)
    store.pack_debit(*KEY, 25, now=T0 + DAY)  # one 25 minute interview: over 15 minutes, but only one interview
    assert revoke(store) == "revoked"


def test_refund_is_allowed_up_to_15_minutes_used(store):
    activate(store)
    store.pack_debit(*KEY, 15, now=T0 + DAY)
    assert revoke(store, max_interviews_started=0, max_minutes_used=15, rule="any") == "revoked"


def test_refund_is_refused_once_a_second_interview_has_started_and_over_15_minutes_used(store):
    activate(store)
    store.pack_debit(*KEY, 15, now=T0 + DAY)
    store.pack_debit(*KEY, 15, now=T0 + 2 * DAY)
    assert revoke(store) == "not_eligible"
    row = wallet(store)
    assert row["minutes_total"] == 150 and row["expires_at"] == T0 + 30 * DAY  # untouched


def test_rule_all_needs_both_conditions(store):
    activate(store)
    store.pack_debit(*KEY, 25, now=T0 + DAY)  # one interview, but 25 > 15 minutes
    assert revoke(store, max_interviews_started=1, max_minutes_used=15, rule="all") == "not_eligible"
    store.pack_credit(*KEY, 25)
    store.pack_debit(*KEY, 15, now=T0 + DAY)
    assert revoke(store, max_interviews_started=1, max_minutes_used=15, rule="all") == "revoked"


def test_refunding_twice_does_nothing_the_second_time(store):
    activate(store)
    assert revoke(store) == "revoked"
    assert revoke(store) == "already_revoked"
    assert wallet(store)["minutes_total"] == 0


def test_refund_of_an_unknown_payment_or_another_students_payment(store):
    activate(store)
    assert revoke(store, payment_id="nope") == "not_found"
    assert revoke(store, key=("t1", "stu-2", "economy")) == "not_found"
    assert revoke(store, key=("t1", "stu-1", "premium")) == "not_found"
    assert revoke(store, key=("t2", "stu-1", "economy")) == "not_found"
    assert wallet(store)["minutes_total"] == 150


def test_refunding_one_of_two_stacked_payments_removes_only_its_share(store):
    activate(store, payment_id="a", at=T0)
    activate(store, payment_id="b", at=T0 + DAY)  # stacked: 300 minutes, expiry T0 + 60 days
    assert revoke(store, payment_id="b") == "revoked"
    row = wallet(store)
    assert row["minutes_total"] == 150
    assert row["expires_at"] == T0 + 30 * DAY


def test_refund_never_removes_minutes_that_were_already_used(store):
    activate(store)
    store.pack_debit(*KEY, 25, now=T0 + DAY)
    assert revoke(store) == "revoked"
    row = wallet(store)
    assert row["minutes_total"] == 25  # the used minutes stay on the books; nothing is left to spend
    assert row["minutes_total"] - row["minutes_used"] == 0
