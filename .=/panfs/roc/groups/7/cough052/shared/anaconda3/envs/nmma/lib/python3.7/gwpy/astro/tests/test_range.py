# -*- coding: utf-8 -*-
# Copyright (C) Duncan Macleod (2014-2020)
#
# This file is part of GWpy.
#
# GWpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWpy.  If not, see <http://www.gnu.org/licenses/>.

"""Unit tests for :mod:`gwpy.astro.range`
"""

import pytest

from astropy import units
from scipy.integrate import trapz

from ... import astro
from ...testing import utils
from ...timeseries import TimeSeries
from ...frequencyseries import FrequencySeries
from ...spectrogram import Spectrogram
from ...utils.misc import null_context

__author__ = 'Duncan Macleod <duncan.macleod@ligo.org>'
__credits__ = 'Alex Urban <alexander.urban@ligo.org>'

# -- test results -------------------------------------------------------------

TEST_RESULTS = {
    'sensemon_range': 19.332958991178117 * units.Mpc,
    'burst_range': 13.592140825568954 * units.Mpc,
}


# -- utilities ----------------------------------------------------------------

@pytest.fixture(scope='module')
def hoft():
    return TimeSeries.read(
        utils.TEST_HDF5_FILE,
        "L1:LDAS-STRAIN",
        format="hdf5",
    )


@pytest.fixture(scope='module')
def psd(hoft):
    return hoft.psd(
        .4,
        overlap=.2,
        method="median",
        window=('kaiser', 24),
    )


# -- gwpy.astro.range ---------------------------------------------------------

def test_sensemon_range_psd(psd):
    """Test for :func:`gwpy.astro.sensemon_range_psd`
    """
    fisco = astro.range._get_isco_frequency(1.4, 1.4).value
    frange = (psd.frequencies.value < fisco)
    r = astro.sensemon_range_psd(psd[frange])
    assert isinstance(r, FrequencySeries)
    utils.assert_quantity_almost_equal(
        trapz(r, r.frequencies) ** (1/2.),
        TEST_RESULTS['sensemon_range'],
    )
    assert r.f0.value > 0


def test_sensemon_range(psd):
    """Test for :func:`gwpy.astro.sensemon_range`
    """
    r = astro.sensemon_range(psd)
    utils.assert_quantity_almost_equal(r, TEST_RESULTS['sensemon_range'])


def test_burst_range_spectrum(psd):
    """Test for :func:`gwpy.astro.burst_range_spectrum`
    """
    f = psd.frequencies
    frange = (f.value >= 100) & (f.value < 500)
    r = astro.burst_range_spectrum(psd[frange])
    assert isinstance(r, FrequencySeries)
    utils.assert_quantity_almost_equal(
        (trapz(r**3, r.frequencies) / (400 * units.Hz)) ** (1/3.),
        TEST_RESULTS['burst_range'],
    )
    assert r.f0.value > 0


def test_burst_range(psd):
    """Test for :func:`gwpy.astro.burst_range`
    """
    r = astro.burst_range(psd)
    utils.assert_quantity_almost_equal(r, TEST_RESULTS['burst_range'])


@pytest.mark.parametrize('rangekwargs', [
    {'mass1': 1.4, 'mass2': 1.4},
    {'energy': 1e-2},
])
def test_range_timeseries(hoft, rangekwargs):
    with (
        null_context() if 'energy' in rangekwargs
        else pytest.warns(DeprecationWarning)
    ):
        trends = astro.range_timeseries(
            hoft,
            0.5,
            fftlength=0.25,
            overlap=0.125,
            method="median",
            nproc=2,
            **rangekwargs,
        )
    assert isinstance(trends, TimeSeries)
    assert trends.size == 2
    assert trends.unit == 'Mpc'
    assert trends.dt == 0.5 * units.second


@pytest.mark.parametrize('rangekwargs, outunit', [
    ({'mass1': 1.4, 'mass2': 1.4}, units.Mpc ** 2 / units.Hz),
    ({'energy': 1e-2}, units.Mpc),
])
def test_range_spectrogram(hoft, rangekwargs, outunit):
    with (
        null_context() if 'energy' in rangekwargs
        else pytest.warns(DeprecationWarning)
    ):
        spec = astro.range_spectrogram(
            hoft,
            0.5,
            fftlength=0.25,
            overlap=0.125,
            method="median",
            nproc=2,
            **rangekwargs,
        )
    assert isinstance(spec, Spectrogram)
    assert spec.shape[0] == 2
    assert spec.unit == outunit
    assert spec.f0 == spec.df
    assert spec.dt == 0.5 * units.second
    assert spec.df == 4 * units.Hertz


@pytest.mark.parametrize('range_func', [
    astro.range_timeseries,
    astro.range_spectrogram,
])
def test_range_incompatible_input(range_func):
    with pytest.raises(TypeError) as exc:
        range_func(2, 0.5)
    assert str(exc.value).startswith(
        'Could not produce a spectrogram from the input')
