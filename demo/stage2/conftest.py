"""Keep target tests out of the host harness suite.

The contained runner explicitly selects acceptance/regression and sets its
confcutdir below this file. Target modules are never imported on the host.
"""

collect_ignore = ['regression']
