"""Per-reading flag bits."""

from enum import IntFlag


class Flag(IntFlag):
    RESET = 1  # full-charge reset fired (SOC = 1)
    CORRECTED = 2  # EKF voltage correction applied
    GAP = 4  # this step bridged a gap (I = 0 integrated, no correction)
    DEGRADED = 8  # estimate quality degraded (after a gap, until reset or sigma recovers)
    LARGE_CORRECTION = 16  # |dSOC| from the EKF exceeded the threshold
    COLD_START = 32  # first step without saved state
    MEASURED_CURRENT = 64  # I came from a measured current instead of P/V


def names(flags: int) -> list[str]:
    return [f.name.lower() for f in Flag if flags & f]
