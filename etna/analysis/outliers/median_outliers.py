import math
import typing

import numpy as np

from etna.datasets.tsdataset import TSDataset


def get_anomalies_median(
    ts: TSDataset, window_size: int = 10, alpha: float = 3
) -> typing.Dict[str, typing.List[np.datetime64]]:
    """
    Get point outliers in time series using median model (estimation model-based method).
    Outliers are all points deviating from the median by more than alpha * std, where std is the sample variance in the window.

    Parameters
    ----------
    ts:
        TSDataset with timeseries data
    window_size:
        number of points in the window
    alpha:
        coefficient for determining the threshold
    """
    outliers_per_segment = {}
    segments = ts.segments
    for seg in segments:
        anomal = []

        segment_df = ts.df[seg].reset_index()
        values = segment_df["target"].values
        timestamp = segment_df["timestamp"].values

        n_iter = math.ceil(len(values) / window_size)
        for i in range(n_iter):
            left_border = i * window_size
            right_border = min(left_border + window_size, len(values))
            med = np.median(values[left_border:right_border])
            std = np.std(values[left_border:right_border])
            diff = np.abs(values[left_border:right_border] - med)
            anomal.extend(np.where(diff > std * alpha)[0] + left_border)
        outliers_per_segment[seg] = [timestamp[i] for i in anomal]
    return outliers_per_segment