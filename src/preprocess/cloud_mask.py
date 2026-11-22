import numpy as np

# SCL classes: 0 no data, 1 saturated/defective, 3 cloud shadow,
# 8/9 cloud (medium/high), 10 thin cirrus, 11 snow/ice
BAD_SCL = (0, 1, 3, 8, 9, 10, 11)


def valid_mask(scl: np.ndarray) -> np.ndarray:
    """True where the pixel is usable for vegetation analysis."""
    return ~np.isin(scl, BAD_SCL)