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

Run regressions with::

    python -m pytest -q tests/unit
    VERTICA_TEST_URL=vertica+vertica_python://dbadmin@localhost:5433/sc121481 python -m pytest -q tests/live

The live tests supplement, not replace, Shell's unchanged SC-121481 contract.
They require its native fixture. SQLAlchemy 2.0.52 / vertica-python 0.10.2 and
Vertica 25.4.0-0 are the tested combination. No full write/transaction/TLS or
Superset application qualification is claimed.

Jenkins publishes immutable wheels to the existing Preset package bucket using
the same ``ci-user`` credential as ``preset-io/sqlalchemy-drill``. PR builds use
``0.6.3.1+pr.<number>.<commit>``; only reviewed master may publish a stable version.
Consumers must pin the actual published wheel URL and SHA256, not assume that a
version declared here has already been released.
