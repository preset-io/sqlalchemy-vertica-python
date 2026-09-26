"""SQLAlchemy 2 regressions: no PostgreSQL catalog may leak into reflection."""
import re
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
        ('uq_pair', 'a'), ('uq_pair', 'b'),
    ]
    assert VerticaDialect().get_unique_constraints(connection, 't', schema='s') == [
        {'name': 'uq_pair', 'column_names': ['a', 'b']},
    ]


def test_unknown_column_type_warns_and_returns_nulltype():
    with pytest.warns(sa.exc.SAWarning, match="Did not recognize type 'MYSTERY' of column 'g'"):
        info = VerticaDialect()._get_column_info(
            'g', 'mystery(10)', True, None, False, False, None,
        )
    assert info == {
        'name': 'g', 'type': sa.types.NULLTYPE, 'nullable': True,
        'default': None, 'primary_key': False,
    }


@pytest.mark.parametrize('schema', [None, 's'])
def test_primary_key_query_orders_by_key_position(schema):
    connection = Mock()
    connection.execute.return_value = iter([])
    connection.scalar.return_value = 'public'
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
    assert 'table_name = :table_name' in query
    assert 'table_schema = :schema' in query
    # schema=None means the default schema, not every schema.
    assert connection.execute.call_args.args[1] == {
        'table_name': 't', 'schema': 'public' if schema is None else 's'}


def test_primary_key_preserves_declared_column_order():
    columns = ['order_id', 'customer_id', 'event_date', 'region_code', 'product_sku']
    connection = Mock()
    connection.execute.return_value = iter((1, 'pk_orders', column) for column in columns)
    assert VerticaDialect().get_pk_constraint(connection, 'orders', schema='s') == {
        'constrained_columns': columns, 'name': 'pk_orders',
    }


def test_unique_constraints_are_ordered_by_the_catalog_query():
    # The catalog stores no key position for UNIQUE constraints; the query
    # orders by constraint name and then by column position in the table.
    connection = Mock()
    connection.execute.return_value.all.return_value = [
        ('uq_a', 'x'), ('uq_a', 'y'), ('uq_z', 'b'), ('uq_z', 'a'),
    ]
    result = VerticaDialect().get_unique_constraints(connection, 't', schema='s')
    assert result == [
        {'name': 'uq_a', 'column_names': ['x', 'y']},
        {'name': 'uq_z', 'column_names': ['b', 'a']},
    ]
    query = ' '.join(str(connection.execute.call_args.args[0]).lower().split())
    assert query.endswith('order by cc.constraint_name, c.ordinal_position')


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
def test_enumeration_without_schema_uses_the_default_schema(method):
    connection = Mock()
    captured = _capture_execute(connection)
    connection.scalar.return_value = 'public'
    getattr(VerticaDialect(), method)(connection, schema=None)
    assert ':schema' in str(captured['statement'])
    assert captured['params'] == {'schema': 'public'}


class _CatalogResult:
    """Minimal result object for the catalog queries issued by the dialect."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)

    def fetchall(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return True


def _record_catalog_queries(connection, rows_for=lambda sql: ()):
    calls = []

    def execute(statement, params=None, *args, **kwargs):
        calls.append((' '.join(str(statement).split()), params))
        return _CatalogResult(rows_for(str(statement)))

    connection.execute.side_effect = execute
    connection.scalar.return_value = 'public'
    return calls


def _identity_column_rows(sql):
    # One identity column, so get_columns also runs its sequence lookup.
    if 'v_catalog.columns' in sql and 'constraint_columns' not in sql:
        row = Mock(column_name='id', data_type='int', column_default='',
                   is_nullable=False, is_identity=True)
        return [row]
    return []


CATALOG_CALLS = {
    'has_schema': lambda d, c, schema, name: d.has_schema(c, schema),
    'has_table': lambda d, c, schema, name: d.has_table(c, name, schema=schema),
    'has_sequence': lambda d, c, schema, name: d.has_sequence(c, name, schema=schema),
    'has_type': lambda d, c, schema, name: d.has_type(c, name, schema=schema),
    'get_table_comment': lambda d, c, schema, name: d.get_table_comment(c, name, schema=schema),
    'get_columns': lambda d, c, schema, name: d.get_columns(c, name, schema=schema),
    'get_unique_constraints':
        lambda d, c, schema, name: d.get_unique_constraints(c, name, schema=schema),
    'get_check_constraints':
        lambda d, c, schema, name: d.get_check_constraints(c, name, schema=schema),
    'get_pk_constraint': lambda d, c, schema, name: d.get_pk_constraint(c, name, schema=schema),
    'get_table_names': lambda d, c, schema, name: d.get_table_names(c, schema=schema),
    'get_view_names': lambda d, c, schema, name: d.get_view_names(c, schema=schema),
}

UNUSUAL_NAMES = [
    ("abc'def", "tab'le"),
    ("it''s", "O'Brien's table"),
    ('dou"ble', 'back\\slash'),
    ('%(schema)s', 'name:with:colons'),
]


def _catalog_queries(method, schema, name):
    connection = Mock()
    calls = _record_catalog_queries(connection, _identity_column_rows)
    CATALOG_CALLS[method](VerticaDialect(), connection, schema, name)
    return calls


@pytest.mark.parametrize('method', sorted(CATALOG_CALLS))
@pytest.mark.parametrize('schema, name', UNUSUAL_NAMES)
def test_names_containing_quotes_do_not_change_the_query(method, schema, name):
    baseline = _catalog_queries(method, 'plain_schema', 'plain_name')
    unusual = _catalog_queries(method, schema, name)
    assert baseline, 'expected the method to issue a catalog query'
    # Same SQL text regardless of the names; only the bound values differ.
    assert [sql for sql, _ in unusual] == [sql for sql, _ in baseline]
    for sql, params in unusual:
        assert schema not in sql and name not in sql
        assert 'plain_schema' not in sql and 'plain_name' not in sql


@pytest.mark.parametrize('method', sorted(CATALOG_CALLS))
def test_catalog_names_are_sent_as_bound_values(method):
    schema, name = "abc'def", "tab'le"
    calls = _catalog_queries(method, schema, name)
    sent = [value for _, params in calls for value in (params or {}).values()]
    if method != 'has_type':
        assert schema in sent
    if method not in ('has_schema', 'get_table_names', 'get_view_names'):
        assert name in sent
    for sql, params in calls:
        placeholders = set(re.findall(r'(?<!:):(\w+)', sql))
        assert placeholders == set(params or {})


@pytest.mark.parametrize('method', sorted(set(CATALOG_CALLS) - {'has_schema', 'has_type'}))
def test_catalog_without_schema_binds_only_what_is_used(method):
    calls = _catalog_queries(method, None, "tab'le")
    for sql, params in calls:
        assert "tab'le" not in sql
        placeholders = set(re.findall(r'(?<!:):(\w+)', sql))
        assert placeholders == set(params or {})


@pytest.mark.parametrize('type_name', ['integer', 'INTEGER', 'Integer'])
def test_has_type_matches_catalog_names_case_insensitively(type_name):
    # v_catalog.types stores names such as 'Integer'; emulate its comparison.
    with sa.create_engine('sqlite://').connect() as conn:
        conn.execute(sa.text('ATTACH DATABASE ":memory:" AS v_catalog'))
        conn.execute(sa.text('CREATE TABLE v_catalog.types (type_name TEXT)'))
        conn.execute(sa.text("INSERT INTO v_catalog.types VALUES ('Integer'), ('Varchar')"))
        dialect = VerticaDialect()
        assert dialect.has_type(conn, type_name) is True
        assert dialect.has_type(conn, 'no_such_type') is False
