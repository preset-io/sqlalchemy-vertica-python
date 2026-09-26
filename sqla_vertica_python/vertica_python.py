import re
import ssl

from sqlalchemy import text, exc, util
from sqlalchemy import types as sqltypes
from sqlalchemy.dialects.postgresql.base import PGDialect, PGTypeCompiler
from sqlalchemy.dialects.postgresql import INTERVAL, TIME, TIMESTAMP
from sqlalchemy.engine import reflection
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.schema import CreateColumn
from sqlalchemy.ext.compiler import compiles


# From Postgresql 10 IDENTITY columns section in sqlalchemy/dialects/postgresql/base.py
@compiles(CreateColumn, 'vertica')
def use_identity(element, compiler, **kw):
    text = compiler.visit_create_column(element, **kw)
    text = text.replace("SERIAL", "IDENTITY(1,1)")
    return text


class LONG_VARCHAR(sqltypes.VARCHAR):
    __visit_name__ = 'LONG_VARCHAR'


class LONG_VARBINARY(sqltypes.VARBINARY):
    __visit_name__ = 'LONG_VARBINARY'


class _VerticaSpatial(sqltypes.UserDefinedType):
    """Vertica spatial column; values are Vertica's own serialization.

    Use ST_AsText()/ST_GeomFromText() in SQL to exchange readable values.
    """

    cache_ok = True
    type_name = None

    def __init__(self, length=None):
        self.length = length

    def get_col_spec(self, **kw):
        if self.length is None:
            return self.type_name
        return '%s(%d)' % (self.type_name, self.length)


class GEOMETRY(_VerticaSpatial):
    type_name = 'GEOMETRY'


class GEOGRAPHY(_VerticaSpatial):
    type_name = 'GEOGRAPHY'


class ROW(sqltypes.UserDefinedType):
    """Vertica ROW (struct) column, reflected with its declared definition."""

    cache_ok = True

    def __init__(self, definition):
        self.definition = definition

    def get_col_spec(self, **kw):
        return self.definition


# Vertica's VARCHAR/VARBINARY default to 80 bytes when no length is given;
# SQLAlchemy's unbounded String means "no practical limit".
MAX_VARCHAR_LENGTH = 65000


def _sized(name, length):
    return name + ('(%d)' % length if length else '')


class VerticaTypeCompiler(PGTypeCompiler):
    """PostgreSQL DDL types Vertica lacks (TEXT, BYTEA as large binary, JSON,
    NVARCHAR, CLOB/BLOB) are rendered as their Vertica equivalents."""

    def visit_LONG_VARCHAR(self, type_, **kw):
        return _sized('LONG VARCHAR', type_.length)

    def visit_LONG_VARBINARY(self, type_, **kw):
        return _sized('LONG VARBINARY', type_.length)

    def visit_VARCHAR(self, type_, **kw):
        return 'VARCHAR(%d)' % (type_.length or MAX_VARCHAR_LENGTH)

    visit_NVARCHAR = visit_VARCHAR

    def visit_NCHAR(self, type_, **kw):
        return _sized('CHAR', type_.length)

    def visit_TEXT(self, type_, **kw):
        return self.visit_LONG_VARCHAR(type_, **kw)

    visit_CLOB = visit_TEXT

    def visit_JSON(self, type_, **kw):
        # No JSON column type; SQLAlchemy's JSON serializes to text.
        return 'LONG VARCHAR'

    def visit_large_binary(self, type_, **kw):
        return self.visit_LONG_VARBINARY(type_, **kw)

    visit_BLOB = visit_large_binary

    def visit_VARBINARY(self, type_, **kw):
        return 'VARBINARY(%d)' % (type_.length or MAX_VARCHAR_LENGTH)

    def visit_ARRAY(self, type_, **kw):
        inner = self.process(type_.item_type, **kw)
        for _ in range(type_.dimensions or 1):
            inner = 'ARRAY[%s]' % inner
        return inner


# Options vertica-python only accepts with a specific Python type. Values
# taken from a URL query string arrive as text and must be converted, or the
# driver silently treats any nonempty string (including "false") as true.
_BOOLEAN_OPTIONS = frozenset((
    'autocommit', 'binary_transfer', 'connection_load_balance',
    'disable_copy_local', 'request_complex_types', 'ssl',
    'use_prepared_statements',
))
_TRUE = frozenset(('1', 'on', 't', 'true', 'y', 'yes'))
_FALSE = frozenset(('0', 'off', 'f', 'false', 'n', 'no'))
_TLS_MODES = ('disable', 'prefer', 'require', 'verify-ca', 'verify-full')


def _coerce_bool(key, value):
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise exc.ArgumentError(
        'Vertica connection option %r must be a boolean, not %r' % (key, value))


def _coerce_backup_nodes(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return [node.strip() for node in str(value).split(',') if node.strip()]


def _normalize_connect_options(options):
    """Type-check vertica-python options from the URL or ``connect_args``.

    ``tlsmode`` ``verify-ca``/``verify-full`` are turned into an SSLContext
    here: vertica-python's own context trusts only ``tls_cafile`` and so
    rejects every certificate when none is given. This one uses the system
    trust store unless ``tls_cafile`` or ``tls_cadata`` (PEM text, usable from
    JSON configuration) supply the CA.
    """
    options = dict(options)
    for key in _BOOLEAN_OPTIONS & set(options):
        if key == 'ssl' and isinstance(options[key], ssl.SSLContext):
            continue
        options[key] = _coerce_bool(key, options[key])
    for key in ('connection_timeout',):
        if key in options and options[key] is not None:
            options[key] = float(options[key])
    for key in ('port', 'log_level'):
        if isinstance(options.get(key), str) and options[key].strip().isdigit():
            options[key] = int(options[key])
    if 'backup_server_node' in options:
        options['backup_server_node'] = _coerce_backup_nodes(options['backup_server_node'])

    cadata = options.pop('tls_cadata', None)
    tlsmode = options.get('tlsmode')
    if tlsmode is not None:
        tlsmode = str(tlsmode).strip().lower()
        if tlsmode not in _TLS_MODES:
            raise exc.ArgumentError(
                'Vertica tlsmode must be one of %s, not %r' % (', '.join(_TLS_MODES), tlsmode))
        options['tlsmode'] = tlsmode
    if tlsmode is not None and isinstance(options.get('ssl'), ssl.SSLContext):
        # vertica-python would silently ignore the context.
        raise exc.ArgumentError('Pass either tlsmode or an ssl.SSLContext, not both')
    if tlsmode in ('verify-ca', 'verify-full'):
        context = ssl.create_default_context(
            cafile=options.pop('tls_cafile', None), cadata=cadata)
        context.check_hostname = tlsmode == 'verify-full'
        context.verify_mode = ssl.CERT_REQUIRED
        certfile = options.pop('tls_certfile', None)
        keyfile = options.pop('tls_keyfile', None)
        if certfile or keyfile:
            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        # An SSLContext means "require TLS with this context" to the driver;
        # tlsmode would take precedence and discard it.
        del options['tlsmode']
        options['ssl'] = context
    elif cadata is not None:
        raise exc.ArgumentError(
            "tls_cadata requires tlsmode 'verify-ca' or 'verify-full'")
    return options


# An INSERT whose VALUES are all plain placeholders; only these may use the
# driver's executemany, which rewrites the INSERT into COPY ... FROM STDIN and
# sends each placeholder's value as literal COPY data.
_COPY_SAFE_INSERT = re.compile(
    r'^\s*INSERT\s+INTO\s+[^()]+\([^()]*\)\s*VALUES\s*\(\s*%s(?:\s*,\s*%s)*\s*\)\s*$',
    re.I)
_BINARY_TYPES = (bytes, bytearray, memoryview)


def _binary_literal(value):
    return "X'%s'" % bytes(value).hex()


def _split_type_spec(data_type):
    """Split 'name(args)' into (NAME, [args]); nested brackets are preserved."""
    m = re.match(r'^\s*(\w[\w ]*?)\s*(?:\((\d+)(?:\s*,\s*(\d+))?\))?\s*$', data_type)
    if not m:
        return None, []
    return m.group(1).upper(), [int(g) for g in m.group(2, 3) if g is not None]


class VerticaDialect(PGDialect):
    """ Vertica Dialect using a vertica-python connection and PGDialect """

    name = 'vertica'
    driver = 'vertica_python'

    # PostgreSQL bulk reflection queries pg_catalog, which Vertica does not
    # implement. Use SQLAlchemy's supported per-table reflection adapter for
    # *every* bulk API, including those used by Table/MetaData autoload.
    get_multi_columns = DefaultDialect.get_multi_columns
    get_multi_pk_constraint = DefaultDialect.get_multi_pk_constraint
    get_multi_foreign_keys = DefaultDialect.get_multi_foreign_keys
    get_multi_indexes = DefaultDialect.get_multi_indexes
    get_multi_unique_constraints = DefaultDialect.get_multi_unique_constraints
    get_multi_check_constraints = DefaultDialect.get_multi_check_constraints
    get_multi_table_comment = DefaultDialect.get_multi_table_comment
    get_multi_table_options = DefaultDialect.get_multi_table_options

    # These optional PostgreSQL reflection APIs must not issue pg_catalog SQL
    # either. The base dialect reports unsupported operations explicitly.
    get_materialized_view_names = DefaultDialect.get_materialized_view_names
    get_temp_table_names = DefaultDialect.get_temp_table_names
    get_temp_view_names = DefaultDialect.get_temp_view_names

    supports_statement_cache = False

    # Vertica has no CREATE TYPE ... AS ENUM; store enums as VARCHAR.
    supports_native_enum = False

    # vertica-python substitutes named (:name) parameters with one regex
    # replacement per parameter over the whole statement, so a value that
    # contains ":other" is rewritten by the next substitution -- corrupting
    # data and allowing SQL injection. Positional %s parameters are
    # substituted in a single pass; "qmark" is for use_prepared_statements.
    default_paramstyle = 'format'
    _safe_paramstyles = ('format', 'qmark')

    def __init__(self, paramstyle=None, **kwargs):
        if paramstyle is not None and paramstyle not in self._safe_paramstyles:
            raise exc.ArgumentError(
                'Vertica paramstyle must be one of %s; %r is unsafe with vertica-python'
                % (', '.join(self._safe_paramstyles), paramstyle))
        super().__init__(paramstyle=paramstyle or self.default_paramstyle, **kwargs)

    # Vertica has no RETURNING; SQLAlchemy 2 uses these flags even before
    # initialize(), rather than the legacy implicit_returning setting.
    insert_returning = False
    update_returning = False
    delete_returning = False

    # UPDATE functionality works with the following option set to False
    supports_sane_rowcount = False

    supports_unicode_statements = True
    supports_unicode_binds = True
    supports_native_decimal = True

    type_compiler_cls = VerticaTypeCompiler
    type_compiler = VerticaTypeCompiler

    # Keys are the type names v_catalog.columns.data_type reports (upper-cased,
    # without arguments), plus legacy spellings kept for compatibility.
    ischema_names = {
        'BINARY': sqltypes.BINARY,
        'VARBINARY': sqltypes.VARBINARY,
        'LONG VARBINARY': LONG_VARBINARY,
        'BYTEA': sqltypes.VARBINARY,
        'RAW': sqltypes.VARBINARY,

        'BOOLEAN': sqltypes.BOOLEAN,

        'CHAR': sqltypes.CHAR,
        'VARCHAR': sqltypes.VARCHAR,
        'LONG VARCHAR': LONG_VARCHAR,
        'VARCHAR2': sqltypes.VARCHAR,
        'TEXT': sqltypes.VARCHAR,
        'UUID': sqltypes.UUID,

        'DATE': sqltypes.DATE,
        'DATETIME': TIMESTAMP,
        'SMALLDATETIME': TIMESTAMP,
        'TIME': TIME,
        'TIMETZ': TIME,
        'TIME WITH TIMEZONE': TIME,
        'TIMESTAMP': TIMESTAMP,
        'TIMESTAMPTZ': TIMESTAMP,
        'TIMESTAMP WITH TIMEZONE': TIMESTAMP,

        'INTERVAL': INTERVAL,

        # Vertica FLOAT, FLOAT8, DOUBLE PRECISION and REAL are all 64-bit.
        'FLOAT': sqltypes.DOUBLE_PRECISION,
        'FLOAT8': sqltypes.DOUBLE_PRECISION,
        'DOUBLE': sqltypes.DOUBLE_PRECISION,
        'DOUBLE PRECISION': sqltypes.DOUBLE_PRECISION,
        'REAL': sqltypes.DOUBLE_PRECISION,

        # Every Vertica integer type is a signed 64-bit INTEGER, and the
        # catalog reports all of them as 'int'; reflect the real width.
        'INT': sqltypes.BIGINT,
        'INTEGER': sqltypes.BIGINT,
        'INT8': sqltypes.BIGINT,
        'BIGINT': sqltypes.BIGINT,
        'SMALLINT': sqltypes.BIGINT,
        'TINYINT': sqltypes.BIGINT,

        'NUMERIC': sqltypes.NUMERIC,
        'DECIMAL': sqltypes.NUMERIC,
        'NUMBER': sqltypes.NUMERIC,
        'MONEY': sqltypes.NUMERIC,

        'GEOMETRY': GEOMETRY,
        'GEOGRAPHY': GEOGRAPHY,
    }

    _isolation_lookup = {
        'READ COMMITTED', 'READ UNCOMMITTED', 'REPEATABLE READ', 'SERIALIZABLE',
    }

    # skip all the version-specific stuff in PGDialect's initialize method (Vertica versions don't match feature-wise)
    def initialize(self, connection):
        super(PGDialect, self).initialize(connection)

    def get_isolation_level_values(self, dbapi_connection):
        return ['AUTOCOMMIT'] + sorted(self._isolation_lookup)

    def set_isolation_level(self, dbapi_connection, level):
        if level == 'AUTOCOMMIT':
            dbapi_connection.autocommit = True
            return
        dbapi_connection.autocommit = False
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(
                'SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL %s' % level)
        finally:
            cursor.close()

    def get_isolation_level(self, dbapi_connection):
        # SHOW returns (name, setting), e.g. ('transaction_isolation', 'READ COMMITTED').
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('SHOW TRANSACTION_ISOLATION')
            return cursor.fetchone()[-1].upper()
        finally:
            cursor.close()

    def detect_autocommit_setting(self, dbapi_connection):
        return bool(dbapi_connection.autocommit)

    def connect(self, *cargs, **cparams):
        # URL query options and connect_args both arrive here.
        return self.loaded_dbapi.connect(*cargs, **_normalize_connect_options(cparams))

    def is_disconnect(self, e, connection, cursor):
        if not isinstance(e, self.loaded_dbapi.Error):
            return False
        # Raised for a dropped socket, "Connection closed by Vertica" (e.g.
        # after CLOSE_SESSION) and use of a closed connection.
        if isinstance(e, self.loaded_dbapi.errors.ConnectionError):
            return True
        return connection is not None and connection.closed()

    @staticmethod
    def _register_binary_adapters(cursor):
        register = getattr(cursor, 'register_sql_literal_adapter', None)
        if register is not None:
            for binary_type in _BINARY_TYPES:
                register(binary_type, _binary_literal)

    def do_execute(self, cursor, statement, parameters, context=None):
        # vertica-python decodes bytes parameters as UTF-8 text; bind them as
        # hexadecimal VARBINARY literals instead.
        self._register_binary_adapters(cursor)
        cursor.execute(statement, parameters)

    def do_executemany(self, cursor, statement, parameters, context=None):
        rows = list(parameters)
        if _COPY_SAFE_INSERT.match(statement) and not any(
                isinstance(value, _BINARY_TYPES) for row in rows for value in row):
            cursor.executemany(statement, rows)
            return
        # SQL expressions in VALUES or binary values cannot go through the
        # COPY rewrite; execute them one by one instead.
        for row in rows:
            self.do_execute(cursor, statement, row, context)

    @classmethod
    def import_dbapi(cls):
        vp_module = __import__('vertica_python')

        # sqlalchemy expects to find the base Error class here,
        # so we need to alias it
        vp_module.Error = vp_module.errors.Error
        # SQLAlchemy's LargeBinary binds through the DB-API Binary constructor,
        # which vertica-python does not define; it binds bytes natively.
        if not hasattr(vp_module, 'Binary'):
            vp_module.Binary = bytes

        return vp_module
    
    dbapi = import_dbapi


    def create_connect_args(self, url):
        opts = url.translate_connect_args(username='user')
        opts.update(url.query)
        return [[], opts]


    def has_schema(self, connection, schema):
        query = ("SELECT EXISTS (SELECT schema_name FROM v_catalog.schemata "
                 "WHERE schema_name = :schema)")
        rs = connection.execute(text(query), {"schema": schema})
        return bool(rs.scalar())


    @reflection.cache
    def has_table(self, connection, table_name, schema=None, **kw):
        if schema is None:
            schema = self._get_default_schema_name(connection)
        query = ("SELECT EXISTS ("
                 "SELECT table_name FROM v_catalog.all_tables "
                 "WHERE schema_name = :schema AND "
                 "table_name = :table_name"
                 ")")
        rs = connection.execute(
            text(query), {"schema": schema, "table_name": table_name})
        return bool(rs.scalar())


    def has_sequence(self, connection, sequence_name, schema=None):
        if schema is None:
            schema = self._get_default_schema_name(connection)
        query = ("SELECT EXISTS ("
                 "SELECT sequence_name FROM v_catalog.sequences "
                 "WHERE sequence_schema = :schema AND "
                 "sequence_name = :sequence_name"
                 ")")
        rs = connection.execute(
            text(query), {"schema": schema, "sequence_name": sequence_name})
        return bool(rs.scalar())


    def has_type(self, connection, type_name, schema=None):
        # v_catalog.types stores mixed-case names (e.g. 'Integer', 'Varchar'),
        # so compare case-insensitively.
        query = ("SELECT EXISTS ("
                 "SELECT type_name FROM v_catalog.types "
                 "WHERE LOWER(type_name) = LOWER(:type_name)"
                 ")")
        rs = connection.execute(text(query), {"type_name": type_name})
        return bool(rs.scalar())


    def _get_server_version_info(self, connection):
        v = connection.scalar(text("select version()"))
        m = re.match(
            r'.*Vertica Analytic Database '
            r'v(\d+)\.(\d+)\.(\d+).*',
            v)
        if not m:
            raise AssertionError(
                "Could not determine version from string '%s'" % v)
        return tuple([int(x) for x in m.group(1, 2, 3) if x is not None])


    def _get_default_schema_name(self, connection):
        return connection.scalar(text("select current_schema()"))

    def _schema_or_default(self, connection, schema):
        # SQLAlchemy's contract: schema=None means the default schema, never
        # "every schema"; otherwise same-named tables in different schemas
        # are merged into one result.
        if schema is not None:
            return schema
        return self.default_schema_name or self._get_default_schema_name(connection)


    @reflection.cache
    def get_schema_names(self, connection, **kw):
        query = "SELECT schema_name FROM v_catalog.schemata ORDER BY schema_name"
        rs = connection.execute(text(query))
        return [row[0] for row in rs if not row[0].startswith('v_')]


    @reflection.cache
    def get_table_comment(self, connection, table_name, schema=None, **kw):
        schema = self._schema_or_default(connection, schema)
        query = """
        SELECT comment FROM v_catalog.comments WHERE object_type = 'TABLE'
        AND object_name = :table_name
        AND object_schema = :schema
        """
        rs = connection.execute(
            text(query), {"table_name": table_name, "schema": schema})
        return {"text": rs.scalar()}


    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        query = ("SELECT table_name FROM v_catalog.tables "
                 "WHERE table_schema = :schema ORDER BY table_name")
        rs = connection.execute(
            text(query), {"schema": self._schema_or_default(connection, schema)})
        return [row[0] for row in rs]


    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        query = ("SELECT table_name FROM v_catalog.views "
                 "WHERE table_schema = :schema ORDER BY table_name")
        rs = connection.execute(
            text(query), {"schema": self._schema_or_default(connection, schema)})
        return [row[0] for row in rs]

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        schema = self._schema_or_default(connection, schema)
        params = {"table_name": table_name, "schema": schema}

        pk_column_select = """
        SELECT column_name FROM v_catalog.primary_keys
        WHERE table_name = :table_name
        AND constraint_type = 'p'
        AND table_schema = :schema
        """
        primary_key_columns = tuple(
            row[0] for row in connection.execute(text(pk_column_select), params))
        column_select = """
        SELECT
          column_name,
          data_type,
          column_default,
          is_nullable,
          is_identity,
          ordinal_position
        FROM v_catalog.columns
        where table_name = :table_name
        AND table_schema = :schema
        UNION ALL
        SELECT
          column_name,
          data_type,
          NULL as column_default,
          true as is_nullable,
          false as is_identity,
          ordinal_position
        FROM v_catalog.view_columns
        where table_name = :table_name
        AND table_schema = :schema
        ORDER BY ordinal_position ASC
        """
        colobjs = []
        column_select_results = list(connection.execute(text(column_select), params))
        if not column_select_results and not self.has_table(
                connection, table_name, schema=schema, **kw):
            raise exc.NoSuchTableError(table_name)
        for row in column_select_results:
            sequence_info = connection.execute(text("""
                SELECT
                sequence_name as name,
                minimum as start,
                increment_by as increment
                FROM v_catalog.sequences
                WHERE identity_table_name = :table_name
                AND sequence_schema = :schema
                """), params).first() if row.is_identity else None

            colobj = self._get_column_info(
                row.column_name,
                row.data_type,
                row.is_nullable,
                row.column_default,
                row.is_identity,
                (row.column_name in primary_key_columns),
                sequence_info
            )
            if colobj:
                colobjs.append(colobj)
        return colobjs

    def _resolve_type(self, name, data_type):
        """Map a v_catalog data_type string to a SQLAlchemy type instance."""
        spec = data_type.strip()
        # Collections carry a trailing maximum size: ARRAY[INT](65000).
        collection = re.match(r'^(array|set)\[(.*)\](?:\(\d+\))?$', spec, re.I | re.S)
        if collection:
            item = self._resolve_type(name, collection.group(2))
            if item is sqltypes.NULLTYPE:
                return item
            if isinstance(item, sqltypes.ARRAY):
                return sqltypes.ARRAY(item.item_type, dimensions=(item.dimensions or 1) + 1)
            return sqltypes.ARRAY(item)
        if re.match(r'^row\s*\(', spec, re.I):
            return ROW(spec)

        typename, args = _split_type_spec(spec)
        if typename is None:
            raise ValueError(
                "data type string not parseable for type name and optional parameters: %s"
                % data_type)
        if typename.startswith('INTERVAL'):
            fields = typename[len('INTERVAL'):].strip() or None
            return INTERVAL(precision=args[0] if args else None, fields=fields)
        typeobj = self.ischema_names.get(typename)
        if typeobj is None:
            util.warn(f"Did not recognize type '{typename}' of column '{name}'")
            return sqltypes.NULLTYPE
        if not isinstance(typeobj, type):
            return typeobj
        if typeobj in (TIME, TIMESTAMP):
            return typeobj(
                timezone=typename in ('TIMETZ', 'TIMESTAMPTZ') or 'ZONE' in typename,
                precision=args[0] if args else None)
        if typeobj in (sqltypes.BIGINT, sqltypes.BOOLEAN, sqltypes.DATE,
                       sqltypes.DOUBLE_PRECISION, sqltypes.UUID):
            return typeobj()
        return typeobj(*args)

    def _get_column_info(self, name, data_type, is_nullable, default, is_identity, is_primary_key, sequence):
        column_info = {
            'name': name,
            'type': self._resolve_type(name, data_type),
            'nullable': is_nullable,
            'default': default,
            'primary_key': (is_primary_key or is_identity)
        }
        if is_identity:
            column_info['autoincrement'] = True
        if sequence:
            column_info['sequence'] = dict(sequence._mapping)
        return column_info

    @reflection.cache
    def get_unique_constraints(self, connection, table_name, schema=None, **kw):
        # The catalog does not record a key position for UNIQUE constraints
        # (unlike primary_keys and foreign_keys), and constraint_columns rows
        # come back in no defined order. Order key columns by their position
        # in the table so the result is deterministic.
        query = """
        SELECT cc.constraint_name, cc.column_name
        FROM v_catalog.constraint_columns cc
        JOIN v_catalog.columns c
          ON c.table_id = cc.table_id AND c.column_name = cc.column_name
        WHERE cc.table_name = :table_name
        AND cc.table_schema = :schema
        AND cc.constraint_type = 'u'
        ORDER BY cc.constraint_name, c.ordinal_position
        """
        params = {
            "table_name": table_name,
            "schema": self._schema_or_default(connection, schema),
        }
        result = {}
        for constraint_name, column_name in connection.execute(text(query), params).all():
            result.setdefault(constraint_name, []).append(column_name)
        return [
            {"name": name, "column_names": columns}
            for name, columns in result.items()
        ]

    @reflection.cache
    def get_check_constraints(self, connection, table_name, schema=None, **kw):
        query = """
        SELECT
            cons.constraint_name as name,
            cons.predicate as src
        FROM
            v_catalog.table_constraints cons
        WHERE
            cons.constraint_type = 'c'
          AND
            cons.table_id = (
                SELECT
                    i.table_id
                FROM
                    v_catalog.tables i
                WHERE
                    i.table_name = :table_name
                AND i.table_schema = :schema
            )
        ORDER BY cons.constraint_name
        """
        params = {
            "table_name": table_name,
            "schema": self._schema_or_default(connection, schema),
        }

        return [
            {
                'name': name,
                'sqltext': src[1:-1]
            } for name, src in connection.execute(text(query), params).fetchall()
        ]

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        query = "SELECT constraint_id, constraint_name, column_name FROM v_catalog.primary_keys \n\
                 WHERE constraint_type = 'p' AND table_name = :table_name \n\
                 AND table_schema = :schema"
        params = {
            "table_name": table_name,
            "schema": self._schema_or_default(connection, schema),
        }

        # Key position lives in primary_keys, not constraint_columns.
        query += " ORDER BY CAST(ordinal_position AS INTEGER)"

        cols = []
        name = None
        for row in connection.execute(text(query), params):
             name = row[1] if name is None else name
             cols.append(row[2])

        return {"constrained_columns": cols, "name": name}

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        # Vertica records declared foreign keys even though it does not
        # enforce them; reflect them like any other dialect.
        default_schema = self.default_schema_name or self._get_default_schema_name(connection)
        query = """
        SELECT constraint_name, column_name, reference_table_schema,
               reference_table_name, reference_column_name
        FROM v_catalog.foreign_keys
        WHERE table_name = :table_name
        AND table_schema = :schema
        ORDER BY constraint_name, CAST(ordinal_position AS INTEGER)
        """
        params = {
            "table_name": table_name,
            "schema": schema if schema is not None else default_schema,
        }
        fkeys = {}
        for name, column, ref_schema, ref_table, ref_column in connection.execute(
                text(query), params).all():
            fk = fkeys.get(name)
            if fk is None:
                fk = fkeys[name] = {
                    'name': name,
                    'constrained_columns': [],
                    # SQLAlchemy convention: omit the schema when it is the
                    # default and the caller did not name one explicitly.
                    'referred_schema': (
                        None if schema is None and ref_schema == default_schema
                        else ref_schema),
                    'referred_table': ref_table,
                    'referred_columns': [],
                    'options': {},
                }
            fk['constrained_columns'].append(column)
            fk['referred_columns'].append(ref_column)
        return list(fkeys.values())


    def get_indexes(self, connection, table_name, schema=None, **kw):
        # Vertica has no indexes (storage is organized by projections).
        return []

    def get_table_options(self, connection, table_name, schema=None, **kw):
        # No MySQL-style table options exist; report none rather than raising.
        return {}


    # Disable index creation since that's not a thing in Vertica.
    def visit_create_index(self, create):
        return None
