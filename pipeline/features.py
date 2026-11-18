"""Physics-derived features of the solar wind.

Everything here comes from textbook plasma physics, so each column has a
meaning you can explain, not just a statistical role.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PROTON_MASS_KG = 1.6726e-27
L1_DISTANCE_KM = 1.5e6  # approximate Earth-L1 distance; spacecraft fly halo orbits around it


def dynamic_pressure_npa(density_cc, speed_kms):
    """Ram pressure rho*v^2 of the solar wind in nanopascals.

    n [cm^-3] -> m^-3 (x1e6), v [km/s] -> m/s (x1e3), Pa -> nPa (x1e9):
    1.6726e-27 * 1e6 * 1e6 * 1e9 = 1.6726e-6
    """
    return 1.6726e-6 * density_cc * speed_kms ** 2


def clock_angle_rad(by, bz):
    """IMF clock angle in the GSM y-z plane: 0 = due north, pi = due south."""
    return np.arctan2(by, bz)


def newell_coupling(speed_kms, by, bz):
    """Newell et al. (2007) solar wind-magnetosphere coupling function,
    dPhi/dt ~ v^(4/3) * B_T^(2/3) * sin^(8/3)(theta/2).

    Large when the wind is fast and the field points south (Bz < 0), which is
    when energy gets into Earth's magnetosphere. Reported in relative units
    (divided by 1e3) because only its relative size matters here.
    """
    bt_perp = np.sqrt(by ** 2 + bz ** 2)
    theta = clock_angle_rad(by, bz)
    return (speed_kms ** (4 / 3)) * (bt_perp ** (2 / 3)) * (np.abs(np.sin(theta / 2)) ** (8 / 3)) / 1e3


def lead_time_minutes(speed_kms):
    """How long the wind measured at L1 takes to reach Earth: our warning time."""
    return L1_DISTANCE_KM / speed_kms / 60.0


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["pdyn_npa"] = dynamic_pressure_npa(df["proton_density"], df["proton_speed"])
    df["newell"] = newell_coupling(df["proton_speed"], df["by_gsm"], df["bz_gsm"])
    df["lead_time_min"] = lead_time_minutes(df["proton_speed"])
    return df
