"""Water current derivation from boat velocity vectors.

The boat's velocity through the water (STW, HDG) and over the ground
(SOG, COG) differ by the water current acting on the hull. Subtracting
the two vectors yields the current the boat experienced at that moment.

Convention: "set" is the compass direction the current is flowing *toward*
(oceanographic), degrees 0..360 with 0=N, 90=E. "drift" is current speed
in knots.
"""

from __future__ import annotations

import math


def _polar_to_ne(speed: float, compass_deg: float) -> tuple[float, float]:
    rad = math.radians(compass_deg)
    return (speed * math.cos(rad), speed * math.sin(rad))


def compute_set_drift(
    sog: float | None,
    cog: float | None,
    stw: float | None,
    hdg: float | None,
) -> tuple[float, float] | None:
    """Return (set_deg, drift_kts) or None if any input is missing.

    set_deg is the direction the current flows *toward*, 0..360.
    When drift is effectively zero, set_deg is reported as 0.0.
    """
    if sog is None or cog is None or stw is None or hdg is None:
        return None

    n_g, e_g = _polar_to_ne(sog, cog)
    n_w, e_w = _polar_to_ne(stw, hdg)
    n_c, e_c = n_g - n_w, e_g - e_w

    drift = math.hypot(n_c, e_c)
    if drift < 1e-9:
        return (0.0, 0.0)
    set_deg = math.degrees(math.atan2(e_c, n_c)) % 360.0
    return (set_deg, drift)
