"""Streaming PCM aggregation for bounded-memory waveform generation."""

import math
import sys
from array import array

try:
    import audioop
except ImportError:  # Python 3.13 removed audioop; keep tests/tools portable.
    audioop = None


class WaveformLimitError(ValueError):
    pass


def _rms(data, width):
    if audioop is not None:
        return audioop.rms(data, width)
    if width != 2:
        raise ValueError("the portable RMS fallback supports 16-bit PCM only")
    values = array("h")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    if not values:
        return 0
    return int(math.sqrt(sum(value * value for value in values) / len(values)))


def validate_waveform_request(duration, buckets, rate, maximum_duration):
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise WaveformLimitError("audio duration is unavailable")
    if duration > maximum_duration:
        raise WaveformLimitError(f"audio exceeds the {maximum_duration / 3600:g} hour limit")
    if buckets <= 0 or rate <= 0:
        raise WaveformLimitError("invalid waveform dimensions")
    return max(1, int(math.ceil(duration * rate)))


def aggregate_pcm(chunks, buckets, total_samples, width=2):
    """Reduce an iterable of PCM byte chunks to normalized RMS buckets."""
    samples_per_bucket = max(1, int(math.ceil(total_samples / buckets)))
    sums = [0.0] * buckets
    counts = [0] * buckets
    sample_index = 0
    carry = b""

    for chunk in chunks:
        data = carry + chunk
        usable = len(data) - (len(data) % width)
        carry = data[usable:]
        offset = 0
        while offset < usable and sample_index < total_samples:
            bucket = min(buckets - 1, sample_index // samples_per_bucket)
            boundary = min(total_samples, (bucket + 1) * samples_per_bucket)
            count = min((usable - offset) // width, boundary - sample_index)
            if count <= 0:
                break
            end = offset + count * width
            rms = _rms(data[offset:end], width)
            sums[bucket] += float(rms * rms) * count
            counts[bucket] += count
            sample_index += count
            offset = end

    raw = [math.sqrt(total / count) if count else 0.0 for total, count in zip(sums, counts)]
    high = max(raw, default=0.0) or 1.0
    return [round((value / high) ** 0.7, 3) for value in raw]
