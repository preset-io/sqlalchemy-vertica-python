"""Additional real-server regressions (requires VERTICA_TEST_URL explicitly)."""
import os

import pytest
import sqlalchemy as sa


@pytest.fixture
def engine():
    engine = sa.create_engine(os.environ['VERTICA_TEST_URL'], pool_pre_ping=True)
    yield engine
    engine.dispose()


def test_metadata_reflect_and_views(engine):
    metadata = sa.MetaData()
    metadata.reflect(engine, schema='sc121481', views=True)
    table = metadata.tables['sc121481.sc121481_fixture']
    assert 'sc121481.sc121481_view' in metadata.tables
    with engine.connect() as conn:
        assert conn.execute(sa.select(table.c.txt).where(table.c.id == 'seed')).scalar_one() == 'hello'
    view_columns = sa.inspect(engine).get_multi_columns(
        schema='sc121481', kind=sa.engine.reflection.ObjectKind.VIEW)
    assert ('sc121481', 'sc121481_view') in view_columns


def test_all_bulk_reflection_apis(engine):
    inspector = sa.inspect(engine)
    for name in ('columns', 'pk_constraint', 'foreign_keys', 'indexes',
                 'unique_constraints', 'check_constraints', 'table_comment'):
        result = getattr(inspector, 'get_multi_' + name)(
            schema='sc121481', filter_names=['sc121481_fixture'])
        assert ('sc121481', 'sc121481_fixture') in result
    assert inspector.get_multi_columns(schema='sc121481', filter_names=['absent']) == {}
    with pytest.raises(sa.exc.NoSuchTableError):
        inspector.get_columns('absent', schema='sc121481')
