"""splitcore - cut one image into N equal tiles and enlarge each towards the original size.

A tile keeps its shape and grows as far as it fits inside the size of the
original picture, upright or turned by 90 degrees (see ``grids.scale_factor``).

The same package runs natively (CLI, tests) and inside the browser through
Pyodide, where the web app drives it from a Web Worker.
"""

__version__ = "1.1.1"
