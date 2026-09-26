sqlalchemy-vertica-python
=========================

Vertica dialect for sqlalchemy. Forked from the `Vertica ODBC dialect <https://pypi.python.org/pypi/vertica-sqlalchemy>`_, written by `James Casbon <https://github.com/jamescasbon>`_.

This module implements a Vertica dialect for SQLAlchemy using the pure-Python DB-API driver `vertica-python <https://github.com/vertica/vertica-python>`_, as adapted by `Luke Emery-Fertitta <https://github.com/lemeryfertitta>`_.

It is currently maintained by `BlueLabs <https://bluelabs.com/>`_ - PRs are welcome!

Engine creation:

.. code-block:: python

    import sqlalchemy as sa
    sa.create_engine('vertica+vertica_python://user:pwd@host:port/database')

Installation
------------

From PyPI: ::

     pip install sqlalchemy-vertica-python

From git: ::

     git clone https://github.com/bluelabsio/vertica-sqlalchemy-python
     cd vertica-sqlalchemy-python
     python setup.py install
     

Usage
------------

**ID/Primary Key Declaration**

Do not use this. The INSERT will fail as it will try to insert the ID

    id = Column(Integer, primary_key=True)

Do the following instead

    id = Column(Integer, Sequence('user_id_seq'), primary_key=True)

Preset SQLAlchemy 2 fork
-----------------------

This fork targets SQLAlchemy 2.0 and preserves the existing
``vertica+vertica_python://`` entry point. It uses the upstream 0.6.3 executable
initialization statements, and routes all bulk reflection through SQLAlchemy's
per-table adapter and Vertica's ``v_catalog`` implementations, not PostgreSQL's
``pg_catalog``. Inspector keyword arguments and missing-table errors follow the
SQLAlchemy 2 contract. It also preserves unique-constraint columns when consuming
results and uses ``Row._mapping`` for reflected identity sequences.

Reflection and execution behaviour:

* All Vertica integer types are 64-bit and reflect as ``BIGINT``. ``UUID``,
  ``GEOMETRY``/``GEOGRAPHY``, ``ARRAY``/``SET``, ``ROW``, every ``INTERVAL``
  form, ``LONG VARCHAR``/``LONG VARBINARY`` and time precision are reflected.
* ``schema=None`` means the default schema; same-named tables in other schemas
  are never merged. Declared foreign keys are reflected; unique-constraint
  columns are ordered by their position in the table (the catalog records no
  key position for UNIQUE constraints).
* Statements use positional ``%s`` parameters. vertica-python substitutes
  named parameters one at a time over the whole statement, so a value containing
  ``:other`` would be rewritten by the next substitution. ``bytes`` values are
  sent as ``X'..'`` literals. ``executemany`` uses the driver's COPY rewrite only
  for INSERTs whose VALUES are plain placeholders without binary values.
* ``isolation_level`` (including ``AUTOCOMMIT``) is supported, and a lost
  connection (for example after ``CLOSE_SESSION``) is treated as a disconnect, so
  ``pool_pre_ping`` replaces it.

Connection options from the URL query or ``connect_args`` are type-checked:
booleans such as ``ssl`` accept ``true/false/1/0/on/off/yes/no`` and reject
anything else, ``connection_timeout`` is a number and ``backup_server_node`` a
comma-separated list. ``tlsmode`` accepts ``disable``, ``prefer``, ``require``,
``verify-ca`` or ``verify-full``. For the two verifying modes the dialect uses
the system trust store unless ``tls_cafile`` (a path) or ``tls_cadata`` (PEM
text, e.g. from JSON configuration) supplies the CA; ``tls_certfile`` and
``tls_keyfile`` enable client certificates. ``ssl=true`` alone encrypts without
verifying the server certificate.

Run regressions with::

    python -m pytest -q tests/unit
    VERTICA_TEST_URL=vertica+vertica_python://dbadmin@localhost:5433/sc121481 python -m pytest -q tests/live

``tests/live/test_sqlalchemy2.py`` requires the native fixture described there;
``tests/live/test_types_and_execution.py`` creates and drops its own schemas.
SQLAlchemy 2.0.52, vertica-python 1.4.0 and Vertica 25.4.0-0 are the tested
combination; vertica-python 1.4.0 or later is required (earlier releases return
binary, ``TIMETZ`` and ``INTERVAL`` values as undecoded text and ignore
``tlsmode``).

Jenkins publishes immutable wheels to the existing Preset package bucket using
the same ``ci-user`` credential as ``preset-io/sqlalchemy-drill``. PR builds use
``<version>+pr.<number>.<commit>``; only reviewed master may publish a stable version.
Consumers must pin the actual published wheel URL and SHA256, not assume that a
version declared here has already been released.
