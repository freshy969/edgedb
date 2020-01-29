.. _ref_cheatsheet_admin:

Admin Commands
==============

Create a database:

.. code-block:: edgeql-repl

    db> CREATE DATABASE my_new_project;
    CREATE

Create a role:

.. code-block:: edgeql-repl

    db> CREATE SUPERUSER ROLE project;
    CREATE

Configure passwordless access (such as to a local development database):

.. code-block:: edgeql-repl

    db> CONFIGURE SYSTEM INSERT Auth {
    ...     priority := 0,
    ...     method := (INSERT Trust),
    ... }
    CONFIGURE SYSTEM

Configure a port for accessing ``my_new_project`` database using EdgeQL:

.. code-block:: edgeql-repl

    db> CONFIGURE SYSTEM INSERT Port {
    ...     protocol := "edgeql+http",
    ...     database := "my_new_project",
    ...     address := "127.0.0.1",
    ...     port := 8888,
    ...     user := "http",
    ...     concurrency := 4,
    ... };
    CONFIGURE SYSTEM

Configure a port for accessing ``my_new_project`` database using GraphQL:

.. code-block:: edgeql-repl

    db> CONFIGURE SYSTEM INSERT Port {
    ...     protocol := "graphql+http",
    ...     database := "my_new_project",
    ...     address := "127.0.0.1",
    ...     port := 8888,
    ...     user := "http",
    ...     concurrency := 4,
    ... };
    CONFIGURE SYSTEM
