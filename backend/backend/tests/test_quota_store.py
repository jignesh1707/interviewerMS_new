import threading

import pytest

from app.services.storage import Store

PERIOD = "2026-10"


@pytest.fixture()
def store(tmp_path):
    store = Store(database_path=tmp_path / "quota.db")
    yield store
    store.close()


def test_new_student_has_nothing_used(store):
    assert store.quota_get("t1", "stu-1", PERIOD) == {"used_minutes": 0, "bonus_minutes": 0}


def test_debit_succeeds_until_the_allowance_is_spent(store):
    for _ in range(10):  # 10 x 15 = 150
        assert store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150) is True
    assert store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150) is False
    assert store.quota_get("t1", "stu-1", PERIOD)["used_minutes"] == 150


def test_failed_debit_leaves_the_balance_untouched(store):
    assert store.quota_debit("t1", "stu-1", PERIOD, 140, allowance=150) is True
    assert store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150) is False
    assert store.quota_get("t1", "stu-1", PERIOD)["used_minutes"] == 140
    assert store.quota_debit("t1", "stu-1", PERIOD, 10, allowance=150) is True  # exactly what is left


def test_mixed_lengths_share_one_balance(store):
    assert store.quota_debit("t1", "stu-1", PERIOD, 25, allowance=150)
    assert store.quota_debit("t1", "stu-1", PERIOD, 20, allowance=150)
    assert store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150)
    assert store.quota_get("t1", "stu-1", PERIOD)["used_minutes"] == 60


def test_credit_gives_minutes_back_and_never_goes_negative(store):
    store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150)
    store.quota_credit("t1", "stu-1", PERIOD, 15)
    assert store.quota_get("t1", "stu-1", PERIOD)["used_minutes"] == 0
    store.quota_credit("t1", "stu-1", PERIOD, 15)  # double refund must not create free minutes
    assert store.quota_get("t1", "stu-1", PERIOD)["used_minutes"] == 0


def test_bonus_minutes_raise_the_ceiling(store):
    assert store.quota_debit("t1", "stu-1", PERIOD, 150, allowance=150)
    assert store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150) is False
    store.quota_add_bonus("t1", "stu-1", PERIOD, 30)
    assert store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150) is True
    assert store.quota_get("t1", "stu-1", PERIOD)["bonus_minutes"] == 30


def test_students_tenants_and_periods_are_separate(store):
    assert store.quota_debit("t1", "stu-1", PERIOD, 150, allowance=150)
    assert store.quota_debit("t1", "stu-2", PERIOD, 15, allowance=150)  # another student
    assert store.quota_debit("t2", "stu-1", PERIOD, 15, allowance=150)  # another tenant
    assert store.quota_debit("t1", "stu-1", "2026-11", 15, allowance=150)  # next month resets


def test_concurrent_bookings_cannot_overspend(store):
    results = []
    lock = threading.Lock()

    def book():
        ok = store.quota_debit("t1", "stu-1", PERIOD, 15, allowance=150)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=book) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 10
    assert store.quota_get("t1", "stu-1", PERIOD)["used_minutes"] == 150
