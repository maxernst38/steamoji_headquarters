"""Standard VEX VRC field geometry, used as the target coordinate system for calibration."""

FIELD_SIZE_IN = 144.0
TILE_SIZE_IN = 24.0


def tile_grid_points():
    """All floor tile-grid intersections (corners included) as (x_in, y_in) tuples,
    origin at one field corner, axes along the field edges.
    """
    steps = int(FIELD_SIZE_IN / TILE_SIZE_IN) + 1
    return [
        (round(i * TILE_SIZE_IN, 3), round(j * TILE_SIZE_IN, 3))
        for i in range(steps)
        for j in range(steps)
    ]
