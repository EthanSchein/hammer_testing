.. _cad-tools:

Hammer CAD Tools
===============================

Hammer has plugins for open-source and commercial CAD tools, including the Cadence and Synopsys plugins, in the ``hammer/<action>`` folders. Plugins for Mentor (now Siemens) tools such as Calibre live in a separate repo, ``hammer-mentor-plugins``. ``hammer-mentor-plugins`` is a private repo since it contains tool-specific commands not yet cleared for public release. Access to it may be granted for Hammer users who already have licenses for those tools. See the note about :ref:`plugins access <plugins-access>` for instructions for how to request access.

The structure of each repository is as follows:

* hammer

    * ACTION

        * TOOL_NAME

            * ``__init__.py`` contains the methods needed to implement the tool
            * ``defaults.yml`` contains the default :ref:`config` needed by the tool
        
        
ACTION is the Hammer action name (e.g. ``par``, ``synthesis``, ``drc``, etc.).
TOOL_NAME is the name of the tool, which is referenced in your configuration.
For example, if ACTION is ``par`` and TOOL_NAME is ``par_tool_foo``, the configuration would reference it as ``hammer.par.par_tool_foo``.
