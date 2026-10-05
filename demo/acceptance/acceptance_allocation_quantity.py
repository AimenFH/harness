"""Acceptance check for the Stage 1 demo bug fix (Cosmic Python, chapter 5).

This file lives in the harness repository, OUTSIDE the target copy that
the agent may edit, so the agent can neither see nor change it. demo.py
runs it with the target copy as the working directory, which makes the
target's packages (domain, service_layer, adapters) importable.

The file name does not start with "test_" on purpose, so the harness's
own test suite does not collect it.

Expected: FAILS on the starting commit, PASSES after a correct fix.
"""

import pytest

from adapters import repository
from domain import model
from service_layer import services


class FakeRepository(repository.AbstractRepository):
    """In-memory repository, defined here so the agent cannot change it."""

    def __init__(self, batches=()):
        self._batches = set(batches)

    def add(self, batch):
        self._batches.add(batch)

    def get(self, reference):
        return next(b for b in self._batches if b.reference == reference)

    def list(self):
        return list(self._batches)


class FakeSession:
    committed = False

    def commit(self):
        self.committed = True


def invalid_quantity_error():
    error = getattr(services, "InvalidQuantity", None)
    assert error is not None, "service_layer.services.InvalidQuantity does not exist"
    return error


def make_batch(qty=10):
    batch = model.Batch("batch-1", "LAMP", qty, eta=None)
    return batch, FakeRepository([batch])


def assert_rejected(qty, batch, repo, session):
    """Call allocate with a bad qty; it must raise InvalidQuantity.

    If it is accepted instead, the failure message shows the actual bug
    (stock and commit state), not just that the exception is missing.
    """
    stock_before = batch.available_quantity
    try:
        services.allocate("order-1", "LAMP", qty, repo, session)
    except Exception as error:
        assert isinstance(error, invalid_quantity_error()), (
            f"expected InvalidQuantity, got {type(error).__name__}: {error}")
    else:
        pytest.fail(f"allocate(qty={qty}) was accepted: available stock "
                    f"{stock_before} -> {batch.available_quantity}, "
                    f"committed={session.committed}")


@pytest.mark.parametrize("bad_qty", [0, -5])
def test_non_positive_quantity_is_rejected(bad_qty):
    batch, repo = make_batch()
    session = FakeSession()

    assert_rejected(bad_qty, batch, repo, session)

    assert batch.available_quantity == 10, "stock must not change"
    assert session.committed is False, "nothing may be committed"


def test_negative_quantity_cannot_be_used_to_overallocate():
    batch, repo = make_batch(qty=10)

    assert_rejected(-5, batch, repo, FakeSession())

    # Only 10 units exist, so an order for 15 must still be out of stock.
    with pytest.raises(model.OutOfStock):
        services.allocate("order-2", "LAMP", 15, repo, FakeSession())


def test_positive_quantity_still_allocates():
    batch, repo = make_batch(qty=10)
    session = FakeSession()

    assert services.allocate("order-1", "LAMP", 3, repo, session) == "batch-1"
    assert batch.available_quantity == 7
    assert session.committed is True
