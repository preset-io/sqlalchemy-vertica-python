"""Real-server regressions for types, schemas, parameters and connections.

Requires VERTICA_TEST_URL for a disposable database where the user may create
and drop schemas.
"""
import os
import uuid

import pytest
import sqlalchemy as sa

SCHEMA_A = 'live_types_a'
SCHEMA_B = 'live_types_b'


@pytest.fixture(scope='module')
def engine():
    engine = sa.create_engine(os.environ['VERTICA_TEST_URL'], pool_pre_ping=True,
                              pool_size=1, max_overflow=0, pool_timeout=5)
    with engine.begin() as conn:
        for schema in (SCHEMA_A, SCHEMA_B):
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
            conn.exec_driver_sql(f'CREATE SCHEMA {schema}')
        conn.exec_driver_sql(
            f'CREATE TABLE {SCHEMA_A}.t (a INT, b BIGINT, c SMALLINT, u UUID, '
            f'g GEOMETRY(100), arr ARRAY[INT], i INTERVAL YEAR TO MONTH, '
            f'CONSTRAINT uq_t UNIQUE (c, a))')
        conn.exec_driver_sql(f'CREATE TABLE {SCHEMA_B}.live_types_dup (other VARCHAR(5))')
        conn.exec_driver_sql('DROP TABLE IF EXISTS live_types_dup')
        conn.exec_driver_sql('CREATE TABLE live_types_dup (mine INT)')
        conn.exec_driver_sql(
            f'CREATE TABLE {SCHEMA_A}.parent (id INT, k INT, PRIMARY KEY (id, k))')
        conn.exec_driver_sql(
            f'CREATE TABLE {SCHEMA_A}.child (pid INT, pk INT, CONSTRAINT fk_child '
            f'FOREIGN KEY (pid, pk) REFERENCES {SCHEMA_A}.parent (id, k))')
        conn.exec_driver_sql(f'CREATE TABLE {SCHEMA_A}.w (id INT, v VARCHAR(40), b VARBINARY(8))')
    yield engine
    with engine.begin() as conn:
        for schema in (SCHEMA_A, SCHEMA_B):
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        conn.exec_driver_sql('DROP TABLE IF EXISTS live_types_dup')
    engine.dispose()


def test_reflected_types(engine):
    columns = {c['name']: c['type'] for c in sa.inspect(engine).get_columns('t', schema=SCHEMA_A)}
    assert all(type(columns[name]) is sa.BIGINT for name in 'abc')
    assert isinstance(columns['u'], sa.UUID)
    assert not any(isinstance(t, sa.types.NullType) for t in columns.values())


def test_default_schema_does_not_merge_schemas(engine):
    # schema=None means the default schema only, even when another schema
    # holds a table of the same name.
    inspector = sa.inspect(engine)
    assert [c['name'] for c in inspector.get_columns('live_types_dup')] == ['mine']
    assert inspector.get_table_names().count('live_types_dup') == 1
    assert [c['name'] for c in inspector.get_columns('live_types_dup', schema=SCHEMA_B)] == ['other']


def test_unique_and_foreign_keys(engine):
    inspector = sa.inspect(engine)
    assert inspector.get_unique_constraints('t', schema=SCHEMA_A) == [
        {'name': 'uq_t', 'column_names': ['a', 'c']}]
    fk, = inspector.get_foreign_keys('child', schema=SCHEMA_A)
    assert (fk['constrained_columns'], fk['referred_table'], fk['referred_columns']) == (
        ['pid', 'pk'], 'parent', ['id', 'k'])


def test_parameters_are_not_rewritten_by_other_parameters(engine):
    with engine.connect() as conn:
        row = conn.execute(sa.text('SELECT :a, :b'), {'a': "x :b ') --", 'b': 'y'}).one()
    assert tuple(row) == ("x :b ') --", 'y')


def test_executemany_binary_and_uuid(engine):
    table = sa.Table('w', sa.MetaData(), schema=SCHEMA_A, autoload_with=engine)
    rows = [{'id': 1, 'v': 'a|b\\c', 'b': b'\x00\xff'}, {'id': 2, 'v': None, 'b': None}]
    with engine.begin() as conn:
        conn.execute(table.insert(), rows)
        assert [dict(r) for r in conn.execute(
            sa.select(table).order_by(table.c.id)).mappings()] == rows
    with engine.connect() as conn:
        value = uuid.uuid4()
        assert conn.execute(sa.select(sa.cast(sa.literal(value, sa.UUID), sa.UUID))).scalar() == value


def test_isolation_level_does_not_leak_connections(engine):
    for _ in range(3):
        with engine.connect() as conn:
            conn.execution_options(isolation_level='SERIALIZABLE')
            assert conn.get_isolation_level() == 'SERIALIZABLE'
    with engine.connect() as conn:
        assert conn.get_isolation_level() == 'READ COMMITTED'


def test_pre_ping_replaces_a_closed_session(engine):
    with engine.connect() as conn:
        session = conn.exec_driver_sql('SELECT session_id FROM current_session').scalar()
    admin = sa.create_engine(engine.url, poolclass=sa.pool.NullPool)
    with admin.connect() as conn:
        conn.exec_driver_sql(f"SELECT CLOSE_SESSION('{session}')").all()
    admin.dispose()
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT session_id FROM current_session').scalar() != session
