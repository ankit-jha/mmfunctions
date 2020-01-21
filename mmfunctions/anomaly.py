# *****************************************************************************
# © Copyright IBM Corp. 2018.  All Rights Reserved.
#
# This program and the accompanying materials
# are made available under the terms of the Apache V2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
#
# *****************************************************************************

'''
The Built In Functions module contains preinstalled functions
'''

import datetime as dt
import numpy as np
import scipy as sp

#  for Spectral Analysis
from scipy import signal, fftpack
# from scipy.stats import energy_distance
# from sklearn import metrics
from sklearn.covariance import EllipticEnvelope, MinCovDet

#   for KMeans
#  import skimage as ski
from skimage import util as skiutil  # for nifty windowing
from pyod.models.cblof import CBLOF

# import re
import pandas as pd
import logging
# import warnings
# import json
# from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, func
from iotfunctions.base import (BaseTransformer, BaseRegressor)
from iotfunctions.bif import (AlertHighValue)
from iotfunctions.ui import (UISingle, UIMultiItem, UIFunctionOutSingle, UISingleItem, UIFunctionOutMulti)

logger = logging.getLogger(__name__)
PACKAGE_URL = 'git+https://github.com/sedgewickmm18/mmfunctions.git@'
_IS_PREINSTALLED = False

FrequencySplit = 0.3
DefaultWindowSize = 12
SmallEnergy = 0.0000001


def custom_resampler(array_like):
    # initialize
    if 'gap' not in dir():
        gap = 0

    if (array_like.values.size > 0):
        gap = 0
        return 0
    else:
        gap += 1
        return gap


def min_delta(df):
    # minimal time delta for merging

    try:
        mindelta = df.index.to_series().diff().min()
    except Exception as e:
        logger.debug('Min Delta error: ' + str(e))
        mindelta = pd.Timedelta('5 seconds')

    if mindelta == dt.timedelta(seconds=0) or pd.isnull(mindelta):
        mindelta = pd.Timedelta('5 seconds')
    return mindelta


def set_window_size_and_overlap(windowsize, trim_value=2*DefaultWindowSize):
    # make sure it is positive and not too large
    trimmed_ws = np.minimum(np.maximum(windowsize, 1), trim_value)

    # overlap
    if trimmed_ws == 1:
        ws_overlap = 0
    else:
        ws_overlap = trimmed_ws - np.maximum(trimmed_ws // DefaultWindowSize, 1)

    return trimmed_ws, ws_overlap


# Saliency helper functions
# copied from https://github.com/y-bar/ml-based-anomaly-detection
#   remove the boring part from an image resp. time series
def series_filter(values, kernel_size=3):
    """
    Filter a time series. Practically, calculated mean value inside kernel size.
    As math formula, see https://docs.opencv.org/2.4/modules/imgproc/doc/filtering.html.
    :param values:
    :param kernel_size:
    :return: The list of filtered average
    """
    filter_values = np.cumsum(values, dtype=float)

    filter_values[kernel_size:] = filter_values[kernel_size:] - filter_values[:-kernel_size]
    filter_values[kernel_size:] = filter_values[kernel_size:] / kernel_size

    for i in range(1, kernel_size):
        filter_values[i] /= i + 1

    return filter_values


def extrapolate_next(values):
    """
    Extrapolates the next value by sum up the slope of the last value with previous values.
    :param values: a list or numpy array of time-series
    :return: the next value of time-series
    """

    last_value = values[-1]
    slope = [(last_value - v) / i for (i, v) in enumerate(values[::-1])]
    slope[0] = 0
    next_values = last_value + np.cumsum(slope)

    return next_values


def marge_series(values, extend_num=5, forward=5):

    next_value = extrapolate_next(values)[forward]
    extension = [next_value] * extend_num

    if isinstance(values, list):
        marge_values = values + extension
    else:
        marge_values = np.append(values, extension)
    return marge_values


# Saliency class
#  see https://www.inf.uni-hamburg.de/en/inst/ab/cv/research/research1-visual-attention.html
class Saliency(object):
    def __init__(self, amp_window_size, series_window_size, score_window_size):
        self.amp_window_size = amp_window_size
        self.series_window_size = series_window_size
        self.score_window_size = score_window_size

    def transform_saliency_map(self, values):
        """
        Transform a time-series into spectral residual, which is method in computer vision.
        For example, See https://docs.opencv.org/master/d8/d65/group__saliency.html
        :param values: a list or numpy array of float values.
        :return: silency map and spectral residual
        """

        freq = np.fft.fft(values)
        mag = np.sqrt(freq.real ** 2 + freq.imag ** 2)

        # remove the boring part of a timeseries
        spectral_residual = np.exp(np.log(mag) - series_filter(np.log(mag), self.amp_window_size))

        freq.real = freq.real * spectral_residual / mag
        freq.imag = freq.imag * spectral_residual / mag

        # and apply inverse fourier transform
        saliency_map = np.fft.ifft(freq)
        return saliency_map

    def transform_spectral_residual(self, values):
        saliency_map = self.transform_saliency_map(values)
        spectral_residual = np.sqrt(saliency_map.real ** 2 + saliency_map.imag ** 2)
        return spectral_residual


class SpectralAnomalyScore(BaseTransformer):
    '''
    Employs spectral analysis to extract features from the time series data and to compute zscore from it
    '''
    def __init__(self, input_item, windowsize, output_item):
        super().__init__()
        logger.debug(input_item)
        self.input_item = input_item

        # use 12 by default
        self.windowsize, self.windowoverlap = set_window_size_and_overlap(windowsize)

        # assume 1 per sec for now
        self.frame_rate = 1

        self.output_item = output_item

    def execute(self, df):

        df_copy = df.copy()
        entities = np.unique(df.index.levels[0])
        logger.debug(str(entities))

        df_copy[self.output_item] = 0
        # df_copy.sort_index()   # NoOp

        for entity in entities:
            # per entity - copy for later inplace operations
            dfe = df_copy.loc[[entity]].dropna(how='all')
            dfe_orig = df_copy.loc[[entity]].copy()

            # get rid of entityid part of the index
            # do it inplace as we copied the data before
            dfe.reset_index(level=[0], inplace=True)
            dfe.sort_index(inplace=True)
            dfe_orig.reset_index(level=[0], inplace=True)
            dfe_orig.sort_index(inplace=True)

            # minimal time delta for merging
            mindelta = min_delta(dfe_orig)

            logger.debug('Timedelta:' + str(mindelta))

            #  interpolate gaps - data imputation
            # Size = dfe[[self.input_item]].fillna(0).to_numpy().size
            dfe = dfe.interpolate(method='time')

            # one dimensional time series - named temperature for catchyness
            temperature = dfe[[self.input_item]].fillna(0).to_numpy().reshape(-1,)

            logger.debug('Module Spectral, Entity: ' + str(entity) + ', Input: ' + str(self.input_item) +
                         ', Windowsize: ' + str(self.windowsize) + ', Output: ' + str(self.output_item) +
                         ', Overlap: ' + str(self.windowoverlap) + ', Inputsize: ' + str(temperature.size))

            if temperature.size <= self.windowsize:
                logger.debug(str(temperature.size) + ' <= ' + str(self.windowsize))
                # df_copy.loc[[entity]] = 0.0001
                dfe[self.output_item] = 0.0001
            else:
                logger.debug(str(temperature.size) + str(self.windowsize))

                dfe[self.output_item] = 0.0007
                try:
                    # Fourier transform:
                    #   frequency, time, spectral density
                    frequency_temperature, time_series_temperature, spectral_density_temperature = signal.spectrogram(
                        temperature, fs=self.frame_rate, window='hanning',
                        nperseg=self.windowsize, noverlap=self.windowoverlap,
                        detrend=False, scaling='spectrum')

                    # cut off freqencies too low to fit into the window
                    frequency_temperatureb = (frequency_temperature > 2/self.windowsize).astype(int)
                    frequency_temperature = frequency_temperature * frequency_temperatureb
                    frequency_temperature[frequency_temperature == 0] = 1 / self.windowsize

                    highfrequency_temperature = frequency_temperature.copy()
                    lowfrequency_temperature = frequency_temperature.copy()
                    highfrequency_temperature[highfrequency_temperature <= FrequencySplit] = 0
                    lowfrequency_temperature[lowfrequency_temperature > FrequencySplit] = 0

                    # Compute energy = frequency * spectral density over time in decibel
                    lowsignal_energy = np.log10(np.maximum(SmallEnergy, np.dot(spectral_density_temperature.T,
                                                lowfrequency_temperature)) + SmallEnergy)
                    highsignal_energy = np.log10(np.maximum(SmallEnergy, np.dot(spectral_density_temperature.T,
                                                 highfrequency_temperature)) + SmallEnergy)

                    lowsignal_energy = np.dot(spectral_density_temperature.T, lowfrequency_temperature)
                    highsignal_energy = np.dot(spectral_density_temperature.T, highfrequency_temperature)

                    # compute the elliptic envelope to exploit Minimum Covariance Determinant estimates
                    #    standardizing
                    # low_stddev = lowsignal_energy.std(ddof=0)
                    # high_stddev = highsignal_energy.std(ddof=0)

                    # if low_stddev != 0:
                    #     lowsignal_energy = (lowsignal_energy - lowsignal_energy.mean())/low_stddev
                    # else:
                    lowsignal_energy = (lowsignal_energy - lowsignal_energy.mean())

                    # if high_stddev != 0:
                    #     highsignal_energy = (highsignal_energy - highsignal_energy.mean())/high_stddev
                    # else:
                    highsignal_energy = (highsignal_energy - highsignal_energy.mean())

                    twoDimsignal_energy = np.vstack((lowsignal_energy, highsignal_energy)).T
                    logger.debug('lowsignal_energy: ' + str(lowsignal_energy.shape) + ', highsignal_energy:' +
                                 str(highsignal_energy.shape) + 'input' + str(twoDimsignal_energy.shape))

                    # inliers have a score of 1, outliers -1, and 0 indicates an issue with the data
                    dfe[self.output_item] = 0.0002
                    ellEnv = EllipticEnvelope(random_state=0)

                    dfe[self.output_item] = 0.0003
                    ellEnv.fit(twoDimsignal_energy)

                    # compute distance to elliptic envelope
                    dfe[self.output_item] = 0.0004

                    # ets_zscore = ellEnv.decision_function(twoDimsignal_energy, raw_values=True).copy()
                    ets_zscore = ellEnv.decision_function(twoDimsignal_energy).copy()

                    logger.debug('Spectral z-score max: ' + str(ets_zscore.max()) + ', min: ' + str(ets_zscore.min()))

                    # length of time_series_temperature, signal_energy and ets_zscore is smaller than half the original
                    #   extend it to cover the full original length
                    dfe[self.output_item] = 0.0005
                    linear_interpolate = sp.interpolate.interp1d(
                        time_series_temperature, ets_zscore, kind='linear', fill_value='extrapolate')

                    dfe[self.output_item] = 0.0006
                    zscoreI = linear_interpolate(np.arange(0, temperature.size, 1))

                    dfe[self.output_item] = zscoreI

                except Exception as e:
                    logger.error('Spectral failed with ' + str(e))

                # absolute zscore > 3 ---> anomaly
                dfe_orig = pd.merge_asof(dfe_orig, dfe[self.output_item],
                                         left_index=True, right_index=True, direction='nearest', tolerance=mindelta)

                if self.output_item+'_y' in dfe_orig:
                    zScoreII = dfe_orig[self.output_item+'_y'].to_numpy()
                elif self.output_item in dfe_orig:
                    zScoreII = dfe_orig[self.output_item].to_numpy()
                else:
                    zScoreII = dfe_orig[self.input_item].to_numpy()

                idx = pd.IndexSlice
                df_copy.loc[idx[entity, :], self.output_item] = zScoreII

        msg = 'SpectralAnomalyScore'
        self.trace_append(msg)

        return (df_copy)

    @classmethod
    def build_ui(cls):

        # define arguments that behave as function inputs
        inputs = []
        inputs.append(UISingleItem(
                name='input_item',
                datatype=float,
                description='Column for feature extraction'
                                              ))

        inputs.append(UISingle(
                name='windowsize',
                datatype=int,
                description='Window size for spectral analysis - default 12'
                                              ))

        # define arguments that behave as function outputs
        outputs = []
        outputs.append(UIFunctionOutSingle(
                name='output_item',
                datatype=float,
                description='Spectral anomaly score (elliptic envelope)'
                ))
        return (inputs, outputs)


class KMeansAnomalyScore(BaseTransformer):
    '''
    Employs kmeans on windowed time series data and to compute
     an anomaly score from proximity to centroid's center points
    '''
    def __init__(self, input_item, windowsize, output_item):
        super().__init__()
        logger.debug(input_item)
        self.input_item = input_item

        # use 12 by default
        self.windowsize, _ = set_window_size_and_overlap(windowsize)

        # step
        self.step = 1

        # assume 1 per sec for now
        self.frame_rate = 1

        self.output_item = output_item

    def execute(self, df):

        df_copy = df.copy()
        entities = np.unique(df_copy.index.levels[0])
        logger.debug(str(entities))

        df_copy[self.output_item] = 0
        # df_copy.sort_index() - NoOP

        for entity in entities:
            # per entity - copy for later inplace operations
            dfe = df_copy.loc[[entity]].dropna(how='all')
            dfe_orig = df_copy.loc[[entity]].copy()

            # get rid of entityid part of the index
            # do it inplace as we copied the data before
            dfe.reset_index(level=[0], inplace=True)
            dfe.sort_index(inplace=True)
            dfe_orig.reset_index(level=[0], inplace=True)
            dfe_orig.sort_index(inplace=True)

            # minimal time delta for merging
            mindelta = min_delta(dfe_orig)

            logger.debug('Timedelta:' + str(mindelta))

            #  interpolate gaps - data imputation
            # Size = dfe[[self.input_item]].fillna(0).to_numpy().size
            dfe = dfe.interpolate(method='time')

            # one dimensional time series - named temperature for catchyness
            temperature = dfe[[self.input_item]].fillna(0).to_numpy().reshape(-1,)

            logger.debug('Module KMeans, Entity: ' + str(entity) + ', Input: ' + str(self.input_item) +
                         ', Windowsize: ' + str(self.windowsize) + ', Output: ' + str(self.output_item) +
                         ', Overlap: ' + str(self.step) + ', Inputsize: ' + str(temperature.size))

            if temperature.size > self.windowsize:
                logger.debug(str(temperature.size) + ',' + str(self.windowsize))

                # Chop into overlapping windows
                slices = skiutil.view_as_windows(temperature, window_shape=(self.windowsize,), step=self.step)

                if self.windowsize > 1:
                    n_cluster = 40
                else:
                    n_cluster = 20

                n_cluster = np.minimum(n_cluster, slices.shape[0] // 2)

                logger.debug('KMeans parms, Clusters: ' + str(n_cluster) + ', Slices: ' + str(slices.shape))

                cblofwin = CBLOF(n_clusters=n_cluster, n_jobs=-1)
                try:
                    cblofwin.fit(slices)
                except Exception as e:
                    logger.info('KMeans failed with ' + str(e))
                    self.trace_append('KMeans failed with' + str(e))
                    continue

                pred_score = cblofwin.decision_scores_.copy()

                # length of time_series_temperature, signal_energy and ets_zscore is smaller than half the original
                #   extend it to cover the full original length
                time_series_temperature = np.linspace(
                     self.windowsize//2, temperature.size - self.windowsize//2 + 1,
                     temperature.size - self.windowsize + 1)

                linear_interpolateK = sp.interpolate.interp1d(
                    time_series_temperature, pred_score, kind='linear', fill_value='extrapolate')

                kmeans_scoreI = linear_interpolateK(np.arange(0, temperature.size, 1))

                dfe[self.output_item] = kmeans_scoreI

                # absolute kmeans_score > 1000 ---> anomaly
                dfe_orig = pd.merge_asof(dfe_orig, dfe[self.output_item],
                                         left_index=True, right_index=True, direction='nearest', tolerance=mindelta)

                if self.output_item+'_y' in dfe_orig:
                    zScoreII = dfe_orig[self.output_item+'_y'].to_numpy()
                elif self.output_item in dfe_orig:
                    zScoreII = dfe_orig[self.output_item].to_numpy()
                else:
                    zScoreII = dfe_orig[self.input_item].to_numpy()

                idx = pd.IndexSlice
                df_copy.loc[idx[entity, :], self.output_item] = zScoreII

        msg = 'KMeansAnomalyScore'
        self.trace_append(msg)
        return (df_copy)

    @classmethod
    def build_ui(cls):
        # define arguments that behave as function inputs
        inputs = []
        inputs.append(UISingleItem(
                name='input_item',
                datatype=float,
                description='Column for feature extraction'
                                              ))

        inputs.append(UISingle(
                name='windowsize',
                datatype=int,
                description='Window size for spectral analysis - default 12'
                                              ))

        # define arguments that behave as function outputs
        outputs = []
        outputs.append(UIFunctionOutSingle(
                name='output_item',
                datatype=float,
                description='Anomaly score (kmeans)'
                ))
        return (inputs, outputs)


class GeneralizedAnomalyScore(BaseTransformer):
    """
    Employs GAM on windowed time series data to compute an anomaly score from the covariance matrix
    """

    def __init__(self, input_item, windowsize, output_item):
        super().__init__()
        logger.debug(input_item)

        self.whoami = 'GAM'

        self.input_item = input_item

        # use 12 by default
        self.windowsize, self.windowoverlap = set_window_size_and_overlap(windowsize)

        # step
        self.step = 1

        # assume 1 per sec for now
        self.frame_rate = 1

        self.output_item = output_item

    def prepare_data(self, dfEntity):

        logger.debug(self.whoami + ': prepare Data')

        # interpolate gaps - data imputation
        dfe = dfEntity.interpolate(method="time")

        # one dimensional time series - named temperature for catchyness
        temperature = dfe[[self.input_item]].fillna(0).to_numpy().reshape(-1,)

        return dfe, temperature

    def feature_extract(self, temperature):

        logger.debug(self.whoami + ': feature extract')

        slices = skiutil.view_as_windows(
            temperature, window_shape=(self.windowsize,), step=self.step
        )
        return slices

    def execute(self, df):

        df_copy = df.copy()
        entities = np.unique(df_copy.index.levels[0])
        logger.debug(str(entities))

        df_copy[self.output_item] = 0
        # df_copy.sort_index()

        for entity in entities:
            # per entity - copy for later inplace operations
            dfe = df_copy.loc[[entity]].dropna(how='all')
            dfe_orig = df_copy.loc[[entity]].copy()

            # get rid of entityid part of the index
            # do it inplace as we copied the data before
            dfe.reset_index(level=[0], inplace=True)
            dfe.sort_index(inplace=True)
            dfe_orig.reset_index(level=[0], inplace=True)
            dfe_orig.sort_index(inplace=True)

            # minimal time delta for merging
            mindelta = min_delta(dfe_orig)

            # interpolate gaps - data imputation by default
            #   for missing data detection we look at the timestamp gradient instead
            dfe, temperature = self.prepare_data(dfe)

            logger.debug('Module GeneralizedAnomaly, Entity: ' + str(entity) + ', Input: ' + str(self.input_item) +
                         ', Windowsize: ' + str(self.windowsize) + ', Output: ' + str(self.output_item) +
                         ', Overlap: ' + str(self.step) + ', Inputsize: ' + str(temperature.size))

            if temperature.size > self.windowsize:
                logger.debug(str(temperature.size) + "," + str(self.windowsize))

                # NN = GeneralizedAnomalyModel( base_learner=MinCovDet(), fit_function="fit",
                #        predict_function="mahalanobis", score_sign=1,)
                temperature -= np.mean(temperature, axis=0)
                mcd = MinCovDet()

                # Chop into overlapping windows (default) or run through FFT first
                slices = self.feature_extract(temperature)

                pred_score = None

                try:
                    # NN.fit(slices)
                    mcd.fit(slices)

                    # pred_score = NN.decision_function(slices).copy()
                    pred_score = mcd.mahalanobis(slices).copy()

                except ValueError as ve:

                    logger.info(
                        self.whoami + " GeneralizedAnomalyScore: Entity: "
                        + str(entity) + ", Input: " + str(self.input_item) + ", WindowSize: "
                        + str(self.windowsize) + ", Output: " + str(self.output_item) + ", Step: "
                        + str(self.step) + ", InputSize: " + str(slices.shape)
                        + " failed in the fitting step with \"" + str(ve) + "\" - scoring zero")

                    pred_score = np.zeros(slices.shape[0])
                    pass

                except Exception as e:

                    dfe[self.output_item] = 0
                    logger.error(
                        self.whoami + " GeneralizedAnomalyScore: Entity: "
                        + str(entity) + ", Input: " + str(self.input_item) + ", WindowSize: "
                        + str(self.windowsize) + ", Output: " + str(self.output_item) + ", Step: "
                        + str(self.step) + ", InputSize: " + str(slices.shape)
                        + " failed in the fitting step with " + str(e))
                    continue

                # will break if pred_score is None
                # length of timesTS, ETS and ets_zscore is smaller than half the original
                #   extend it to cover the full original length
                timesTS = np.linspace(
                    self.windowsize // 2,
                    temperature.size - self.windowsize // 2 + 1,
                    temperature.size - self.windowsize + 1,
                )

                logger.debug(self.whoami + '   Entity: ' + str(entity) + ', result shape: ' + str(timesTS.shape) +
                             ' score shape: ' + str(pred_score.shape))

                # timesI = np.linspace(0, Size - 1, Size)
                linear_interpolateK = sp.interpolate.interp1d(
                    timesTS, pred_score, kind="linear", fill_value="extrapolate"
                )

                # kmeans_scoreI = np.interp(timesI, timesTS, pred_score)
                gam_scoreI = linear_interpolateK(np.arange(0, temperature.size, 1))

                dfe[self.output_item] = gam_scoreI

                # absolute kmeans_score > 1000 ---> anomaly

                dfe_orig = pd.merge_asof(
                    dfe_orig,
                    dfe[self.output_item],
                    left_index=True,
                    right_index=True,
                    direction="nearest",
                    tolerance=mindelta,
                )

                if self.output_item + "_y" in dfe_orig:
                    zScoreII = dfe_orig[self.output_item + "_y"].to_numpy()
                elif self.output_item in dfe_orig:
                    zScoreII = dfe_orig[self.output_item].to_numpy()
                else:
                    print(dfe_orig.head(2))
                    zScoreII = dfe_orig[self.input_item].to_numpy()

                idx = pd.IndexSlice
                df_copy.loc[idx[entity, :], self.output_item] = zScoreII

        msg = "GeneralizedAnomalyScore"
        self.trace_append(msg)
        return df_copy

    @classmethod
    def build_ui(cls):
        # define arguments that behave as function inputs
        inputs = []
        inputs.append(
            UISingleItem(
                name="input_item",
                datatype=float,
                description="Column for feature extraction",
            )
        )

        inputs.append(
            UISingle(
                name="windowsize",
                datatype=int,
                description="Window size for Generalized Anomaly analysis - default 12",
            )
        )

        # define arguments that behave as function outputs
        outputs = []
        outputs.append(
            UIFunctionOutSingle(
                name="output_item",
                datatype=float,
                description="Anomaly score (GeneralizedAnomaly)",
            )
        )
        return (inputs, outputs)


class NoDataAnomalyScore(GeneralizedAnomalyScore):
    '''
    Employs generalized anomaly analysis to extract features from the
      gaps in time series data and to compute the elliptic envelope from it
    '''
    def __init__(self, input_item, windowsize, output_item):
        super().__init__(input_item, windowsize, output_item)
        self.whoami = 'NoData'
        logger.debug('NoData')

    def prepare_data(self, dfEntity):

        logger.debug(self.whoami + ': prepare Data')

        # count the timedelta in seconds between two events
        timeSeq = (dfEntity.index.values - dfEntity.index[0].to_datetime64()) / np.timedelta64(1, 's')

        # one dimensional time series - named temperature for catchyness
        #   we look at the gradient of the time series timestamps for anomaly detection
        temperature = np.gradient(timeSeq)

        dfe = dfEntity.copy()
        dfe[[self.input_item]] = temperature

        return dfe, temperature

    def execute(self, df):
        df_copy = super().execute(df)

        msg = "NoDataAnomalyScore"
        self.trace_append(msg)
        return df_copy

    @classmethod
    def build_ui(cls):

        # define arguments that behave as function inputs
        inputs = []
        inputs.append(UISingleItem(
                name='input_item',
                datatype=float,
                description='Column for feature extraction'
                                              ))

        inputs.append(UISingle(
                name='windowsize',
                datatype=int,
                description='Window size for no data spectral analysis - default 12'
                                              ))

        # define arguments that behave as function outputs
        outputs = []
        outputs.append(UIFunctionOutSingle(
                name='output_item',
                datatype=float,
                description='No data anomaly score'
                ))
        return (inputs, outputs)


class FFTbasedGeneralizedAnomalyScore(GeneralizedAnomalyScore):
    """
    Employs FFT and GAM on windowed time series data to compute an anomaly score from the covariance matrix
    """

    def __init__(self, input_item, windowsize, output_item):
        super().__init__(input_item, windowsize, output_item)
        self.whoami = 'FFT'
        logger.debug('FFT')

    def feature_extract(self, temperature):

        logger.debug(self.whoami + ': feature extract')

        slices_ = skiutil.view_as_windows(
            temperature, window_shape=(self.windowsize,), step=self.step
        )
        slicelist = []
        for slice in slices_:
            slicelist.append(fftpack.rfft(slice))

        # return np.array(slicelist)
        return np.stack(slicelist, axis=0)

    def execute(self, df):
        df_copy = super().execute(df)

        msg = "FFTbasedGeneralizedAnomalyScore"
        self.trace_append(msg)
        return df_copy

    @classmethod
    def build_ui(cls):
        # define arguments that behave as function inputs
        inputs = []
        inputs.append(
            UISingleItem(
                name="input_item",
                datatype=float,
                description="Column for feature extraction",
            )
        )

        inputs.append(
            UISingle(
                name="windowsize",
                datatype=int,
                description="Window size for FFT feature based Generalized Anomaly analysis - default 12",
            )
        )

        # define arguments that behave as function outputs
        outputs = []
        outputs.append(
            UIFunctionOutSingle(
                name="output_item",
                datatype=float,
                description="Anomaly score (FFTbasedGeneralizedAnomalyScore)",
            )
        )
        return (inputs, outputs)


class SaliencybasedGeneralizedAnomalyScore(GeneralizedAnomalyScore):
    """
    Employs Saliency and GAM on windowed time series data to compute an anomaly score from the covariance matrix
    """

    def __init__(self, input_item, windowsize, output_item):
        super().__init__(input_item, windowsize, output_item)
        self.whoami = 'Saliency'
        self.saliency = Saliency(windowsize, 0, 0)
        logger.debug('Saliency')

    def feature_extract(self, temperature):

        logger.debug(self.whoami + ': feature extract')

        temperature_saliency = self.saliency.transform_spectral_residual(temperature)

        slices = skiutil.view_as_windows(
            temperature_saliency, window_shape=(self.windowsize,), step=self.step
        )
        return slices

    def execute(self, df):
        df_copy = super().execute(df)

        msg = "SaliencybasedGeneralizedAnomalyScore"
        self.trace_append(msg)
        return df_copy

    @classmethod
    def build_ui(cls):
        # define arguments that behave as function inputs
        inputs = []
        inputs.append(
            UISingleItem(
                name="input_item",
                datatype=float,
                description="Column for feature extraction",
            )
        )

        inputs.append(
            UISingle(
                name="windowsize",
                datatype=int,
                description="Window size for Saliency feature based Generalized Anomaly analysis - default 12",
            )
        )

        # define arguments that behave as function outputs
        outputs = []
        outputs.append(
            UIFunctionOutSingle(
                name="output_item",
                datatype=float,
                description="Anomaly score (FFTbasedGeneralizedAnomalyScore)",
            )
        )
        return (inputs, outputs)


class SimpleAnomaly(BaseRegressor):
    '''
    Sample function uses a regression model to predict the value of one or more output
    variables. It compares the actual value to the prediction and generates an alert
    when the difference between the actual and predicted value is outside of a threshold.
    '''
    # class variables
    train_if_no_model = True
    estimators_per_execution = 3
    num_rounds_per_estimator = 3

    def __init__(self, features, targets, threshold, predictions=None, alerts=None):
        super().__init__(features=features, targets=targets, predictions=predictions)
        if alerts is None:
            alerts = ['%s_alert' % x for x in self.targets]
        self.alerts = alerts
        self.threshold = threshold

    def execute(self, df):

        df = super().execute(df)
        for i, t in enumerate(self.targets):
            prediction = self.predictions[i]
            df['_diff_'] = (df[t] - df[prediction]).abs()
            alert = AlertHighValue(input_item='_diff_', upper_threshold=self.threshold, alert_name=self.alerts[i])
            alert.set_entity_type(self.get_entity_type())
            df = alert.execute(df)

        return df

    @classmethod
    def build_ui(cls):
        # define arguments that behave as function inputs
        inputs = []
        inputs.append(UIMultiItem(name='features', datatype=float, required=True))
        inputs.append(UIMultiItem(name='targets', datatype=float, required=True, output_item='predictions',
                                  is_output_datatype_derived=True))
        inputs.append(UISingle(name='threshold', datatype=float,
                               description=('Threshold for firing an alert. Expressed as absolute value not percent.')))
        # define arguments that behave as function outputs
        outputs = []
        outputs.append(
            UIFunctionOutMulti(name='alerts', datatype=bool, cardinality_from='targets', is_datatype_derived=False, ))

        return (inputs, outputs)


class SimpleRegressor(BaseRegressor):
    '''
    Sample function that predicts the value of a continuous target variable using the selected list of features.
    This function is intended to demonstrate the basic workflow of training, evaluating, deploying
    using a model.
    '''
    # class variables
    train_if_no_model = True
    estimators_per_execution = 3
    num_rounds_per_estimator = 3

    def __init__(self, features, targets, predictions=None):
        super().__init__(features=features, targets=targets, predictions=predictions)

    @classmethod
    def build_ui(cls):
        # define arguments that behave as function inputs
        inputs = []
        inputs.append(UIMultiItem(name='features', datatype=float, required=True))
        inputs.append(UIMultiItem(name='targets', datatype=float, required=True, output_item='predictions',
                                  is_output_datatype_derived=True))
        return (inputs, [])
