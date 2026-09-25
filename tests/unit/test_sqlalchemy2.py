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


def test_autoincrement_insert_has_no_returning_before_initialize():
    table = sa.Table(
        'example', sa.MetaData(),
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('value', sa.String),
    )
    dialect = VerticaDialect()
    compiled = table.insert().values(value='test').compile(dialect=dialect)
    assert 'RETURNING' not in str(compiled).upper()
    assert not compiled.implicit_returning
    for operation in ('insert', 'update', 'delete'):
        assert getattr(VerticaDialect, operation + '_returning') is False
        assert getattr(dialect, operation + '_returning') is False


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


@pytest.mark.parametrize('data_type, typename', [
    ('geometry(1000)', 'GEOMETRY'), ('ARRAY[INT]', 'ARRAY'),
])
def test_unknown_column_type_warns_and_returns_nulltype(data_type, typename):
    with pytest.warns(sa.exc.SAWarning, match=f"Did not recognize type '{typename}' of column 'g'"):
        info = VerticaDialect()._get_column_info(
            'g', data_type, True, '', False, False, None,
        )
    assert isinstance(info['type'], sa.types.NullType)
    assert info == {
        'name': 'g', 'type': sa.types.NULLTYPE, 'nullable': True,
        'default': '', 'primary_key': False,
    }


@pytest.mark.parametrize('schema', [None, 's'])
def test_primary_key_query_orders_by_key_position(schema):
    connection = Mock()
    connection.execute.return_value = iter([])
    assert VerticaDialect().get_pk_constraint(connection, 't', schema=schema) == {
        'constrained_columns': [], 'name': None,
    }
    statement = connection.execute.call_args.args[0]
    assert isinstance(statement, TextClause)
    query = ' '.join(str(statement).lower().split())
    # constraint_columns has no ordinal_position; primary_keys has key order,
    # documented as VARCHAR, so sort numerically (including positions >= 10).
    assert 'from v_catalog.primary_keys' in query
    assert query.endswith('order by cast(ordinal_position as integer)')
    assert "constraint_type = 'p'" in query
    assert "table_name = 't'" in query
    assert ("table_schema = 's'" in query) == (schema is not None)


def test_primary_key_preserves_declared_column_order():
    columns = ['order_id', 'customer_id', 'event_date', 'region_code', 'product_sku']
    connection = Mock()
    connection.execute.return_value = iter((1, 'pk_orders', column) for column in columns)
    assert VerticaDialect().get_pk_constraint(connection, 'orders', schema='s') == {
        'constrained_columns': columns, 'name': 'pk_orders',
    }


@pytest.mark.parametrize('names', [
    ['uq_z', 'uq_a', 'uq_m'], ['uq_m', 'uq_z', 'uq_a'],
])
def test_unique_constraints_have_deterministic_names_and_preserve_columns(names):
    connection = Mock()
    connection.execute.return_value.all.return_value = [
        (index, name, column) for column in ['z', 'a'] for index, name in enumerate(names)
    ]
    assert VerticaDialect().get_unique_constraints(connection, 't', schema='s') == [
        {'name': name, 'column_names': ['z', 'a']} for name in sorted(names)
    ]


def test_identity_sequence_uses_sqlalchemy2_row_mapping():
    with sa.create_engine('sqlite://').connect() as conn:
        row = conn.execute(sa.text("SELECT 'seq' AS name, 1 AS start, 1 AS increment")).one()
    info = VerticaDialect()._get_column_info('id', 'int', False, '', True, True, row)
    assert info['sequence'] == {'name': 'seq', 'start': 1, 'increment': 1}
    assert info['autoincrement'] is True


ENUMERATION_METHODS = ('get_table_names', 'get_view_names')


def _capture_execute(connection):
    captured = {}

    def execute(statement, params=None, *args, **kwargs):
        captured['statement'] = statement
        captured['params'] = params
        return iter(())

    connection.execute.side_effect = execute
    return captured


@pytest.mark.parametrize('method', ENUMERATION_METHODS)
def test_enumeration_passes_schema_as_bound_parameter(method):
    connection = Mock()
    captured = _capture_execute(connection)
    # A schema name that includes a quote character must be carried as a value,
    # never merged into the SQL text.
    schema = "abc'def"
    getattr(VerticaDialect(), method)(connection, schema=schema)
    rendered = str(captured['statement'])
    assert ':schema' in rendered
    assert "'" not in rendered
    assert captured['params'] == {'schema': schema}


@pytest.mark.parametrize('method', ENUMERATION_METHODS)
def test_enumeration_without_schema_emits_no_filter(method):
    connection = Mock()
    captured = _capture_execute(connection)
    getattr(VerticaDialect(), method)(connection, schema=None)
    assert 'WHERE' not in str(captured['statement']).upper()
    assert captured['params'] == {}
