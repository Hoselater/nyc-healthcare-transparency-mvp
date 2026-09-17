"""NYC DOT traffic scraping: real-time link speeds and the public camera network.

Two independent public feeds, joined on geography:

* **Link speeds** -- NYC DOT's real-time sensor feed, republished on NYC Open
  Data as dataset ``i4gi-tjb9``. Each record is a directional road segment
  ("link") with a current speed, a travel time, and the polyline of the road it
  covers. Refreshed roughly every minute.
* **Traffic cameras** -- the NYC Traffic Management Centre camera inventory at
  ``webcams.nyctmc.org``, which gives a point location and a still-image URL per
  camera. Cameras carry no numbers, so they are useful for *looking* at a jam,
  not measuring one; this package joins each congested link to its nearest
  cameras so there is something to look at.

The default area of interest is the East Side of Manhattan (see :mod:`etl.nycdot.geo`).
"""

from etl.nycdot.geo import EAST_SIDE, Region  # noqa: F401
