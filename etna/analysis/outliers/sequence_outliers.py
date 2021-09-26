from typing import TYPE_CHECKING
from typing import Dict
from typing import List
from typing import Tuple

import numpy as np
import pandas as pd
from saxpy.hotsax import find_discords_hotsax
from tqdm import tqdm
import warnings

if TYPE_CHECKING:
    from etna.datasets import TSDataset


def get_segment_sequence_anomalies(
        series: np.ndarray,
        num_anomalies: int = 1,
        anomaly_lenght: int = 100,
        alphabet_size: int = 3,
        word_lenght: int = 3
) -> List[Tuple[int, int]]:
    """Get indices of start and end of sequence outliers for one segment using SAX HOT algorithm.
   Parameters
   ----------
   series:
       array to find outliers in
   num_anomalies:
       number of outliers to be found
   anomaly_lenght:
       target lenght of outliers
   alphabet_size:
       the number of letters with which the subsequence will be encrypted
   word_lenght:
       the number of segments into which the subsequence will be divided by the paa algorithm
   Returns
   -------
   list of tuples with start and end of outliers.
   """
    start_points = find_discords_hotsax(
        series=series,
        win_size=anomaly_lenght,
        num_discords=num_anomalies,
        a_size=alphabet_size,
        paa_size=word_lenght
    )

    result = [(pt[0], pt[0] + anomaly_lenght) for pt in start_points]

    return result


def get_sequence_anomalies(
        ts: "TSDataset",
        num_anomalies: int = 1,
        anomaly_lenght: int = 100,
        alphabet_size: int = 3,
        word_lenght: int = 3
) -> Dict[str, List[Tuple[pd.Timestamp, pd.Timestamp]]]:
    """Finds the start and end of the sequence outliers for each segment using the SAX HOT algorithm. 
    For each subsequence of length anomaly_lenght, how far is it from the others
    after reduction to N (0,1). 
    The outermost subsequences are considered outliers. 
    We use saxpy under the hood.
    repository link: https://github.com/seninp/saxpy.
    Parameters
    ----------
    ts:
        TSDataset with timeseries data
    num_anomalies:
        number of outliers to be found
    anomaly_lenght:
        target lenght of outliers
    alphabet_size:
        the number of letters with which the subsequence will be encrypted
    word_lenght:
        the number of segments into which the subsequence will be divided by the paa algorithm
    Returns
    -------
    dict of sequence outliers in format {segment_name: [start, end]}, where start and end
    is a pd.Timestamp.
    """
    segments = ts.segments
    outliers_per_segment = dict()

    for seg in tqdm(segments):
        segment_df = ts[:, seg, :][seg]
        if 'target' not in segment_df.columns:
            raise ValueError(f"Each segment must contain a column \'target\'. {seg} does not contain.")
        if segment_df['target'].isnull().sum():
            warnings.warn(f"Segment {seg} contains nan-s. They will be removed when calculating outliers." +
                          "Make sure this behavior is acceptable", RuntimeWarning)

        segment_df = segment_df.dropna().reset_index()
        outliers_idxs = get_segment_sequence_anomalies(
            series=segment_df['target'].values,
            num_anomalies=num_anomalies,
            anomaly_lenght=anomaly_lenght,
            alphabet_size=alphabet_size,
            word_lenght=word_lenght
        )

        timestamps = segment_df['timestamp'].values
        outliers = [(timestamps[idx[0]], timestamps[idx[1]]) for idx in outliers_idxs]
        outliers_per_segment[seg] = outliers

    return outliers_per_segment


__all__ = ["get_sequence_anomalies"]