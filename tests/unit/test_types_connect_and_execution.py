"""Regressions for reflected types, connection options and statement execution."""
import ssl
from pathlib import Path
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from sqla_vertica_python import vertica_python as vp
from sqla_vertica_python.vertica_python import VerticaDialect

# Public certificate of a throwaway CA generated for these tests; no key.
TEST_CA = Path(__file__).with_name('data') / 'throwaway-test-ca.pem'


def _vp(name):
    # Resolved lazily so a dialect without the type fails its test, not collection.
    return getattr(vp, name, None)


def _type(data_type):
    return VerticaDialect()._resolve_type('c', data_type)


@pytest.mark.parametrize('declared', ['int', 'integer', 'bigint', 'int8', 'smallint', 'tinyint'])
def test_every_integer_type_reflects_as_bigint(declared):
    # Vertica integers are all 64-bit and the catalog reports them as 'int'.
    assert type(_type(declared)) is sa.BIGINT


# data_type strings reported by v_catalog.columns on Vertica 25.4 for one
# column of every type family.
CATALOG_TYPES = [
    ('boolean', sa.BOOLEAN, {}),
    ('int', sa.BIGINT, {}),
    ('float', sa.DOUBLE_PRECISION, {}),
    ('char(3)', sa.CHAR, {'length': 3}),
    ('varchar(20)', sa.VARCHAR, {'length': 20}),
    ('long varchar(100)', _vp('LONG_VARCHAR'), {'length': 100}),
    ('date', sa.DATE, {}),
    ('time(3)', postgresql.TIME, {'precision': 3, 'timezone': False}),
    ('timetz(2)', postgresql.TIME, {'precision': 2, 'timezone': True}),
    ('timestamp(4)', postgresql.TIMESTAMP, {'precision': 4, 'timezone': False}),
    ('timestamptz(6)', postgresql.TIMESTAMP, {'precision': 6, 'timezone': True}),
    ('timestamp', postgresql.TIMESTAMP, {'precision': None, 'timezone': False}),
    ('interval(3)', postgresql.INTERVAL, {'precision': 3, 'fields': None}),
    ('interval year to month', postgresql.INTERVAL, {'fields': 'YEAR TO MONTH'}),
    ('interval hour', postgresql.INTERVAL, {'fields': 'HOUR'}),
    ('numeric(10,2)', sa.NUMERIC, {'precision': 10, 'scale': 2}),
    ('numeric(37,15)', sa.NUMERIC, {'precision': 37, 'scale': 15}),
    ('binary(2)', sa.BINARY, {'length': 2}),
    ('varbinary(9)', sa.VARBINARY, {'length': 9}),
    ('long varbinary(50)', _vp('LONG_VARBINARY'), {'length': 50}),
    ('uuid', sa.UUID, {}),
    ('geometry(100)', _vp('GEOMETRY'), {'length': 100}),
    ('geography(1048576)', _vp('GEOGRAPHY'), {'length': 1048576}),
]


@pytest.mark.parametrize('data_type, expected, attrs', CATALOG_TYPES)
def test_catalog_types_reflect_without_warnings(recwarn, data_type, expected, attrs):
    reflected = _type(data_type)
    assert type(reflected) is expected
    for name, value in attrs.items():
        assert getattr(reflected, name) == value
    assert not [w for w in recwarn if issubclass(w.category, sa.exc.SAWarning)]


def test_collections_and_rows():
    array = _type('array[varchar(10)](65000)')
    assert isinstance(array, sa.ARRAY) and array.item_type.length == 10
    nested = _type('ARRAY[ARRAY[int]](65000)')
    assert isinstance(nested.item_type, sa.BIGINT) and nested.dimensions == 2
    assert isinstance(_type('set[int8](65000)').item_type, sa.BIGINT)
    row = _type('ROW(a int,b varchar(5))')
    assert isinstance(row, vp.ROW) and row.get_col_spec() == 'ROW(a int,b varchar(5))'
    assert isinstance(_type('ARRAY[ROW(x int)](65000)').item_type, vp.ROW)


def test_vertica_specific_types_compile():
    dialect = VerticaDialect()
    compile_ = lambda t: t.compile(dialect=dialect)
    assert compile_(vp.LONG_VARCHAR(100)) == 'LONG VARCHAR(100)'
    assert compile_(vp.LONG_VARBINARY(50)) == 'LONG VARBINARY(50)'
    assert compile_(vp.GEOMETRY(100)) == 'GEOMETRY(100)'
    assert compile_(sa.ARRAY(sa.BIGINT, dimensions=2)) == 'ARRAY[ARRAY[BIGINT]]'


def test_foreign_keys_are_reflected_in_key_order():
    connection = Mock()
    connection.scalar.return_value = 'public'
    connection.execute.return_value.all.return_value = [
        ('fk_a', 'pid', 'public', 'parent', 'id'),
        ('fk_a', 'pk2', 'public', 'parent', 'k2'),
        ('fk_b', 'oid', 'other', 'orders', 'id'),
    ]
    dialect = VerticaDialect()
    assert dialect.get_foreign_keys(connection, 'child') == [
        {'name': 'fk_a', 'constrained_columns': ['pid', 'pk2'], 'referred_schema': None,
         'referred_table': 'parent', 'referred_columns': ['id', 'k2'], 'options': {}},
        {'name': 'fk_b', 'constrained_columns': ['oid'], 'referred_schema': 'other',
         'referred_table': 'orders', 'referred_columns': ['id'], 'options': {}},
    ]
    query = ' '.join(str(connection.execute.call_args.args[0]).lower().split())
    assert 'from v_catalog.foreign_keys' in query
    assert query.endswith('order by constraint_name, cast(ordinal_position as integer)')
    assert connection.execute.call_args.args[1] == {'table_name': 'child', 'schema': 'public'}
    # With an explicit schema the referred schema is always reported.
    assert dialect.get_foreign_keys(connection, 'child', schema='public')[0][
        'referred_schema'] == 'public'


def test_table_options_are_empty_not_an_error():
    assert VerticaDialect().get_table_options(Mock(), 't', schema='s') == {}
    assert VerticaDialect.get_multi_table_options is not None


def test_isolation_level_round_trip():
    dialect = VerticaDialect()
    cursor = Mock()
    connection = Mock(autocommit=False)
    connection.cursor.return_value = cursor
    cursor.fetchone.return_value = ('transaction_isolation', 'Read Committed')
    assert dialect.get_isolation_level(connection) == 'READ COMMITTED'
    cursor.execute.assert_called_once_with('SHOW TRANSACTION_ISOLATION')
    assert 'AUTOCOMMIT' in dialect.get_isolation_level_values(connection)
    dialect.set_isolation_level(connection, 'SERIALIZABLE')
    assert connection.autocommit is False
    cursor.execute.assert_called_with(
        'SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL SERIALIZABLE')
    dialect.set_isolation_level(connection, 'AUTOCOMMIT')
    assert connection.autocommit is True
    assert dialect.detect_autocommit_setting(connection) is True


def test_positional_paramstyle_is_forced():
    engine = sa.create_engine('vertica+vertica_python://user@localhost/db')
    assert engine.dialect.paramstyle == 'format'
    compiled = sa.text('SELECT :a, :b').bindparams(a='x :b', b='y').compile(engine)
    assert str(compiled) == 'SELECT %s, %s'
    assert compiled.positiontup == ['a', 'b']
    engine.dispose()
    for unsafe in ('named', 'pyformat', 'numeric'):
        with pytest.raises(sa.exc.ArgumentError, match='unsafe'):
            sa.create_engine('vertica+vertica_python://user@localhost/db', paramstyle=unsafe)
    assert VerticaDialect.supports_multivalues_insert is False


def test_binary_parameters_are_hex_literals():
    assert vp._binary_literal(b'\x00\xff') == "X'00ff'"
    assert vp._binary_literal(memoryview(b'ab')) == "X'6162'"
    assert VerticaDialect.import_dbapi().Binary is bytes
    cursor = Mock()
    VerticaDialect().do_execute(cursor, 'SELECT %s', (b'\x00',))
    registered = {call.args[0] for call in cursor.register_sql_literal_adapter.call_args_list}
    assert registered == {bytes, bytearray, memoryview}
    cursor.execute.assert_called_once_with('SELECT %s', (b'\x00',))


@pytest.mark.parametrize('statement, rows, copy', [
    ('INSERT INTO s.t (a, b) VALUES (%s, %s)', [(1, 'x'), (2, 'y')], True),
    ('INSERT INTO s.t (a, b) VALUES (%s, now())', [(1,), (2,)], False),
    ('INSERT INTO s.t (a, b) VALUES (%s, %s)', [(1, b'\x00'), (2, None)], False),
    ('UPDATE s.t SET a = %s WHERE b = %s', [(1, 'x'), (2, 'y')], False),
])
def test_executemany_only_uses_copy_for_plain_placeholder_inserts(statement, rows, copy):
    cursor = Mock()
    VerticaDialect().do_executemany(cursor, statement, rows)
    if copy:
        cursor.executemany.assert_called_once_with(statement, rows)
        cursor.execute.assert_not_called()
    else:
        cursor.executemany.assert_not_called()
        assert [c.args for c in cursor.execute.call_args_list] == [(statement, r) for r in rows]


def test_connection_errors_are_disconnects():
    dialect = VerticaDialect()
    dialect.loaded_dbapi = dbapi = VerticaDialect.import_dbapi()
    open_connection = Mock(**{'closed.return_value': False})
    lost = dbapi.errors.ConnectionError('Connection closed by Vertica')
    assert dialect.is_disconnect(lost, open_connection, None)
    assert not dialect.is_disconnect(dbapi.errors.QueryError.__new__(dbapi.errors.QueryError),
                                     open_connection, None)
    assert not dialect.is_disconnect(ValueError('x'), open_connection, None)


@pytest.mark.parametrize('status, called', [
    ('no_transaction', False), ('in_transaction', True), ('failed_transaction', True), (None, True),
])
def test_commit_and_rollback_skip_only_idle_connections(status, called):
    connection = Mock(transaction_status=status)
    VerticaDialect().do_rollback(connection)
    VerticaDialect().do_commit(connection)
    assert connection.rollback.called is called
    assert connection.commit.called is called


def test_url_options_are_typed():
    options = vp._normalize_connect_options({
        'ssl': 'false', 'autocommit': 'off', 'connection_timeout': '5',
        'backup_server_node': 'a:5433, b', 'log_level': '10', 'session_label': 'x',
    })
    assert options == {
        'ssl': False, 'autocommit': False, 'connection_timeout': 5.0,
        'backup_server_node': ['a:5433', 'b'], 'log_level': 10, 'session_label': 'x',
    }
    with pytest.raises(sa.exc.ArgumentError, match="'ssl' must be a boolean"):
        vp._normalize_connect_options({'ssl': 'maybe'})
    with pytest.raises(sa.exc.ArgumentError, match='tlsmode must be one of'):
        vp._normalize_connect_options({'tlsmode': 'verify'})


def test_verified_tls_builds_a_trusting_context():
    options = vp._normalize_connect_options({'tlsmode': 'VERIFY-FULL'})
    context = options.pop('ssl')
    assert options == {}
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    # The system trust store is loaded (vertica-python's own context is empty).
    assert context.cert_store_stats()['x509_ca'] > 0 or ssl.get_default_verify_paths().cafile is None

    pem = TEST_CA.read_text()
    for extra in ({'tls_cadata': pem}, {'tls_cafile': str(TEST_CA)}):
        context = vp._normalize_connect_options({'tlsmode': 'verify-ca', **extra})['ssl']
        assert context.verify_mode == ssl.CERT_REQUIRED and not context.check_hostname
        assert context.cert_store_stats()['x509_ca'] >= 1

    assert vp._normalize_connect_options({'tlsmode': 'require'}) == {'tlsmode': 'require'}
    with pytest.raises(sa.exc.ArgumentError, match='tls_cadata requires'):
        vp._normalize_connect_options({'tlsmode': 'require', 'tls_cadata': pem})
    with pytest.raises(sa.exc.ArgumentError, match='not both'):
        vp._normalize_connect_options({'tlsmode': 'require', 'ssl': ssl.create_default_context()})


def test_connect_normalizes_url_and_connect_args():
    dialect = VerticaDialect()
    dialect.loaded_dbapi = Mock()
    dialect.connect(host='h', ssl='0', tlsmode='disable')
    dialect.loaded_dbapi.connect.assert_called_once_with(host='h', ssl=False, tlsmode='disable')
