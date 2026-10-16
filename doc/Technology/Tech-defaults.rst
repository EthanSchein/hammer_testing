.. _tech-defaults:

Hammer Tech defaults.yml
===============================

The ``defaults.yml`` for a technology specifies some technology-specific :ref:`config` that should be left as default unless you desire to override them. Some of them work directly with the keys in the ``tech.json``.

Most of the keys in the ``defaults.yml`` are a part of the ``vlsi`` and ``technology`` namespaces. An example of the setup of the ``defaults.yml`` is located in ``hammer/technology/asap7/defaults.yml`` and certain important keys should be common to most technology plugins:

* ``vlsi.core.node`` defines the node that the place-and-route tool expects. It affects what kind of licenses are needed.
* ``vlsi.inputs`` should at least have the nominal supplies and a typical pair of characterized setup & hold corners.
* ``vlsi.technology`` needs to specify a ``placement_site`` as defined in the technology LEF, a ``bump_block_cut_layer`` to set blockages under bumps, and optional ``tap_cell_interval`` and ``tap_cell_offset`` for placing well taps.
* ``technology.core`` needs to specify the stackup to use and which layer the standard cell power rails are on. Innovus draws the lowest layer power rails over the tap cells in the ``special_cells`` list of the ``tech.json``.

Tool environment variables (commonly needed for DRC/LVS decks) and other necessary default options specific to the technology should be set in this file. This file can also define technology-specific Hammer IR, in the namespace ``technology.<tech_name>.<key_name>``. As always, they can be overridden by other snippets of Hammer IR.

.. note::
    In SledgeHammer, the dependency check and the PD cache treat every key outside the stage namespaces (``synthesis.*``, ``par.*``, ``drc.*``, ``lvs.*``, ``sim.*`` and so on) as an input to every stage, including the ``technology.<tech_name>.*``, ``vlsi.*`` and ``mentor.*`` keys set in this file. Changing one, even a value that only DRC or LVS reads (for example ``technology.sky130.drc_deck``), marks every stage to rerun and gives ``syn`` and ``par`` a new PD cache key, so neither is skipped or restored from a result cached under the old value. The exceptions are SledgeHammer's own run-control keys (the ``vlsi.pd_cache``, ``vlsi.substep_resume`` and ``vlsi.error_scan`` settings declared in ``hammer/config/defaults.yml``, and the four ``vlsi.core.airflow_*`` keys): neither the check nor the cache key reads them, so setting one here never reruns or re-keys a stage. See `How the cache key is built <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/PD_STORE_README.md#how-the-cache-key-is-built>`_.

The data types for all keys in ``defaults.yml`` can be found in ``defaults_types.yml``. When adding or overriding to ``defaults.yml``, make sure that said data types are updated accordingly to prevent problems with the type checker.
