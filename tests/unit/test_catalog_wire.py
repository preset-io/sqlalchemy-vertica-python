"""Wire-level check of catalog queries: compile each statement the way
SQLAlchemy executes it and format it with vertica-python's own client-side
parameter substitution, i.e. the exact SQL text sent to the server.

Unit tests that only inspect ``connection.execute`` arguments cannot see how
the driver splices bound values into the statement. Here every bound value
must appear as exactly one correctly quoted literal, whatever characters the
schema or table name contains.
"""
import logging
import re
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from vertica_python.vertica.cursor import Cursor

# Names that are legal Vertica identifiers when double-quoted: a quote, and
# the placeholder spelling of another bound parameter of the same query.
UNUSUAL_NAMES = [
    ("sch'ema", "tab'le"),
    ("sch :table_name :sequence_name", "tab :schema"),
    ("sch %s %(schema)s", "tab %s :sequence_name"),
]

CATALOG_CALLS = {
    'has_table': lambda d, c, s, t: d.has_table(c, t, schema=s),
    'has_sequence': lambda d, c, s, t: d.has_sequence(c, t, schema=s),
    'get_columns': lambda d, c, s, t: d.get_columns(c, t, schema=s),
    'get_table_comment': lambda d, c, s, t: d.get_table_comment(c, t, schema=s),
    'get_unique_constraints': lambda d, c, s, t: d.get_unique_constraints(c, t, schema=s),
    'get_check_constraints': lambda d, c, s, t: d.get_check_constraints(c, t, schema=s),
    'get_pk_constraint': lambda d, c, s, t: d.get_pk_constraint(c, t, schema=s),
}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)

    fetchall = all

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return True


def _rows_for(sql):
    # One identity column, so get_columns also issues its sequence lookup.
    if 'v_catalog.columns' in sql and 'constraint_columns' not in sql:
        return [Mock(column_name='id', data_type='int', column_default=None,
                     is_nullable=False, is_identity=True)]
    return []


def _wire_sql(dialect, statement, params):
    """Return (sent, expected): driver-formatted SQL and a one-pass reference."""
    compiled = statement.compile(dialect=dialect)
    bound = compiled.construct_params(params or {})
    cursor = Cursor(None, logging.getLogger(__name__))
    quote = lambda value: "'%s'" % value.replace("'", "''")  # noqa: E731
    if dialect.positional:
        args = tuple(bound[name] for name in compiled.positiontup)
        values = iter(args)
        expected = re.sub(r'%%|%s', lambda m: '%' if m.group() == '%%' else quote(next(values)),
                          compiled.string)
    else:
        args = bound
        expected = re.sub(r'(?<!:):(\w+)', lambda m: quote(bound[m.group(1)]), compiled.string)
    sent = cursor.format_operation_with_parameters(compiled.string, args) if args else compiled.string
    return sent, expected, bound


@pytest.fixture(scope='module')
def dialect():
    # A real engine, so the paramstyle is exactly what production uses.
    engine = sa.create_engine('vertica+vertica_python://user@localhost/db')
    yield engine.dialect
    engine.dispose()


@pytest.mark.parametrize('method', sorted(CATALOG_CALLS))
@pytest.mark.parametrize('schema, table', UNUSUAL_NAMES)
def test_catalog_sql_sent_to_the_server_quotes_names_exactly(dialect, method, schema, table):
    connection = Mock()
    calls = []

    def execute(statement, params=None, *args, **kwargs):
        calls.append((statement, params))
        return _Result(_rows_for(str(statement)))

    connection.execute.side_effect = execute
    connection.scalar.return_value = 'public'
    CATALOG_CALLS[method](dialect, connection, schema, table)
    assert calls, 'expected at least one catalog query'
    for statement, params in calls:
        sent, expected, bound = _wire_sql(dialect, statement, params)
        assert sent == expected
        for value in bound.values():
            assert "'%s'" % value.replace("'", "''") in sent


class _WireCursor(Cursor):
    """vertica-python's Cursor with only the network round trip replaced."""

    def flush_to_query_ready(self):
        pass

    def _execute_simple_query(self, query):
        self.connection.sent.append(query)
        self.description = None


class _WireConnection:
    """Just enough of a vertica-python connection for Cursor.execute."""

    def __init__(self):
        self.options = {'use_prepared_statements': False}
        self.autocommit = False
        self.sent = []

    def closed(self):
        return False

    def cursor(self):
        return _WireCursor(self, logging.getLogger(__name__))

    def commit(self):
        pass

    rollback = close = commit


@pytest.fixture
def wire():
    raw = _WireConnection()
    engine = sa.create_engine('vertica+vertica_python://user@localhost/db',
                              creator=lambda: raw, poolclass=sa.pool.StaticPool)
    # No server: skip version/default-schema discovery on first connect.
    engine.dialect.initialize = lambda connection: None
    engine.dialect.default_schema_name = 'public'
    with engine.connect() as conn:
        raw.sent.clear()
        yield conn, raw.sent
    engine.dispose()


def test_literal_percent_reaches_the_server_unescaped(wire):
    conn, sent = wire
    conn.execute(sa.text("SELECT '50%' AS a, 'x%%y' AS b"))
    conn.execute(sa.select(sa.literal_column("'50%'")))
    conn.execute(sa.select(sa.column('c')).select_from(sa.table('pct%tab', schema='s%1')))
    conn.execute(sa.text("SELECT :v AS a, '50%' AS b"), {'v': '%s 10%'})
    assert sent == [
        "SELECT '50%' AS a, 'x%%y' AS b",
        "SELECT '50%'",
        'SELECT c \nFROM "s%1"."pct%tab"',
        "SELECT '%s 10%' AS a, '50%' AS b",
    ]


def test_raw_driver_sql_is_sent_verbatim(wire):
    # SQLAlchemy does not escape exec_driver_sql text, so neither may we.
    conn, sent = wire
    conn.exec_driver_sql("SELECT '50%', 'a%%b'")
    assert sent == ["SELECT '50%', 'a%%b'"]


@pytest.mark.parametrize('method', sorted(CATALOG_CALLS))
@pytest.mark.parametrize('schema, table', UNUSUAL_NAMES)
def test_catalog_queries_through_the_execution_path(wire, method, schema, table):
    # Capture each catalog statement, then execute it through SQLAlchemy and
    # the dialect's do_execute into vertica-python's Cursor.execute.
    conn, sent = wire
    captured = Mock()
    calls = []

    def execute(statement, params=None, *args, **kwargs):
        calls.append((statement, params))
        return _Result(_rows_for(str(statement)))

    captured.execute.side_effect = execute
    captured.scalar.return_value = 'public'
    CATALOG_CALLS[method](conn.dialect, captured, schema, table)
    for statement, params in calls:
        sent.clear()
        conn.execute(statement, params or {})
        assert sent == [_wire_sql(conn.dialect, statement, params)[1]]
