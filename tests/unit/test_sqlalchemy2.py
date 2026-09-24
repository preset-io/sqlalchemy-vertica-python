"""SQLAlchemy 2 regressions: no PostgreSQL catalog may leak into reflection."""
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import reflection
from sqlalchemy.sql.elements import TextClause

from sqla_vertica_python.vertica_python import VerticaDialect


BULK_METHODS = (
    'columns', 'pk_constraint', 'foreign_keys', 'indexes',
    'unique_constraints', 'check_constraints', 'table_comment', 'table_options',
)


def test_legacy_url_entry_point():
    engine = sa.create_engine('vertica+vertica_python://user@localhost/db')
    assert isinstance(engine.dialect, VerticaDialect)
    assert engine.dialect.driver == 'vertica_python'
    engine.dispose()


def test_initialize_uses_executable_sql():
    connection = Mock()

    def scalar(statement):
        assert isinstance(statement, TextClause)
        return ('Vertica Analytic Database v25.4.0-0' if 'version' in str(statement)
                else 'public')

    connection.scalar.side_effect = scalar
    connection.connection.dbapi_connection.cursor.return_value.fetchone.return_value = ('read committed',)
    dialect = VerticaDialect()
    dialect.initialize(connection)
    assert dialect.server_version_info == (25, 4, 0)
    assert dialect.default_schema_name == 'public'


@pytest.mark.parametrize('name', BULK_METHODS)
def test_bulk_uses_vertica_single_table_method(name):
    dialect = VerticaDialect()
    single = Mock(return_value={'reflected': name})
    setattr(dialect, 'get_' + name, single)
    dialect.get_table_names = Mock(return_value=['wanted', 'excluded'])
    connection = Mock()
    cache = {}
    result = dict(getattr(dialect, 'get_multi_' + name)(
        connection, schema='schema', filter_names=['wanted'],
        kind=reflection.ObjectKind.TABLE, scope=reflection.ObjectScope.DEFAULT,
        info_cache=cache,
    ))
    assert result == {('schema', 'wanted'): {'reflected': name}}
    single.assert_called_once_with(connection, 'wanted', schema='schema', info_cache=cache)
    connection.execute.assert_not_called()


def test_bulk_view_and_missing_table():
    dialect = VerticaDialect()
    dialect.get_view_names = Mock(return_value=['view'])
    dialect.get_columns = Mock(return_value=[{'name': 'id'}])
    args = dict(schema='s', filter_names=None, kind=reflection.ObjectKind.VIEW,
                scope=reflection.ObjectScope.DEFAULT)
    assert dict(dialect.get_multi_columns(Mock(), **args)) == {
        ('s', 'view'): [{'name': 'id'}],
    }
    dialect.get_columns.side_effect = sa.exc.NoSuchTableError('missing')
    args.update(filter_names=['missing'], kind=reflection.ObjectKind.ANY,
                scope=reflection.ObjectScope.ANY)
    assert dict(dialect.get_multi_columns(Mock(), **args)) == {}


def test_has_table_accepts_inspector_cache():
    connection = Mock()
    connection.execute.return_value.scalar.return_value = True
    assert VerticaDialect().has_table(connection, 't', schema='s', info_cache={})


def test_missing_columns_raise_no_such_table():
    connection = Mock()
    connection.execute.return_value = []
    dialect = VerticaDialect()
    dialect.has_table = Mock(return_value=False)
    with pytest.raises(sa.exc.NoSuchTableError):
        dialect.get_columns(connection, 'missing', schema='s')


def test_unique_constraint_columns_survive_result_consumption():
    connection = Mock()
    connection.execute.return_value.all.return_value = [
        (1, 'uq_pair', 'a'), (1, 'uq_pair', 'b'),
    ]
    assert VerticaDialect().get_unique_constraints(connection, 't', schema='s') == [
        {'name': 'uq_pair', 'column_names': ['a', 'b']},
    ]


def test_identity_sequence_uses_sqlalchemy2_row_mapping():
    with sa.create_engine('sqlite://').connect() as conn:
        row = conn.execute(sa.text("SELECT 'seq' AS name, 1 AS start, 1 AS increment")).one()
    info = VerticaDialect()._get_column_info('id', 'int', False, '', True, True, row)
    assert info['sequence'] == {'name': 'seq', 'start': 1, 'increment': 1}
    assert info['autoincrement'] is True
