.. _ref_cheatsheet_insert:

Insert
======

.. note::

    The types used in these queries are defined :ref:`here
    <ref_cheatsheet_types>`.

Insert basic movie stub:

.. code-block:: edgeql

    INSERT Movie {
        title := 'Dune',
        year := 2020,
        image := 'dune2020.jpg',
        directors := (
            SELECT Person
            FILTER
                .last_name = 'Villeneuve'
            # the LIMIT is needed to satisfy the single
            # link requirement validation
            LIMIT 1
        )
    }
