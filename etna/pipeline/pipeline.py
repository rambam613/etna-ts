from copy import deepcopy
from enum import Enum
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple

import pandas as pd
from joblib import Parallel
from joblib import delayed

from etna.core import BaseMixin
from etna.datasets import TSDataset
from etna.loggers import tslogger
from etna.metrics import Metric
from etna.metrics import MetricAggregationMode
from etna.metrics.utils import compute_metrics
from etna.models.base import Model
from etna.transforms.base import Transform


class CrossValidationMode(Enum):
    """Enum for different cross-validation modes."""

    expand = "expand"
    constant = "constant"


class Pipeline(BaseMixin):
    """Pipeline of transforms with a final estimator."""

    def __init__(self, model: Model, transforms: Iterable[Transform] = (), horizon: int = 1):
        """
        Create instance of Pipeline with given parameters.

        Parameters
        ----------
        model:
            Instance of the etna Model
        transforms:
            Sequence of the transforms
        horizon:
            Number of timestamps in the future for forecasting
        """
        self.model = model
        self.transforms = transforms
        self.horizon = horizon
        self.ts = None

    def fit(self, ts: TSDataset) -> "Pipeline":
        """Fit the Pipeline.
        Fit and apply given transforms to the data, then fit the model on the transformed data.

        Parameters
        ----------
        ts:
            Dataset with timeseries data
        Returns
        -------
        Pipeline:
            Fitted Pipeline instance
        """
        self.ts = ts
        self.ts.fit_transform(self.transforms)
        self.model.fit(self.ts)
        return self

    def forecast(self) -> TSDataset:
        """Make predictions.

        Returns
        -------
        TSDataset
            TSDataset with forecast
        """
        future = self.ts.make_future(self.horizon)
        predictions = self.model.forecast(future)
        return predictions

    def _init_backtest(self):
        self._folds: Optional[Dict[int, Any]] = None
        self._fold_column = "fold_number"

    @staticmethod
    def _validate_backtest_n_folds(n_folds: int):
        """Check that given n_folds value is valid."""
        if n_folds < 1:
            raise ValueError(f"Folds number should be a positive number, {n_folds} given")

    @staticmethod
    def _validate_backtest_dataset(ts: TSDataset, n_folds: int, horizon: int):
        """Check that all the given timestamps have enough timestamp points to validate forecaster with given number of splits."""
        min_required_length = horizon * n_folds
        segments = set(ts.df.columns.get_level_values("segment"))
        for segment in segments:
            segment_target = ts[:, segment, "target"]
            if len(segment_target) < min_required_length:
                raise ValueError(
                    f"All the series from feature dataframe should contain at least "
                    f"{horizon} * {n_folds} = {min_required_length} timestamps; "
                    f"series {segment} does not."
                )

    @staticmethod
    def _validate_backtest_metrics(metrics: List[Metric]):
        """Check that given metrics are valid for backtest."""
        if not metrics:
            raise ValueError("At least one metric required")
        for metric in metrics:
            if not metric.mode == MetricAggregationMode.per_segment:
                raise ValueError(
                    f"All the metrics should be in {MetricAggregationMode.per_segment}, "
                    f"{metric.__class__.__name__} metric is in {metric.mode} mode"
                )

    @staticmethod
    def _generate_folds_datasets(
        ts: TSDataset, n_folds: int, horizon: int, mode: str = "expand"
    ) -> Tuple[TSDataset, TSDataset]:
        """Generate a sequence of train-test pairs according to timestamp."""
        mode = CrossValidationMode[mode.lower()]
        if mode == CrossValidationMode.expand:
            constant_history_length = 0
        elif mode == CrossValidationMode.constant:
            constant_history_length = 1
        else:
            raise NotImplementedError(
                f"Only '{CrossValidationMode.expand}' and '{CrossValidationMode.constant}' modes allowed"
            )

        timestamps = ts.index
        min_timestamp_idx, max_timestamp_idx = 0, len(timestamps)
        for offset in range(n_folds, 0, -1):
            # if not self._constant_history_length, left border of train df is always equal to minimal timestamp value;
            # it means that all the given data is used.
            # if self._constant_history_length, left border of train df moves to one horizon steps on each split
            min_train_idx = min_timestamp_idx + (n_folds - offset) * horizon * constant_history_length
            max_train_idx = max_timestamp_idx - horizon * offset - 1
            min_test_idx = max_train_idx + 1
            max_test_idx = max_train_idx + horizon

            min_train, max_train = timestamps[min_train_idx], timestamps[max_train_idx]
            min_test, max_test = timestamps[min_test_idx], timestamps[max_test_idx]

            train, test = ts.train_test_split(
                train_start=min_train, train_end=max_train, test_start=min_test, test_end=max_test
            )

            yield train, test

    def _run_fold(
        self,
        train: TSDataset,
        test: TSDataset,
        fold_number: int,
        metrics: Optional[List[Metric]] = None,
    ) -> Dict[str, Any]:
        """Run fit-forecast pipeline of model for one fold."""
        tslogger.start_experiment(job_type="crossval", group=str(fold_number))

        pipeline = deepcopy(self)
        pipeline.fit(ts=train)
        forecast = pipeline.forecast()

        fold = {}
        for stage_name, stage_df in zip(("train", "test"), (train, test)):
            fold[f"{stage_name}_timerange"] = {}
            fold[f"{stage_name}_timerange"]["start"] = stage_df.index.min()
            fold[f"{stage_name}_timerange"]["end"] = stage_df.index.max()
        fold["forecast"] = forecast
        fold["metrics"] = deepcopy(compute_metrics(metrics=metrics, y_true=test, y_pred=forecast))

        tslogger.log_backtest_run(pd.DataFrame(fold["metrics"]), forecast.to_pandas(), test.to_pandas())
        tslogger.finish_experiment()

        return fold

    def _get_backtest_metrics(self, aggregate_metrics: bool = False) -> pd.DataFrame:
        """Get dataframe with metrics."""
        metrics_df = pd.DataFrame()

        for i, fold in self._folds.items():
            fold_metrics = pd.DataFrame(fold["metrics"]).reset_index().rename({"index": "segment"}, axis=1)
            fold_metrics[self._fold_column] = i
            metrics_df = metrics_df.append(fold_metrics)

        metrics_df.sort_values(["segment", self._fold_column], inplace=True)

        if aggregate_metrics:
            metrics_df = metrics_df.groupby("segment").mean().reset_index().drop(self._fold_column, axis=1)

        return metrics_df

    def _get_fold_info(self) -> pd.DataFrame:
        """Get information about folds."""
        timerange_df = pd.DataFrame()
        for fold_number, fold_info in self._folds.items():
            tmp_df = pd.DataFrame()
            for stage_name in ("train", "test"):
                for border in ("start", "end"):
                    tmp_df[f"{stage_name}_{border}_time"] = [fold_info[f"{stage_name}_timerange"][border]]
            tmp_df[self._fold_column] = fold_number
            timerange_df = timerange_df.append(tmp_df)
        return timerange_df

    def _get_backtest_forecasts(self) -> pd.DataFrame:
        """Get forecasts from different folds."""
        stacked_forecast = pd.DataFrame()
        for fold_number, fold_info in self._folds.items():
            forecast = fold_info["forecast"]
            for segment in forecast.segments:
                forecast.loc[:, pd.IndexSlice[segment, self._fold_column]] = fold_number
            stacked_forecast = stacked_forecast.append(forecast.df)
        return stacked_forecast

    def backtest(
        self,
        ts: TSDataset,
        metrics: List[Metric],
        n_folds: int = 5,
        mode: str = "expand",
        aggregate_metrics: bool = False,
        n_jobs: int = 1,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Run backtest with the pipeline.

        Parameters
        ----------
        ts:
            dataset to fit models in backtest
        metrics:
            list of metrics to compute for each fold
        n_folds:
            number of folds
        mode:
            one of 'expand', 'constant' -- train generation policy
        aggregate_metrics:
            if True aggregate metrics above folds, return raw metrics otherwise
        n_jobs:
            number of jobs to run in parallel

        Returns
        -------
        pd.DataFrame, pd.DataFrame, pd.Dataframe:
            metrics dataframe, forecast dataframe and dataframe with information about folds
        """
        self._init_backtest()
        self._validate_backtest_n_folds(n_folds=n_folds)
        self._validate_backtest_dataset(ts=ts, n_folds=n_folds, horizon=self.horizon)
        self._validate_backtest_metrics(metrics=metrics)
        folds = Parallel(n_jobs=n_jobs, verbose=11, backend="multiprocessing")(
            delayed(self._run_fold)(train=train, test=test, fold_number=fold_number, metrics=metrics)
            for fold_number, (train, test) in enumerate(
                self._generate_folds_datasets(ts=ts, n_folds=n_folds, horizon=self.horizon, mode=mode)
            )
        )

        self._folds = {i: fold for i, fold in enumerate(folds)}

        metrics_df = self._get_backtest_metrics(aggregate_metrics=aggregate_metrics)
        forecast_df = self._get_backtest_forecasts()
        fold_info_df = self._get_fold_info()

        tslogger.start_experiment(job_type="crossval_results", group="all")
        tslogger.log_backtest_metrics(ts, metrics_df, forecast_df, fold_info_df)
        tslogger.finish_experiment()

        return metrics_df, forecast_df, fold_info_df