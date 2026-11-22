import numpy as np


def to_reflectance(dn: np.ndarray, baseline: str) -> np.ndarray:
    # Processing baseline N0400 and newer adds +1000 to digital numbers.
    offset = 1000 if int(baseline.lstrip("N")) >= 400 else 0
    refl = (dn.astype("float32") - offset) / 10000.0
    return np.clip(refl, 0, None)


def compute_ndvi(red_dn, nir_dn, baseline: str) -> np.ndarray:
    red = to_reflectance(red_dn, baseline)
    nir = to_reflectance(nir_dn, baseline)
    denom = red + nir
    ndvi = np.full(denom.shape, np.nan, dtype="float32")
    np.divide(nir - red, denom, out=ndvi, where=denom > 0)
    ndvi[(red_dn == 0) | (nir_dn == 0)] = np.nan   # no-data pixels
    return ndvi