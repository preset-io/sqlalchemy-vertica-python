"""Catalog methods with schema/table names that contain a quote or another
bound parameter's placeholder, against a real server.

Requires VERTICA_TEST_URL for a disposable database where the user may create
and drop schemas. The objects are created with double-quoted identifiers.
"""
import os

import pytest
import sqlalchemy as sa

SCHEMA = "sch'ema :table_name :sequence_name"
TABLE = "tab'le :schema %s"
SEQUENCE = "seq'ence :schema"


def q(name):
    return '"%s"' % name.replace('"', '""')


@pytest.fixture(scope='module')
def engine():
    engine = sa.create_engine(os.environ['VERTICA_TEST_URL'])
    with engine.begin() as conn:
        conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {q(SCHEMA)} CASCADE')
        conn.exec_driver_sql(f'CREATE SCHEMA {q(SCHEMA)}')
        conn.exec_driver_sql(
            f'CREATE TABLE {q(SCHEMA)}.{q(TABLE)} ('
            f'id IDENTITY(1,1), k INT NOT NULL, label VARCHAR(10), '
            f'CONSTRAINT pk_names PRIMARY KEY (k), '
            f'CONSTRAINT uq_names UNIQUE (label), '
            f'CONSTRAINT ck_names CHECK (k > 0))')
        conn.exec_driver_sql(f"COMMENT ON TABLE {q(SCHEMA)}.{q(TABLE)} IS 'names comment'")
        conn.exec_driver_sql(f'CREATE SEQUENCE {q(SCHEMA)}.{q(SEQUENCE)}')
    yield engine
    with engine.begin() as conn:
        conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {q(SCHEMA)} CASCADE')
    engine.dispose()


def test_catalog_methods_find_objects_with_unusual_names(engine):
    inspector = sa.inspect(engine)
    assert inspector.has_table(TABLE, schema=SCHEMA)
    assert not inspector.has_table(TABLE + 'x', schema=SCHEMA)
    assert TABLE in inspector.get_table_names(schema=SCHEMA)
    assert [c['name'] for c in inspector.get_columns(TABLE, schema=SCHEMA)] == ['id', 'k', 'label']
    assert inspector.get_pk_constraint(TABLE, schema=SCHEMA) == {
        'constrained_columns': ['k'], 'name': 'pk_names'}
    assert inspector.get_unique_constraints(TABLE, schema=SCHEMA) == [
        {'name': 'uq_names', 'column_names': ['label']}]
    assert [c['name'] for c in inspector.get_check_constraints(TABLE, schema=SCHEMA)] == ['ck_names']
    assert inspector.get_table_comment(TABLE, schema=SCHEMA) == {'text': 'names comment'}
    with engine.connect() as conn:
        assert engine.dialect.has_sequence(conn, SEQUENCE, schema=SCHEMA)
        assert not engine.dialect.has_sequence(conn, SEQUENCE + 'x', schema=SCHEMA)
    table = sa.Table(TABLE, sa.MetaData(), schema=SCHEMA, autoload_with=engine)
    assert list(table.c.keys()) == ['id', 'k', 'label']
    assert inspector.has_schema(SCHEMA)


PCT_TABLE = 'pct%tab'


def test_literal_percent_and_percent_named_table(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE TABLE {q(SCHEMA)}.{q(PCT_TABLE)} ("v%" VARCHAR(10))')
        # Driver SQL: text() would read ":table_name" in the schema as a bind.
        conn.exec_driver_sql(f"INSERT INTO {q(SCHEMA)}.{q(PCT_TABLE)} VALUES ('50%')")
    table = sa.Table(PCT_TABLE, sa.MetaData(), schema=SCHEMA, autoload_with=engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT '50%'")).scalar() == '50%'
        assert conn.execute(sa.text("SELECT '50%', :p"), {'p': '%s'}).one() == ('50%', '%s')
        assert conn.execute(sa.select(table.c['v%'])).scalar() == '50%'
        assert conn.execute(
            sa.select(sa.func.count()).select_from(table).where(table.c['v%'].like('50%'))
        ).scalar() == 1
        # Raw driver SQL is sent as written.
        assert conn.exec_driver_sql("SELECT '50%'").scalar() == '50%'
    assert sa.inspect(engine).get_table_names(schema=SCHEMA) == [PCT_TABLE, TABLE]
