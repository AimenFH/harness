"""Independent expiry acceptance; executed explicitly in Docker, never by host pytest."""

from datetime import date, datetime, timedelta, timezone
import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import clear_mappers, sessionmaker

from adapters import orm, repository
from domain import model
from service_layer import services

NOW = date(2035, 1, 15)
EXPIRY = NOW + timedelta(days=2)


class FakeRepository(repository.AbstractRepository):
    def __init__(self, batches):
        self.batches = list(batches)

    def add(self, batch):
        self.batches.append(batch)

    def get(self, reference):
        return next(b for b in self.batches if b.reference == reference)

    def list(self):
        return list(self.batches)


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def fixture_stock(qty=10):
    batch = model.Batch('batch-1', 'LAMP', qty, eta=None)
    return batch, FakeRepository([batch]), FakeSession()


def require_feature():
    """Missing feature is an assertion failure, never a collection error or skip."""
    requirements = [
        ('expires_at' in inspect.signature(services.allocate).parameters,
         'services.allocate does not accept expires_at'),
        ('now' in inspect.signature(services.allocate).parameters,
         'services.allocate has no injectable now'),
        ('expires_at' in inspect.signature(model.OrderLine).parameters,
         'OrderLine has no optional expires_at'),
        (callable(getattr(model.Batch, 'release_expired', None)),
         'Batch.release_expired is missing'),
        (callable(getattr(services, 'release_expired', None)),
         'services.release_expired is missing'),
        (isinstance(getattr(services, 'InvalidExpiry', None), type),
         'services.InvalidExpiry is missing'),
    ]
    missing = [message for valid, message in requirements if not valid]
    assert not missing, 'MISSING FEATURE: ' + '; '.join(missing)


def reserve(order, qty, repo, session, expires_at=EXPIRY):
    return services.allocate(order, 'LAMP', qty, repo, session,
                             expires_at=expires_at, now=NOW)


def test_legacy_allocation_control_still_works_on_starting_code():
    batch, repo, session = fixture_stock()
    assert services.allocate('old-order', 'LAMP', 3, repo, session) == 'batch-1'
    assert batch.available_quantity == 7
    assert session.commits == 1
    assert model.OrderLine('old-order', 'LAMP', 3) in batch._allocations


def test_optional_parameters_have_compatible_defaults():
    require_feature()
    assert inspect.signature(services.allocate).parameters['expires_at'].default is None
    assert inspect.signature(services.allocate).parameters['now'].default is None
    assert inspect.signature(model.OrderLine).parameters['expires_at'].default is None


def test_orderline_and_service_preserve_expiry_and_stock_accounting():
    require_feature()
    batch, repo, session = fixture_stock()
    assert reserve('new-order', 4, repo, session) == 'batch-1'
    assert batch.available_quantity == 6
    assert batch.allocated_quantity == 4
    assert session.commits == 1
    line, = batch._allocations
    assert line.expires_at == EXPIRY
    assert model.OrderLine('new-order', 'LAMP', 4, expires_at=EXPIRY) == line


def test_permanent_reservations_survive_release_and_explicit_none():
    require_feature()
    batch, repo, session = fixture_stock()
    services.allocate('old-call', 'LAMP', 2, repo, session)
    reserve('explicit-none', 3, repo, session, expires_at=None)
    assert services.release_expired(EXPIRY + timedelta(days=100), repo, session) == 0
    assert batch.available_quantity == 5
    assert session.commits == 3


def test_release_boundary_is_inclusive_and_does_not_read_the_clock():
    require_feature()
    batch, repo, session = fixture_stock()
    reserve('expires', 4, repo, session)
    assert services.release_expired(EXPIRY - timedelta(days=1), repo, session) == 0
    assert batch.available_quantity == 6
    assert services.release_expired(EXPIRY, repo, session) == 1
    assert batch.available_quantity == 10
    assert session.commits == 3


def test_domain_release_operates_on_reservations_and_is_idempotent():
    require_feature()
    batch = model.Batch('domain', 'LAMP', 10, eta=None)
    batch.allocate(model.OrderLine('expires', 'LAMP', 4, expires_at=EXPIRY))
    batch.allocate(model.OrderLine('permanent', 'LAMP', 2))
    assert batch.release_expired(EXPIRY - timedelta(days=1)) == 0
    assert batch.release_expired(EXPIRY) == 1
    assert batch.available_quantity == 8
    assert batch.release_expired(EXPIRY) == 0
    assert batch.available_quantity == 8


def test_released_stock_can_be_reused_once_and_cannot_be_returned_twice():
    require_feature()
    batch, repo, session = fixture_stock()
    reserve('expires', 10, repo, session)
    with pytest.raises(model.OutOfStock):
        services.allocate('blocked', 'LAMP', 1, repo, session)
    assert services.release_expired(EXPIRY, repo, session) == 1
    assert services.allocate('replacement', 'LAMP', 10, repo, session) == 'batch-1'
    assert services.release_expired(EXPIRY, repo, session) == 0
    assert services.release_expired(EXPIRY + timedelta(days=10), repo, session) == 0
    assert batch.available_quantity == 0
    with pytest.raises(model.OutOfStock):
        services.allocate('overstock', 'LAMP', 1, repo, session)


def test_release_counts_reservations_across_batches_and_preserves_other_stock():
    require_feature()
    a = model.Batch('a', 'LAMP', 10, eta=None)
    b = model.Batch('b', 'CHAIR', 20, eta=None)
    repo, session = FakeRepository([a, b]), FakeSession()
    reserve('expired-lamp', 4, repo, session)
    reserve('future-lamp', 2, repo, session, expires_at=EXPIRY + timedelta(days=1))
    services.allocate('expired-chair', 'CHAIR', 7, repo, session, expires_at=EXPIRY, now=NOW)
    services.allocate('permanent-chair', 'CHAIR', 3, repo, session)
    before = session.commits
    assert services.release_expired(EXPIRY, repo, session) == 2
    assert session.commits == before + 1
    assert (a.available_quantity, b.available_quantity) == (8, 17)
    assert services.release_expired(EXPIRY, repo, session) == 0
    assert (a.available_quantity, b.available_quantity) == (8, 17)


def test_repeated_identical_reservation_does_not_consume_stock_twice():
    require_feature()
    batch, repo, session = fixture_stock()
    reserve('same', 4, repo, session)
    reserve('same', 4, repo, session)
    assert batch.available_quantity == 6
    assert services.release_expired(EXPIRY, repo, session) == 1
    assert services.release_expired(EXPIRY, repo, session) == 0
    assert batch.available_quantity == 10


@pytest.mark.parametrize('bad_expiry', [
    NOW - timedelta(days=1), NOW, '2035-01-17', 123, True,
    datetime(2035, 1, 17), datetime(2035, 1, 17, tzinfo=timezone.utc),
], ids=['past', 'equal-now', 'string', 'integer', 'boolean', 'datetime', 'aware-datetime'])
def test_invalid_expiry_has_clear_error_without_mutation_or_commit(bad_expiry):
    require_feature()
    batch, repo, session = fixture_stock()
    with pytest.raises(services.InvalidExpiry, match='(?i)expir'):
        reserve('bad', 4, repo, session, expires_at=bad_expiry)
    assert batch.available_quantity == 10
    assert batch._allocations == set()
    assert session.commits == 0


@pytest.mark.parametrize('bad_now', [None, '2035-01-17', datetime(2035, 1, 17), True])
def test_release_requires_an_explicit_date_and_has_no_side_effects_on_error(bad_now):
    require_feature()
    batch, repo, session = fixture_stock()
    reserve('expires', 4, repo, session)
    before = session.commits
    with pytest.raises(services.InvalidExpiry, match='(?i)now|date|clock'):
        services.release_expired(bad_now, repo, session)
    assert batch.available_quantity == 6
    assert session.commits == before


def test_allocate_rejects_invalid_injected_clock_before_any_changes():
    require_feature()
    batch, repo, session = fixture_stock()
    with pytest.raises(services.InvalidExpiry, match='(?i)now|date|clock'):
        services.allocate('bad-clock', 'LAMP', 3, repo, session,
                          expires_at=EXPIRY, now='2035-01-15')
    assert batch.available_quantity == 10
    assert session.commits == 0


@pytest.fixture
def db_session():
    # This fixture is protected too; no target test fixtures are imported.
    clear_mappers()
    engine = create_engine('sqlite:///:memory:')
    orm.start_mappers()
    orm.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        clear_mappers()
        engine.dispose()


def test_expiry_and_release_persist_across_repository_reload(db_session):
    require_feature()
    repo = repository.SqlAlchemyRepository(db_session)
    repo.add(model.Batch('db-batch', 'LAMP', 10, eta=None))
    db_session.commit()
    assert reserve('persisted', 6, repo, db_session) == 'db-batch'
    db_session.expunge_all()
    repo = repository.SqlAlchemyRepository(db_session)
    batch = repo.get('db-batch')
    line, = batch._allocations
    assert line.expires_at == EXPIRY
    assert batch.available_quantity == 4
    assert services.release_expired(EXPIRY, repo, db_session) == 1
    db_session.expunge_all()
    repo = repository.SqlAlchemyRepository(db_session)
    assert repo.get('db-batch').available_quantity == 10
    assert services.release_expired(EXPIRY, repo, db_session) == 0
    services.allocate('replacement', 'LAMP', 10, repo, db_session)
    db_session.expunge_all()
    assert repo.get('db-batch').available_quantity == 0
    assert services.release_expired(EXPIRY, repo, db_session) == 0
    assert repo.get('db-batch').available_quantity == 0


def test_legacy_database_rows_without_expiry_remain_permanent(db_session):
    require_feature()
    db_session.execute("INSERT INTO batches (reference, sku, _purchased_quantity, eta) "
                       "VALUES ('old', 'LAMP', 10, null)")
    db_session.execute("INSERT INTO order_lines (orderid, sku, qty) VALUES ('legacy', 'LAMP', 3)")
    db_session.execute("INSERT INTO allocations (orderline_id, batch_id) "
                       "SELECT order_lines.id, batches.id FROM order_lines, batches")
    db_session.commit()
    repo = repository.SqlAlchemyRepository(db_session)
    batch = repo.get('old')
    line, = batch._allocations
    assert line.expires_at is None
    assert services.release_expired(EXPIRY, repo, db_session) == 0
    assert batch.available_quantity == 7
