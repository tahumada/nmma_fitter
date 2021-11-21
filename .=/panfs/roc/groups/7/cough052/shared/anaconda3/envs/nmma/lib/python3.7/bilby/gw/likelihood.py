
import os
import json
import copy
import math

import attr
import numpy as np
import pandas as pd
from scipy.special import logsumexp

from ..core.likelihood import Likelihood
from ..core.utils import BilbyJsonEncoder, decode_bilby_json
from ..core.utils import (
    logger, UnsortedInterp2d, create_frequency_series, create_time_series,
    speed_of_light, solar_mass, radius_of_earth, gravitational_constant,
    round_up_to_power_of_two)
from ..core.prior import Interped, Prior, Uniform, PriorDict, DeltaFunction
from .detector import InterferometerList, get_empty_interferometer, calibration
from .prior import BBHPriorDict, CBCPriorDict, Cosmological
from .source import lal_binary_black_hole
from .utils import (
    noise_weighted_inner_product, build_roq_weights, zenith_azimuth_to_ra_dec,
    ln_i0
)
from .waveform_generator import WaveformGenerator


class GravitationalWaveTransient(Likelihood):
    """ A gravitational-wave transient likelihood object

    This is the usual likelihood object to use for transient gravitational
    wave parameter estimation. It computes the log-likelihood in the frequency
    domain assuming a colored Gaussian noise model described by a power
    spectral density. See Thrane & Talbot (2019), arxiv.org/abs/1809.02293.

    Parameters
    ==========
    interferometers: list, bilby.gw.detector.InterferometerList
        A list of `bilby.detector.Interferometer` instances - contains the
        detector data and power spectral densities
    waveform_generator: `bilby.waveform_generator.WaveformGenerator`
        An object which computes the frequency-domain strain of the signal,
        given some set of parameters
    distance_marginalization: bool, optional
        If true, marginalize over distance in the likelihood.
        This uses a look up table calculated at run time.
        The distance prior is set to be a delta function at the minimum
        distance allowed in the prior being marginalised over.
    time_marginalization: bool, optional
        If true, marginalize over time in the likelihood.
        This uses a FFT to calculate the likelihood over a regularly spaced
        grid.
        In order to cover the whole space the prior is set to be uniform over
        the spacing of the array of times.
        If using time marginalisation and jitter_time is True a "jitter"
        parameter is added to the prior which modifies the position of the
        grid of times.
    phase_marginalization: bool, optional
        If true, marginalize over phase in the likelihood.
        This is done analytically using a Bessel function.
        The phase prior is set to be a delta function at phase=0.
    calibration_marginalization: bool, optional
        If true, marginalize over calibration response curves in the likelihood.
        This is done numerically over a number of calibration response curve realizations.
    priors: dict, optional
        If given, used in the distance and phase marginalization.
        Warning: when using marginalisation the dict is overwritten which will change the
        the dict you are passing in. If this behaviour is undesired, pass `priors.copy()`.
    distance_marginalization_lookup_table: (dict, str), optional
        If a dict, dictionary containing the lookup_table, distance_array,
        (distance) prior_array, and reference_distance used to construct
        the table.
        If a string the name of a file containing these quantities.
        The lookup table is stored after construction in either the
        provided string or a default location:
        '.distance_marginalization_lookup_dmin{}_dmax{}_n{}.npz'
    calibration_lookup_table: dict, optional
        If a dict, contains the arrays over which to marginalize for each interferometer or the filepaths of the
        calibration files.
        If not provided, but calibration_marginalization is used, then the appropriate file is created to
        contain the curves.
    number_of_response_curves: int, optional
        Number of curves from the calibration lookup table to use.
        Default is 1000.
    starting_index: int, optional
        Sets the index for the first realization of the calibration curve to be considered.
        This, coupled with number_of_response_curves, allows for restricting the set of curves used. This can be used
        when dealing with large frequency arrays to split the calculation into sections.
        Defaults to 0.
    jitter_time: bool, optional
        Whether to introduce a `time_jitter` parameter. This avoids either
        missing the likelihood peak, or introducing biases in the
        reconstructed time posterior due to an insufficient sampling frequency.
        Default is False, however using this parameter is strongly encouraged.
    reference_frame: (str, bilby.gw.detector.InterferometerList, list), optional
        Definition of the reference frame for the sky location.

        - :code:`sky`: sample in RA/dec, this is the default
        - e.g., :code:`"H1L1", ["H1", "L1"], InterferometerList(["H1", "L1"])`:
          sample in azimuth and zenith, `azimuth` and `zenith` defined in the
          frame where the z-axis is aligned the the vector connecting H1
          and L1.

    time_reference: str, optional
        Name of the reference for the sampled time parameter.

        - :code:`geocent`/:code:`geocenter`: sample in the time at the
          Earth's center, this is the default
        - e.g., :code:`H1`: sample in the time of arrival at H1

    Returns
    =======
    Likelihood: `bilby.core.likelihood.Likelihood`
        A likelihood object, able to compute the likelihood of the data given
        some model parameters

    """

    @attr.s
    class _CalculatedSNRs:
        d_inner_h = attr.ib()
        optimal_snr_squared = attr.ib()
        complex_matched_filter_snr = attr.ib()
        d_inner_h_array = attr.ib()
        optimal_snr_squared_array = attr.ib()
        d_inner_h_squared_tc_array = attr.ib()

    def __init__(
        self, interferometers, waveform_generator, time_marginalization=False,
        distance_marginalization=False, phase_marginalization=False, calibration_marginalization=False, priors=None,
        distance_marginalization_lookup_table=None, calibration_lookup_table=None,
        number_of_response_curves=1000, starting_index=0, jitter_time=True, reference_frame="sky",
        time_reference="geocenter"
    ):

        self.waveform_generator = waveform_generator
        super(GravitationalWaveTransient, self).__init__(dict())
        self.interferometers = InterferometerList(interferometers)
        self.time_marginalization = time_marginalization
        self.distance_marginalization = distance_marginalization
        self.phase_marginalization = phase_marginalization
        self.calibration_marginalization = calibration_marginalization
        self.priors = priors
        self._check_set_duration_and_sampling_frequency_of_waveform_generator()
        self.jitter_time = jitter_time
        self.reference_frame = reference_frame
        if "geocent" not in time_reference:
            self.time_reference = time_reference
            self.reference_ifo = get_empty_interferometer(self.time_reference)
            if self.time_marginalization:
                logger.info("Cannot marginalise over non-geocenter time.")
                self.time_marginalization = False
                self.jitter_time = False
        else:
            self.time_reference = "geocent"
            self.reference_ifo = None

        if self.time_marginalization:
            self._check_marginalized_prior_is_set(key='geocent_time')
            self._setup_time_marginalization()
            priors['geocent_time'] = float(self.interferometers.start_time)
            if self.jitter_time:
                priors['time_jitter'] = Uniform(
                    minimum=- self._delta_tc / 2,
                    maximum=self._delta_tc / 2,
                    boundary='periodic',
                    name="time_jitter",
                    latex_label="$t_j$"
                )
            self._marginalized_parameters.append('geocent_time')
        elif self.jitter_time:
            logger.debug(
                "Time jittering requested with non-time-marginalised "
                "likelihood, ignoring.")
            self.jitter_time = False

        if self.phase_marginalization:
            self._check_marginalized_prior_is_set(key='phase')
            priors['phase'] = float(0)
            self._marginalized_parameters.append('phase')

        if self.distance_marginalization:
            self._lookup_table_filename = None
            self._check_marginalized_prior_is_set(key='luminosity_distance')
            self._distance_array = np.linspace(
                self.priors['luminosity_distance'].minimum,
                self.priors['luminosity_distance'].maximum, int(1e4))
            self.distance_prior_array = np.array(
                [self.priors['luminosity_distance'].prob(distance)
                 for distance in self._distance_array])
            self._ref_dist = self.priors['luminosity_distance'].rescale(0.5)
            self._setup_distance_marginalization(
                distance_marginalization_lookup_table)
            for key in ['redshift', 'comoving_distance']:
                if key in priors:
                    del priors[key]
            priors['luminosity_distance'] = float(self._ref_dist)
            self._marginalized_parameters.append('luminosity_distance')

        if self.calibration_marginalization:
            self.number_of_response_curves = number_of_response_curves
            self.starting_index = starting_index
            self._setup_calibration_marginalization(calibration_lookup_table)
            self._marginalized_parameters.append('recalib_index')

    def __repr__(self):
        return self.__class__.__name__ + '(interferometers={},\n\twaveform_generator={},\n\ttime_marginalization={}, ' \
                                         'distance_marginalization={}, phase_marginalization={}, '\
                                         'calibration_marginalization={}, priors={})'\
            .format(self.interferometers, self.waveform_generator, self.time_marginalization,
                    self.distance_marginalization, self.phase_marginalization, self.calibration_marginalization,
                    self.priors)

    def _check_set_duration_and_sampling_frequency_of_waveform_generator(self):
        """ Check the waveform_generator has the same duration and
        sampling_frequency as the interferometers. If they are unset, then
        set them, if they differ, raise an error
        """

        attributes = ['duration', 'sampling_frequency', 'start_time']
        for attribute in attributes:
            wfg_attr = getattr(self.waveform_generator, attribute)
            ifo_attr = getattr(self.interferometers, attribute)
            if wfg_attr is None:
                logger.debug(
                    "The waveform_generator {} is None. Setting from the "
                    "provided interferometers.".format(attribute))
            elif wfg_attr != ifo_attr:
                logger.debug(
                    "The waveform_generator {} is not equal to that of the "
                    "provided interferometers. Overwriting the "
                    "waveform_generator.".format(attribute))
            setattr(self.waveform_generator, attribute, ifo_attr)

    def calculate_snrs(self, waveform_polarizations, interferometer):
        """
        Compute the snrs

        Parameters
        ==========
        waveform_polarizations: dict
            A dictionary of waveform polarizations and the corresponding array
        interferometer: bilby.gw.detector.Interferometer
            The bilby interferometer object

        """
        signal = interferometer.get_detector_response(
            waveform_polarizations, self.parameters)
        _mask = interferometer.frequency_mask

        if 'recalib_index' in self.parameters:
            signal[_mask] *= self.calibration_draws[interferometer.name][int(self.parameters['recalib_index'])]

        d_inner_h = interferometer.inner_product(signal=signal)
        optimal_snr_squared = interferometer.optimal_snr_squared(signal=signal)
        complex_matched_filter_snr = d_inner_h / (optimal_snr_squared**0.5)

        d_inner_h_array = None
        optimal_snr_squared_array = None

        if self.time_marginalization and self.calibration_marginalization:

            d_inner_h_integrand = np.tile(
                interferometer.frequency_domain_strain.conjugate() * signal /
                interferometer.power_spectral_density_array, (self.number_of_response_curves, 1)).T

            d_inner_h_integrand[_mask] *= self.calibration_draws[interferometer.name].T

            d_inner_h_array =\
                4 / self.waveform_generator.duration * np.fft.fft(
                    d_inner_h_integrand[0:-1], axis=0).T

            optimal_snr_squared_integrand = 4. / self.waveform_generator.duration *\
                np.abs(signal)**2 / interferometer.power_spectral_density_array
            optimal_snr_squared_array = np.dot(optimal_snr_squared_integrand[_mask],
                                               self.calibration_abs_draws[interferometer.name].T)

        elif self.time_marginalization and not self.calibration_marginalization:
            d_inner_h_array =\
                4 / self.waveform_generator.duration * np.fft.fft(
                    signal[0:-1] *
                    interferometer.frequency_domain_strain.conjugate()[0:-1] /
                    interferometer.power_spectral_density_array[0:-1])

        elif self.calibration_marginalization and ('recalib_index' not in self.parameters):
            d_inner_h_integrand = 4. / self.waveform_generator.duration * \
                interferometer.frequency_domain_strain.conjugate() * signal / \
                interferometer.power_spectral_density_array
            d_inner_h_array = np.dot(d_inner_h_integrand[_mask], self.calibration_draws[interferometer.name].T)

            optimal_snr_squared_integrand = 4. / self.waveform_generator.duration *\
                np.abs(signal)**2 / interferometer.power_spectral_density_array
            optimal_snr_squared_array = np.dot(optimal_snr_squared_integrand[_mask],
                                               self.calibration_abs_draws[interferometer.name].T)

        return self._CalculatedSNRs(
            d_inner_h=d_inner_h, optimal_snr_squared=optimal_snr_squared,
            complex_matched_filter_snr=complex_matched_filter_snr,
            d_inner_h_array=d_inner_h_array,
            optimal_snr_squared_array=optimal_snr_squared_array,
            d_inner_h_squared_tc_array=None)

    def _check_marginalized_prior_is_set(self, key):
        if key in self.priors and self.priors[key].is_fixed:
            raise ValueError(
                "Cannot use marginalized likelihood for {}: prior is fixed"
                .format(key))
        if key not in self.priors or not isinstance(
                self.priors[key], Prior):
            logger.warning(
                'Prior not provided for {}, using the BBH default.'.format(key))
            if key == 'geocent_time':
                self.priors[key] = Uniform(
                    self.interferometers.start_time,
                    self.interferometers.start_time + self.interferometers.duration)
            elif key == 'luminosity_distance':
                for key in ['redshift', 'comoving_distance']:
                    if key in self.priors:
                        if not isinstance(self.priors[key], Cosmological):
                            raise TypeError(
                                "To marginalize over {}, the prior must be specified as a "
                                "subclass of bilby.gw.prior.Cosmological.".format(key)
                            )
                        self.priors['luminosity_distance'] = self.priors[key].get_corresponding_prior(
                            'luminosity_distance'
                        )
                        del self.priors[key]
            else:
                self.priors[key] = BBHPriorDict()[key]

    @property
    def priors(self):
        return self._prior

    @priors.setter
    def priors(self, priors):
        if priors is not None:
            self._prior = priors.copy()
        elif any([self.time_marginalization, self.phase_marginalization,
                  self.distance_marginalization]):
            raise ValueError("You can't use a marginalized likelihood without specifying a priors")
        else:
            self._prior = None

    def noise_log_likelihood(self):
        log_l = 0
        for interferometer in self.interferometers:
            mask = interferometer.frequency_mask
            log_l -= noise_weighted_inner_product(
                interferometer.frequency_domain_strain[mask],
                interferometer.frequency_domain_strain[mask],
                interferometer.power_spectral_density_array[mask],
                self.waveform_generator.duration) / 2
        return float(np.real(log_l))

    def log_likelihood_ratio(self):
        waveform_polarizations =\
            self.waveform_generator.frequency_domain_strain(self.parameters)

        self.parameters.update(self.get_sky_frame_parameters())

        if waveform_polarizations is None:
            return np.nan_to_num(-np.inf)

        d_inner_h = 0.
        optimal_snr_squared = 0.
        complex_matched_filter_snr = 0.

        if self.time_marginalization and self.calibration_marginalization:
            if self.jitter_time:
                self.parameters['geocent_time'] += self.parameters['time_jitter']

            d_inner_h_array = np.zeros(
                (self.number_of_response_curves, len(self.interferometers.frequency_array[0:-1])),
                dtype=np.complex128)
            optimal_snr_squared_array = np.zeros(self.number_of_response_curves, dtype=np.complex128)

        elif self.time_marginalization:
            if self.jitter_time:
                self.parameters['geocent_time'] += self.parameters['time_jitter']
            d_inner_h_array = np.zeros(
                len(self.interferometers.frequency_array[0:-1]),
                dtype=np.complex128)

        elif self.calibration_marginalization:
            d_inner_h_array = np.zeros(self.number_of_response_curves, dtype=np.complex128)
            optimal_snr_squared_array = np.zeros(self.number_of_response_curves, dtype=np.complex128)

        for interferometer in self.interferometers:
            per_detector_snr = self.calculate_snrs(
                waveform_polarizations=waveform_polarizations,
                interferometer=interferometer)

            d_inner_h += per_detector_snr.d_inner_h
            optimal_snr_squared += np.real(per_detector_snr.optimal_snr_squared)
            complex_matched_filter_snr += per_detector_snr.complex_matched_filter_snr

            if self.time_marginalization or self.calibration_marginalization:
                d_inner_h_array += per_detector_snr.d_inner_h_array

            if self.calibration_marginalization:
                optimal_snr_squared_array += per_detector_snr.optimal_snr_squared_array

        if self.calibration_marginalization and self.time_marginalization:
            log_l = self.time_and_calibration_marginalized_likelihood(
                d_inner_h_array=d_inner_h_array,
                h_inner_h=optimal_snr_squared_array)
            if self.jitter_time:
                self.parameters['geocent_time'] -= self.parameters['time_jitter']

        elif self.calibration_marginalization:
            log_l = self.calibration_marginalized_likelihood(
                d_inner_h_calibration_array=d_inner_h_array,
                h_inner_h=optimal_snr_squared_array)

        elif self.time_marginalization:
            log_l = self.time_marginalized_likelihood(
                d_inner_h_tc_array=d_inner_h_array,
                h_inner_h=optimal_snr_squared)
            if self.jitter_time:
                self.parameters['geocent_time'] -= self.parameters['time_jitter']

        elif self.distance_marginalization:
            log_l = self.distance_marginalized_likelihood(
                d_inner_h=d_inner_h, h_inner_h=optimal_snr_squared)

        elif self.phase_marginalization:
            log_l = self.phase_marginalized_likelihood(
                d_inner_h=d_inner_h, h_inner_h=optimal_snr_squared)

        else:
            log_l = np.real(d_inner_h) - optimal_snr_squared / 2

        return float(log_l.real)

    def generate_posterior_sample_from_marginalized_likelihood(self):
        """
        Reconstruct the distance posterior from a run which used a likelihood
        which explicitly marginalised over time/distance/phase.

        See Eq. (C29-C32) of https://arxiv.org/abs/1809.02293

        Returns
        =======
        sample: dict
            Returns the parameters with new samples.

        Notes
        =====
        This involves a deepcopy of the signal to avoid issues with waveform
        caching, as the signal is overwritten in place.
        """
        if any([self.phase_marginalization, self.distance_marginalization,
                self.time_marginalization, self.calibration_marginalization]):
            signal_polarizations = copy.deepcopy(
                self.waveform_generator.frequency_domain_strain(
                    self.parameters))
        else:
            return self.parameters

        if self.calibration_marginalization and self.time_marginalization:
            raise AttributeError(
                "Cannot use time and calibration marginalization simultaneously for regeneration at the moment!"
                "The matrix manipulation has not been tested.")

        if self.calibration_marginalization:
            new_calibration = self.generate_calibration_sample_from_marginalized_likelihood(
                signal_polarizations=signal_polarizations)
            self.parameters['recalib_index'] = new_calibration
        if self.time_marginalization:
            new_time = self.generate_time_sample_from_marginalized_likelihood(
                signal_polarizations=signal_polarizations)
            self.parameters['geocent_time'] = new_time
        if self.distance_marginalization:
            new_distance = self.generate_distance_sample_from_marginalized_likelihood(
                signal_polarizations=signal_polarizations)
            self.parameters['luminosity_distance'] = new_distance
        if self.phase_marginalization:
            new_phase = self.generate_phase_sample_from_marginalized_likelihood(
                signal_polarizations=signal_polarizations)
            self.parameters['phase'] = new_phase
        return self.parameters.copy()

    def generate_calibration_sample_from_marginalized_likelihood(
            self, signal_polarizations=None):
        """
        Generate a single sample from the posterior distribution for the set of calibration response curves when
        explicitly marginalizing over the calibration uncertainty.

        Parameters
        ----------
        signal_polarizations: dict, optional
            Polarizations modes of the template.

        Returns
        -------
        new_calibration: dict
            Sample set from the calibration posterior
        """
        if 'recalib_index' in self.parameters:
            self.parameters.pop('recalib_index')
        self.parameters.update(self.get_sky_frame_parameters())
        if signal_polarizations is None:
            signal_polarizations = \
                self.waveform_generator.frequency_domain_strain(self.parameters)

        log_like = self.get_calibration_log_likelihoods(signal_polarizations=signal_polarizations)

        calibration_post = np.exp(log_like - max(log_like))
        calibration_post /= np.sum(calibration_post)

        new_calibration = np.random.choice(self.number_of_response_curves, p=calibration_post)

        return new_calibration

    def generate_time_sample_from_marginalized_likelihood(
            self, signal_polarizations=None):
        """
        Generate a single sample from the posterior distribution for coalescence
        time when using a likelihood which explicitly marginalises over time.

        In order to resolve the posterior we artificially upsample to 16kHz.

        See Eq. (C29-C32) of https://arxiv.org/abs/1809.02293

        Parameters
        ==========
        signal_polarizations: dict, optional
            Polarizations modes of the template.

        Returns
        =======
        new_time: float
            Sample from the time posterior.
        """
        self.parameters.update(self.get_sky_frame_parameters())
        if self.jitter_time:
            self.parameters['geocent_time'] += self.parameters['time_jitter']
        if signal_polarizations is None:
            signal_polarizations = \
                self.waveform_generator.frequency_domain_strain(self.parameters)

        times = create_time_series(
            sampling_frequency=16384,
            starting_time=self.parameters['geocent_time'] - self.waveform_generator.start_time,
            duration=self.waveform_generator.duration)
        times = times % self.waveform_generator.duration
        times += self.waveform_generator.start_time

        prior = self.priors["geocent_time"]
        in_prior = (times >= prior.minimum) & (times < prior.maximum)
        times = times[in_prior]

        n_time_steps = int(self.waveform_generator.duration * 16384)
        d_inner_h = np.zeros(len(times), dtype=complex)
        psd = np.ones(n_time_steps)
        signal_long = np.zeros(n_time_steps, dtype=complex)
        data = np.zeros(n_time_steps, dtype=complex)
        h_inner_h = np.zeros(1)
        for ifo in self.interferometers:
            ifo_length = len(ifo.frequency_domain_strain)
            mask = ifo.frequency_mask
            signal = ifo.get_detector_response(
                signal_polarizations, self.parameters)
            signal_long[:ifo_length] = signal
            data[:ifo_length] = np.conj(ifo.frequency_domain_strain)
            psd[:ifo_length][mask] = ifo.power_spectral_density_array[mask]
            d_inner_h += np.fft.fft(signal_long * data / psd)[in_prior]
            h_inner_h += ifo.optimal_snr_squared(signal=signal).real

        if self.distance_marginalization:
            time_log_like = self.distance_marginalized_likelihood(
                d_inner_h, h_inner_h)
        elif self.phase_marginalization:
            time_log_like = ln_i0(abs(d_inner_h)) - h_inner_h.real / 2
        else:
            time_log_like = (d_inner_h.real - h_inner_h.real / 2)

        time_prior_array = self.priors['geocent_time'].prob(times)
        time_post = (
            np.exp(time_log_like - max(time_log_like)) * time_prior_array)

        keep = (time_post > max(time_post) / 1000)
        if sum(keep) < 3:
            keep[1:-1] = keep[1:-1] | keep[2:] | keep[:-2]
        time_post = time_post[keep]
        times = times[keep]

        new_time = Interped(times, time_post).sample()
        return new_time

    def generate_distance_sample_from_marginalized_likelihood(
            self, signal_polarizations=None):
        """
        Generate a single sample from the posterior distribution for luminosity
        distance when using a likelihood which explicitly marginalises over
        distance.

        See Eq. (C29-C32) of https://arxiv.org/abs/1809.02293

        Parameters
        ==========
        signal_polarizations: dict, optional
            Polarizations modes of the template.
            Note: These are rescaled in place after the distance sample is
            generated to allow further parameter reconstruction to occur.

        Returns
        =======
        new_distance: float
            Sample from the distance posterior.
        """
        self.parameters.update(self.get_sky_frame_parameters())
        if signal_polarizations is None:
            signal_polarizations = \
                self.waveform_generator.frequency_domain_strain(self.parameters)

        d_inner_h, h_inner_h = self._calculate_inner_products(signal_polarizations)

        d_inner_h_dist = (
            d_inner_h * self.parameters['luminosity_distance'] /
            self._distance_array)

        h_inner_h_dist = (
            h_inner_h * self.parameters['luminosity_distance']**2 /
            self._distance_array**2)

        if self.phase_marginalization:
            distance_log_like = (
                ln_i0(abs(d_inner_h_dist)) -
                h_inner_h_dist.real / 2
            )
        else:
            distance_log_like = (d_inner_h_dist.real - h_inner_h_dist.real / 2)

        distance_post = (np.exp(distance_log_like - max(distance_log_like)) *
                         self.distance_prior_array)

        new_distance = Interped(
            self._distance_array, distance_post).sample()

        self._rescale_signal(signal_polarizations, new_distance)
        return new_distance

    def _calculate_inner_products(self, signal_polarizations):
        d_inner_h = 0
        h_inner_h = 0
        for interferometer in self.interferometers:
            per_detector_snr = self.calculate_snrs(
                signal_polarizations, interferometer)

            d_inner_h += per_detector_snr.d_inner_h
            h_inner_h += per_detector_snr.optimal_snr_squared
        return d_inner_h, h_inner_h

    def generate_phase_sample_from_marginalized_likelihood(
            self, signal_polarizations=None):
        """
        Generate a single sample from the posterior distribution for phase when
        using a likelihood which explicitly marginalises over phase.

        See Eq. (C29-C32) of https://arxiv.org/abs/1809.02293

        Parameters
        ==========
        signal_polarizations: dict, optional
            Polarizations modes of the template.

        Returns
        =======
        new_phase: float
            Sample from the phase posterior.

        Notes
        =====
        This is only valid when assumes that mu(phi) \propto exp(-2i phi).
        """
        self.parameters.update(self.get_sky_frame_parameters())
        if signal_polarizations is None:
            signal_polarizations = \
                self.waveform_generator.frequency_domain_strain(self.parameters)
        d_inner_h, h_inner_h = self._calculate_inner_products(signal_polarizations)

        phases = np.linspace(0, 2 * np.pi, 101)
        phasor = np.exp(-2j * phases)
        phase_log_post = d_inner_h * phasor - h_inner_h / 2
        phase_post = np.exp(phase_log_post.real - max(phase_log_post.real))
        new_phase = Interped(phases, phase_post).sample()
        return new_phase

    def distance_marginalized_likelihood(self, d_inner_h, h_inner_h):
        d_inner_h_ref, h_inner_h_ref = self._setup_rho(
            d_inner_h, h_inner_h)
        if self.phase_marginalization:
            d_inner_h_ref = np.abs(d_inner_h_ref)
        else:
            d_inner_h_ref = np.real(d_inner_h_ref)

        return self._interp_dist_margd_loglikelihood(
            d_inner_h_ref, h_inner_h_ref)

    def phase_marginalized_likelihood(self, d_inner_h, h_inner_h):
        d_inner_h = ln_i0(abs(d_inner_h))

        if self.calibration_marginalization and self.time_marginalization:
            return d_inner_h - np.outer(h_inner_h, np.ones(np.shape(d_inner_h)[1])) / 2
        else:
            return d_inner_h - h_inner_h / 2

    def time_marginalized_likelihood(self, d_inner_h_tc_array, h_inner_h):
        if self.distance_marginalization:
            log_l_tc_array = self.distance_marginalized_likelihood(
                d_inner_h=d_inner_h_tc_array, h_inner_h=h_inner_h)
        elif self.phase_marginalization:
            log_l_tc_array = self.phase_marginalized_likelihood(
                d_inner_h=d_inner_h_tc_array,
                h_inner_h=h_inner_h)
        else:
            log_l_tc_array = np.real(d_inner_h_tc_array) - h_inner_h / 2
        times = self._times
        if self.jitter_time:
            times = self._times + self.parameters['time_jitter']
        time_prior_array = self.priors['geocent_time'].prob(times) * self._delta_tc
        return logsumexp(log_l_tc_array, b=time_prior_array)

    def time_and_calibration_marginalized_likelihood(self, d_inner_h_array, h_inner_h):
        times = self._times
        if self.jitter_time:
            times = self._times + self.parameters['time_jitter']

        _time_prior = self.priors['geocent_time']
        time_mask = np.logical_and((times >= _time_prior.minimum), (times <= _time_prior.maximum))
        times = times[time_mask]
        time_probs = self.priors['geocent_time'].prob(times) * self._delta_tc

        d_inner_h_array = d_inner_h_array[:, time_mask]
        h_inner_h = h_inner_h

        if self.distance_marginalization:
            log_l_array = self.distance_marginalized_likelihood(
                d_inner_h=d_inner_h_array, h_inner_h=h_inner_h)
        elif self.phase_marginalization:
            log_l_array = self.phase_marginalized_likelihood(
                d_inner_h=d_inner_h_array,
                h_inner_h=h_inner_h)
        else:
            log_l_array = np.real(d_inner_h_array) - np.outer(h_inner_h, np.ones(np.shape(d_inner_h_array)[1])) / 2

        prior_array = np.outer(time_probs, 1. / self.number_of_response_curves * np.ones(len(h_inner_h))).T

        return logsumexp(log_l_array, b=prior_array)

    def get_calibration_log_likelihoods(self, signal_polarizations=None):
        self.parameters.update(self.get_sky_frame_parameters())
        if signal_polarizations is None:
            signal_polarizations =\
                self.waveform_generator.frequency_domain_strain(self.parameters)

        d_inner_h = 0.
        optimal_snr_squared = 0.
        complex_matched_filter_snr = 0.
        d_inner_h_array = np.zeros(self.number_of_response_curves, dtype=np.complex128)
        optimal_snr_squared_array = np.zeros(self.number_of_response_curves, dtype=np.complex128)

        for interferometer in self.interferometers:
            per_detector_snr = self.calculate_snrs(
                waveform_polarizations=signal_polarizations,
                interferometer=interferometer)

            d_inner_h += per_detector_snr.d_inner_h
            optimal_snr_squared += np.real(per_detector_snr.optimal_snr_squared)
            complex_matched_filter_snr += per_detector_snr.complex_matched_filter_snr
            d_inner_h_array += per_detector_snr.d_inner_h_array
            optimal_snr_squared_array += per_detector_snr.optimal_snr_squared_array

        if self.distance_marginalization:
            log_l_cal_array = self.distance_marginalized_likelihood(
                d_inner_h=d_inner_h_array, h_inner_h=optimal_snr_squared_array)
        elif self.phase_marginalization:
            log_l_cal_array = self.phase_marginalized_likelihood(
                d_inner_h=d_inner_h_array,
                h_inner_h=optimal_snr_squared_array)
        else:
            log_l_cal_array = np.real(d_inner_h_array - optimal_snr_squared_array / 2)

        return log_l_cal_array

    def calibration_marginalized_likelihood(self, d_inner_h_calibration_array, h_inner_h):
        if self.distance_marginalization:
            log_l_cal_array = self.distance_marginalized_likelihood(
                d_inner_h=d_inner_h_calibration_array, h_inner_h=h_inner_h)
        elif self.phase_marginalization:
            log_l_cal_array = self.phase_marginalized_likelihood(
                d_inner_h=d_inner_h_calibration_array,
                h_inner_h=h_inner_h)
        else:
            log_l_cal_array = np.real(d_inner_h_calibration_array - h_inner_h / 2)

        return logsumexp(log_l_cal_array) - np.log(self.number_of_response_curves)

    def _setup_rho(self, d_inner_h, optimal_snr_squared):
        optimal_snr_squared_ref = (optimal_snr_squared.real *
                                   self.parameters['luminosity_distance'] ** 2 /
                                   self._ref_dist ** 2.)
        d_inner_h_ref = (d_inner_h * self.parameters['luminosity_distance'] /
                         self._ref_dist)
        return d_inner_h_ref, optimal_snr_squared_ref

    def log_likelihood(self):
        return self.log_likelihood_ratio() + self.noise_log_likelihood()

    @property
    def _delta_distance(self):
        return self._distance_array[1] - self._distance_array[0]

    @property
    def _dist_multiplier(self):
        ''' Maximum value of ref_dist/dist_array '''
        return self._ref_dist / self._distance_array[0]

    @property
    def _optimal_snr_squared_ref_array(self):
        """ Optimal filter snr at fiducial distance of ref_dist Mpc """
        return np.logspace(-5, 10, self._dist_margd_loglikelihood_array.shape[0])

    @property
    def _d_inner_h_ref_array(self):
        """ Matched filter snr at fiducial distance of ref_dist Mpc """
        if self.phase_marginalization:
            return np.logspace(-5, 10, self._dist_margd_loglikelihood_array.shape[1])
        else:
            n_negative = self._dist_margd_loglikelihood_array.shape[1] // 2
            n_positive = self._dist_margd_loglikelihood_array.shape[1] - n_negative
            return np.hstack((
                -np.logspace(3, -3, n_negative), np.logspace(-3, 10, n_positive)
            ))

    def _setup_distance_marginalization(self, lookup_table=None):
        if isinstance(lookup_table, str) or lookup_table is None:
            self.cached_lookup_table_filename = lookup_table
            lookup_table = self.load_lookup_table(
                self.cached_lookup_table_filename)
        if isinstance(lookup_table, dict):
            if self._test_cached_lookup_table(lookup_table):
                self._dist_margd_loglikelihood_array = lookup_table[
                    'lookup_table']
            else:
                self._create_lookup_table()
        else:
            self._create_lookup_table()
        self._interp_dist_margd_loglikelihood = UnsortedInterp2d(
            self._d_inner_h_ref_array, self._optimal_snr_squared_ref_array,
            self._dist_margd_loglikelihood_array, kind='cubic', fill_value=-np.inf)

    @property
    def cached_lookup_table_filename(self):
        if self._lookup_table_filename is None:
            self._lookup_table_filename = (
                '.distance_marginalization_lookup.npz')
        return self._lookup_table_filename

    @cached_lookup_table_filename.setter
    def cached_lookup_table_filename(self, filename):
        if isinstance(filename, str):
            if filename[-4:] != '.npz':
                filename += '.npz'
        self._lookup_table_filename = filename

    def load_lookup_table(self, filename):
        if os.path.exists(filename):
            try:
                loaded_file = dict(np.load(filename))
            except AttributeError as e:
                logger.warning(e)
                self._create_lookup_table()
                return None
            match, failure = self._test_cached_lookup_table(loaded_file)
            if match:
                logger.info('Loaded distance marginalisation lookup table from '
                            '{}.'.format(filename))
                return loaded_file
            else:
                logger.info('Loaded distance marginalisation lookup table does '
                            'not match for {}.'.format(failure))
        elif isinstance(filename, str):
            logger.info('Distance marginalisation file {} does not '
                        'exist'.format(filename))
        return None

    def cache_lookup_table(self):
        np.savez(self.cached_lookup_table_filename,
                 distance_array=self._distance_array,
                 prior_array=self.distance_prior_array,
                 lookup_table=self._dist_margd_loglikelihood_array,
                 reference_distance=self._ref_dist,
                 phase_marginalization=self.phase_marginalization)

    def _test_cached_lookup_table(self, loaded_file):
        pairs = dict(
            distance_array=self._distance_array,
            prior_array=self.distance_prior_array,
            reference_distance=self._ref_dist,
            phase_marginalization=self.phase_marginalization)
        for key in pairs:
            if key not in loaded_file:
                return False, key
            elif not np.array_equal(np.atleast_1d(loaded_file[key]),
                                    np.atleast_1d(pairs[key])):
                return False, key
        return True, None

    def _create_lookup_table(self):
        """ Make the lookup table """
        from tqdm.auto import tqdm
        logger.info('Building lookup table for distance marginalisation.')

        self._dist_margd_loglikelihood_array = np.zeros((400, 800))
        scaling = self._ref_dist / self._distance_array
        d_inner_h_array_full = np.outer(self._d_inner_h_ref_array, scaling)
        h_inner_h_array_full = np.outer(self._optimal_snr_squared_ref_array, scaling ** 2)
        if self.phase_marginalization:
            d_inner_h_array_full = ln_i0(abs(d_inner_h_array_full))
        prior_term = self.distance_prior_array * self._delta_distance
        for ii, optimal_snr_squared_array in tqdm(
            enumerate(h_inner_h_array_full), total=len(self._optimal_snr_squared_ref_array)
        ):
            for jj, d_inner_h_array in enumerate(d_inner_h_array_full):
                self._dist_margd_loglikelihood_array[ii][jj] = logsumexp(
                    d_inner_h_array - optimal_snr_squared_array / 2,
                    b=prior_term
                )
        log_norm = logsumexp(
            0 / self._distance_array, b=self.distance_prior_array * self._delta_distance
        )
        self._dist_margd_loglikelihood_array -= log_norm
        self.cache_lookup_table()

    def _setup_phase_marginalization(self, min_bound=-5, max_bound=10):
        logger.warning(
            "The _setup_phase_marginalization method is deprecated and will be removed, "
            "please update the implementation of phase marginalization "
            "to use bilby.gw.utils.ln_i0"
        )

    @staticmethod
    def _bessel_function_interped(xx):
        logger.warning(
            "The _bessel_function_interped method is deprecated and will be removed, "
            "please update the implementation of phase marginalization "
            "to use bilby.gw.utils.ln_i0"
        )
        return ln_i0(xx) + xx

    def _setup_time_marginalization(self):
        self._delta_tc = 2 / self.waveform_generator.sampling_frequency
        self._times =\
            self.interferometers.start_time + np.linspace(
                0, self.interferometers.duration,
                int(self.interferometers.duration / 2 *
                    self.waveform_generator.sampling_frequency + 1))[1:]
        self.time_prior_array = \
            self.priors['geocent_time'].prob(self._times) * self._delta_tc

    def _setup_calibration_marginalization(self, calibration_lookup_table):
        if calibration_lookup_table is None:
            calibration_lookup_table = {}
        self.calibration_draws = {}
        self.calibration_abs_draws = {}
        self.calibration_parameter_draws = {}
        for interferometer in self.interferometers:

            # Force the priors
            calibration_priors = PriorDict()
            for key in self.priors.keys():
                if 'recalib' in key and interferometer.name in key:
                    calibration_priors[key] = copy.copy(self.priors[key])
                    self.priors[key] = DeltaFunction(0.0)

            # If there is no entry in the lookup table, make an empty one
            if interferometer.name not in calibration_lookup_table.keys():
                calibration_lookup_table[interferometer.name] =\
                    f'{interferometer.name}_calibration_file.h5'

            # If the interferometer lookup table file exists, generate the curves from it
            if os.path.exists(calibration_lookup_table[interferometer.name]):
                self.calibration_draws[interferometer.name] =\
                    calibration.read_calibration_file(
                        calibration_lookup_table[interferometer.name], self.interferometers.frequency_array,
                        self.number_of_response_curves, self.starting_index)

            else:  # generate the fake curves
                from tqdm.auto import tqdm
                self.calibration_parameter_draws[interferometer.name] =\
                    pd.DataFrame(calibration_priors.sample(self.number_of_response_curves))

                self.calibration_draws[interferometer.name] = \
                    np.zeros((self.number_of_response_curves, len(interferometer.frequency_array)), dtype=complex)

                for i in tqdm(range(self.number_of_response_curves)):
                    self.calibration_draws[interferometer.name][i, :] =\
                        interferometer.calibration_model.get_calibration_factor(
                            interferometer.frequency_array,
                            prefix='recalib_{}_'.format(interferometer.name),
                            **self.calibration_parameter_draws[interferometer.name].iloc[i])

                calibration.write_calibration_file(
                    calibration_lookup_table[interferometer.name],
                    self.interferometers.frequency_array,
                    self.calibration_draws[interferometer.name],
                    self.calibration_parameter_draws[interferometer.name])

            interferometer.calibration_model = calibration.Recalibrate()

            _mask = interferometer.frequency_mask
            self.calibration_draws[interferometer.name] = self.calibration_draws[interferometer.name][:, _mask]
            self.calibration_abs_draws[interferometer.name] =\
                np.abs(self.calibration_draws[interferometer.name])**2

    @property
    def interferometers(self):
        return self._interferometers

    @interferometers.setter
    def interferometers(self, interferometers):
        self._interferometers = InterferometerList(interferometers)

    def _rescale_signal(self, signal, new_distance):
        for mode in signal:
            signal[mode] *= self._ref_dist / new_distance

    @property
    def reference_frame(self):
        return self._reference_frame

    @property
    def _reference_frame_str(self):
        if isinstance(self.reference_frame, str):
            return self.reference_frame
        else:
            return "".join([ifo.name for ifo in self.reference_frame])

    @reference_frame.setter
    def reference_frame(self, frame):
        if frame == "sky":
            self._reference_frame = frame
        elif isinstance(frame, InterferometerList):
            self._reference_frame = frame[:2]
        elif isinstance(frame, list):
            self._reference_frame = InterferometerList(frame[:2])
        elif isinstance(frame, str):
            self._reference_frame = InterferometerList([frame[:2], frame[2:4]])
        else:
            raise ValueError("Unable to parse reference frame {}".format(frame))

    def get_sky_frame_parameters(self):
        time = self.parameters['{}_time'.format(self.time_reference)]
        if not self.reference_frame == "sky":
            ra, dec = zenith_azimuth_to_ra_dec(
                self.parameters['zenith'], self.parameters['azimuth'],
                time, self.reference_frame)
        else:
            ra = self.parameters["ra"]
            dec = self.parameters["dec"]
        if "geocent" not in self.time_reference:
            geocent_time = (
                time - self.reference_ifo.time_delay_from_geocenter(
                    ra=ra, dec=dec, time=time
                )
            )
        else:
            geocent_time = self.parameters["geocent_time"]
        return dict(ra=ra, dec=dec, geocent_time=geocent_time)

    @property
    def lal_version(self):
        try:
            from lal import git_version, __version__
            lal_version = str(__version__)
            logger.info("Using lal version {}".format(lal_version))
            lal_git_version = str(git_version.verbose_msg).replace("\n", ";")
            logger.info("Using lal git version {}".format(lal_git_version))
            return "lal_version={}, lal_git_version={}".format(lal_version, lal_git_version)
        except (ImportError, AttributeError):
            return "N/A"

    @property
    def lalsimulation_version(self):
        try:
            from lalsimulation import git_version, __version__
            lalsim_version = str(__version__)
            logger.info("Using lalsimulation version {}".format(lalsim_version))
            lalsim_git_version = str(git_version.verbose_msg).replace("\n", ";")
            logger.info("Using lalsimulation git version {}".format(lalsim_git_version))
            return "lalsimulation_version={}, lalsimulation_git_version={}".format(lalsim_version, lalsim_git_version)
        except (ImportError, AttributeError):
            return "N/A"

    @property
    def meta_data(self):
        return dict(
            interferometers=self.interferometers.meta_data,
            time_marginalization=self.time_marginalization,
            phase_marginalization=self.phase_marginalization,
            distance_marginalization=self.distance_marginalization,
            calibration_marginalization=self.calibration_marginalization,
            waveform_generator_class=self.waveform_generator.__class__,
            waveform_arguments=self.waveform_generator.waveform_arguments,
            frequency_domain_source_model=self.waveform_generator.frequency_domain_source_model,
            parameter_conversion=self.waveform_generator.parameter_conversion,
            sampling_frequency=self.waveform_generator.sampling_frequency,
            duration=self.waveform_generator.duration,
            start_time=self.waveform_generator.start_time,
            time_reference=self.time_reference,
            reference_frame=self._reference_frame_str,
            lal_version=self.lal_version,
            lalsimulation_version=self.lalsimulation_version)


class BasicGravitationalWaveTransient(Likelihood):

    def __init__(self, interferometers, waveform_generator):
        """

        A likelihood object, able to compute the likelihood of the data given
        some model parameters

        The simplest frequency-domain gravitational wave transient likelihood. Does
        not include distance/phase marginalization.


        Parameters
        ==========
        interferometers: list
            A list of `bilby.gw.detector.Interferometer` instances - contains the
            detector data and power spectral densities
        waveform_generator: bilby.gw.waveform_generator.WaveformGenerator
            An object which computes the frequency-domain strain of the signal,
            given some set of parameters

        """
        super(BasicGravitationalWaveTransient, self).__init__(dict())
        self.interferometers = interferometers
        self.waveform_generator = waveform_generator

    def __repr__(self):
        return self.__class__.__name__ + '(interferometers={},\n\twaveform_generator={})'\
            .format(self.interferometers, self.waveform_generator)

    def noise_log_likelihood(self):
        """ Calculates the real part of noise log-likelihood

        Returns
        =======
        float: The real part of the noise log likelihood

        """
        log_l = 0
        for interferometer in self.interferometers:
            log_l -= 2. / self.waveform_generator.duration * np.sum(
                abs(interferometer.frequency_domain_strain) ** 2 /
                interferometer.power_spectral_density_array)
        return log_l.real

    def log_likelihood(self):
        """ Calculates the real part of log-likelihood value

        Returns
        =======
        float: The real part of the log likelihood

        """
        log_l = 0
        waveform_polarizations =\
            self.waveform_generator.frequency_domain_strain(
                self.parameters.copy())
        if waveform_polarizations is None:
            return np.nan_to_num(-np.inf)
        for interferometer in self.interferometers:
            log_l += self.log_likelihood_interferometer(
                waveform_polarizations, interferometer)
        return log_l.real

    def log_likelihood_interferometer(self, waveform_polarizations,
                                      interferometer):
        """

        Parameters
        ==========
        waveform_polarizations: dict
            Dictionary containing the desired waveform polarization modes and the related strain
        interferometer: bilby.gw.detector.Interferometer
            The Interferometer object we want to have the log-likelihood for

        Returns
        =======
        float: The real part of the log-likelihood for this interferometer

        """
        signal_ifo = interferometer.get_detector_response(
            waveform_polarizations, self.parameters)

        log_l = - 2. / self.waveform_generator.duration * np.vdot(
            interferometer.frequency_domain_strain - signal_ifo,
            (interferometer.frequency_domain_strain - signal_ifo) /
            interferometer.power_spectral_density_array)
        return log_l.real


class ROQGravitationalWaveTransient(GravitationalWaveTransient):
    """A reduced order quadrature likelihood object

    This uses the method described in Smith et al., (2016) Phys. Rev. D 94,
    044031. A public repository of the ROQ data is available from
    https://git.ligo.org/lscsoft/ROQ_data.

    Parameters
    ==========
    interferometers: list, bilby.gw.detector.InterferometerList
        A list of `bilby.detector.Interferometer` instances - contains the
        detector data and power spectral densities
    waveform_generator: `bilby.waveform_generator.WaveformGenerator`
        An object which computes the frequency-domain strain of the signal,
        given some set of parameters
    linear_matrix: str, array_like
        Either a string point to the file from which to load the linear_matrix
        array, or the array itself.
    quadratic_matrix: str, array_like
        Either a string point to the file from which to load the
        quadratic_matrix array, or the array itself.
    roq_params: str, array_like
        Parameters describing the domain of validity of the ROQ basis.
    roq_params_check: bool
        If true, run tests using the roq_params to check the prior and data are
        valid for the ROQ
    roq_scale_factor: float
        The ROQ scale factor used.
    priors: dict, bilby.prior.PriorDict
        A dictionary of priors containing at least the geocent_time prior
        Warning: when using marginalisation the dict is overwritten which will change the
        the dict you are passing in. If this behaviour is undesired, pass `priors.copy()`.
    distance_marginalization_lookup_table: (dict, str), optional
        If a dict, dictionary containing the lookup_table, distance_array,
        (distance) prior_array, and reference_distance used to construct
        the table.
        If a string the name of a file containing these quantities.
        The lookup table is stored after construction in either the
        provided string or a default location:
        '.distance_marginalization_lookup_dmin{}_dmax{}_n{}.npz'
    reference_frame: (str, bilby.gw.detector.InterferometerList, list), optional
        Definition of the reference frame for the sky location.
        - "sky": sample in RA/dec, this is the default
        - e.g., "H1L1", ["H1", "L1"], InterferometerList(["H1", "L1"]):
          sample in azimuth and zenith, `azimuth` and `zenith` defined in the
          frame where the z-axis is aligned the the vector connecting H1
          and L1.
    time_reference: str, optional
        Name of the reference for the sampled time parameter.
        - "geocent"/"geocenter": sample in the time at the Earth's center,
          this is the default
        - e.g., "H1": sample in the time of arrival at H1

    """
    def __init__(
        self, interferometers, waveform_generator, priors,
        weights=None, linear_matrix=None, quadratic_matrix=None,
        roq_params=None, roq_params_check=True, roq_scale_factor=1,
        distance_marginalization=False, phase_marginalization=False,
        distance_marginalization_lookup_table=None,
        reference_frame="sky", time_reference="geocenter"

    ):
        super(ROQGravitationalWaveTransient, self).__init__(
            interferometers=interferometers,
            waveform_generator=waveform_generator, priors=priors,
            distance_marginalization=distance_marginalization,
            phase_marginalization=phase_marginalization,
            time_marginalization=False,
            distance_marginalization_lookup_table=distance_marginalization_lookup_table,
            jitter_time=False,
            reference_frame=reference_frame,
            time_reference=time_reference
        )

        self.roq_params_check = roq_params_check
        self.roq_scale_factor = roq_scale_factor
        if isinstance(roq_params, np.ndarray) or roq_params is None:
            self.roq_params = roq_params
        elif isinstance(roq_params, str):
            self.roq_params_file = roq_params
            self.roq_params = np.genfromtxt(roq_params, names=True)
        else:
            raise TypeError("roq_params should be array or str")
        if isinstance(weights, dict):
            self.weights = weights
        elif isinstance(weights, str):
            self.weights = self.load_weights(weights)
        else:
            self.weights = dict()
            if isinstance(linear_matrix, str):
                logger.info(
                    "Loading linear matrix from {}".format(linear_matrix))
                linear_matrix = np.load(linear_matrix).T
            if isinstance(quadratic_matrix, str):
                logger.info(
                    "Loading quadratic_matrix from {}".format(quadratic_matrix))
                quadratic_matrix = np.load(quadratic_matrix).T
            self._set_weights(linear_matrix=linear_matrix,
                              quadratic_matrix=quadratic_matrix)
        self.frequency_nodes_linear =\
            waveform_generator.waveform_arguments['frequency_nodes_linear']
        self.frequency_nodes_quadratic = \
            waveform_generator.waveform_arguments['frequency_nodes_quadratic']

    def calculate_snrs(self, waveform_polarizations, interferometer):
        """
        Compute the snrs for ROQ

        Parameters
        ==========
        waveform_polarizations: waveform
        interferometer: bilby.gw.detector.Interferometer

        """

        f_plus = interferometer.antenna_response(
            self.parameters['ra'], self.parameters['dec'],
            self.parameters['geocent_time'], self.parameters['psi'], 'plus')
        f_cross = interferometer.antenna_response(
            self.parameters['ra'], self.parameters['dec'],
            self.parameters['geocent_time'], self.parameters['psi'], 'cross')

        dt = interferometer.time_delay_from_geocenter(
            self.parameters['ra'], self.parameters['dec'],
            self.parameters['geocent_time'])
        dt_geocent = self.parameters['geocent_time'] - interferometer.strain_data.start_time
        ifo_time = dt_geocent + dt

        calib_linear = interferometer.calibration_model.get_calibration_factor(
            self.frequency_nodes_linear,
            prefix='recalib_{}_'.format(interferometer.name), **self.parameters)
        calib_quadratic = interferometer.calibration_model.get_calibration_factor(
            self.frequency_nodes_quadratic,
            prefix='recalib_{}_'.format(interferometer.name), **self.parameters)

        h_plus_linear = f_plus * waveform_polarizations['linear']['plus'] * calib_linear
        h_cross_linear = f_cross * waveform_polarizations['linear']['cross'] * calib_linear
        h_plus_quadratic = (
            f_plus * waveform_polarizations['quadratic']['plus'] * calib_quadratic)
        h_cross_quadratic = (
            f_cross * waveform_polarizations['quadratic']['cross'] * calib_quadratic)

        indices, in_bounds = self._closest_time_indices(
            ifo_time, self.weights['time_samples'])
        if not in_bounds:
            logger.debug("SNR calculation error: requested time at edge of ROQ time samples")
            return self._CalculatedSNRs(
                d_inner_h=np.nan_to_num(-np.inf), optimal_snr_squared=0,
                complex_matched_filter_snr=np.nan_to_num(-np.inf),
                d_inner_h_squared_tc_array=None,
                d_inner_h_array=None,
                optimal_snr_squared_array=None)

        d_inner_h_tc_array = np.einsum(
            'i,ji->j', np.conjugate(h_plus_linear + h_cross_linear),
            self.weights[interferometer.name + '_linear'][indices])

        d_inner_h = self._interp_five_samples(
            self.weights['time_samples'][indices], d_inner_h_tc_array, ifo_time)

        optimal_snr_squared = \
            np.vdot(np.abs(h_plus_quadratic + h_cross_quadratic)**2,
                    self.weights[interferometer.name + '_quadratic'])

        with np.errstate(invalid="ignore"):
            complex_matched_filter_snr = d_inner_h / (optimal_snr_squared**0.5)
        d_inner_h_squared_tc_array = None

        return self._CalculatedSNRs(
            d_inner_h=d_inner_h, optimal_snr_squared=optimal_snr_squared,
            complex_matched_filter_snr=complex_matched_filter_snr,
            d_inner_h_squared_tc_array=d_inner_h_squared_tc_array,
            d_inner_h_array=None,
            optimal_snr_squared_array=None)

    @staticmethod
    def _closest_time_indices(time, samples):
        """
        Get the closest five times

        Parameters
        ==========
        time: float
            Time to check
        samples: array-like
            Available times

        Returns
        =======
        indices: list
            Indices nearest to time
        in_bounds: bool
            Whether the indices are for valid times
        """
        closest = int((time - samples[0]) / (samples[1] - samples[0]))
        indices = [closest + ii for ii in [-2, -1, 0, 1, 2]]
        in_bounds = (indices[0] >= 0) & (indices[-1] < samples.size)
        return indices, in_bounds

    @staticmethod
    def _interp_five_samples(time_samples, values, time):
        """
        Interpolate a function of time with its values at the closest five times.
        The algorithm is explained in https://dcc.ligo.org/T2100224.

        Parameters
        ==========
        time_samples: array-like
            Closest 5 times
        values: array-like
            The values of the function at closest 5 times
        time: float
            Time at which the function is calculated

        Returns
        =======
        value: float
            The value of the function at the input time
        """
        r1 = (-values[0] + 8. * values[1] - 14. * values[2] + 8. * values[3] - values[4]) / 4.
        r2 = values[2] - 2. * values[3] + values[4]
        a = (time_samples[3] - time) / (time_samples[1] - time_samples[0])
        b = 1. - a
        c = (a**3. - a) / 6.
        d = (b**3. - b) / 6.
        return a * values[2] + b * values[3] + c * r1 + d * r2

    def perform_roq_params_check(self, ifo=None):
        """ Perform checking that the prior and data are valid for the ROQ

        Parameters
        ==========
        ifo: bilby.gw.detector.Interferometer
            The interferometer
        """
        if self.roq_params_check is False:
            logger.warning("No ROQ params checking performed")
            return
        else:
            if getattr(self, "roq_params_file", None) is not None:
                msg = ("Check ROQ params {} with roq_scale_factor={}"
                       .format(self.roq_params_file, self.roq_scale_factor))
            else:
                msg = ("Check ROQ params with roq_scale_factor={}"
                       .format(self.roq_scale_factor))
            logger.info(msg)

        roq_params = self.roq_params
        roq_minimum_frequency = roq_params['flow'] * self.roq_scale_factor
        roq_maximum_frequency = roq_params['fhigh'] * self.roq_scale_factor
        roq_segment_length = roq_params['seglen'] / self.roq_scale_factor
        roq_minimum_chirp_mass = roq_params['chirpmassmin'] / self.roq_scale_factor
        roq_maximum_chirp_mass = roq_params['chirpmassmax'] / self.roq_scale_factor
        roq_minimum_component_mass = roq_params['compmin'] / self.roq_scale_factor

        if ifo.maximum_frequency > roq_maximum_frequency:
            raise BilbyROQParamsRangeError(
                "Requested maximum frequency {} larger than ROQ basis fhigh {}"
                .format(ifo.maximum_frequency, roq_maximum_frequency))
        if ifo.minimum_frequency < roq_minimum_frequency:
            raise BilbyROQParamsRangeError(
                "Requested minimum frequency {} lower than ROQ basis flow {}"
                .format(ifo.minimum_frequency, roq_minimum_frequency))
        if ifo.strain_data.duration != roq_segment_length:
            raise BilbyROQParamsRangeError(
                "Requested duration differs from ROQ basis seglen")

        priors = self.priors
        if isinstance(priors, CBCPriorDict) is False:
            logger.warning("Unable to check ROQ parameter bounds: priors not understood")
            return

        if priors.minimum_chirp_mass is None:
            logger.warning("Unable to check minimum chirp mass ROQ bounds")
        elif priors.minimum_chirp_mass < roq_minimum_chirp_mass:
            raise BilbyROQParamsRangeError(
                "Prior minimum chirp mass {} less than ROQ basis bound {}"
                .format(priors.minimum_chirp_mass,
                        roq_minimum_chirp_mass))

        if priors.maximum_chirp_mass is None:
            logger.warning("Unable to check maximum_chirp mass ROQ bounds")
        elif priors.maximum_chirp_mass > roq_maximum_chirp_mass:
            raise BilbyROQParamsRangeError(
                "Prior maximum chirp mass {} greater than ROQ basis bound {}"
                .format(priors.maximum_chirp_mass,
                        roq_maximum_chirp_mass))

        if priors.minimum_component_mass is None:
            logger.warning("Unable to check minimum component mass ROQ bounds")
        elif priors.minimum_component_mass < roq_minimum_component_mass:
            raise BilbyROQParamsRangeError(
                "Prior minimum component mass {} less than ROQ basis bound {}"
                .format(priors.minimum_component_mass,
                        roq_minimum_component_mass))

    def _set_weights(self, linear_matrix, quadratic_matrix):
        """
        Setup the time-dependent ROQ weights.
        See https://dcc.ligo.org/LIGO-T2100125 for the detail of how to compute them.

        Parameters
        ==========
        linear_matrix, quadratic_matrix: array_like
            Arrays of the linear and quadratic basis

        """

        time_space = self._get_time_resolution()
        number_of_time_samples = int(self.interferometers.duration / time_space)
        try:
            import pyfftw
            ifft_input = pyfftw.empty_aligned(number_of_time_samples, dtype=complex)
            ifft_output = pyfftw.empty_aligned(number_of_time_samples, dtype=complex)
            ifft = pyfftw.FFTW(ifft_input, ifft_output, direction='FFTW_BACKWARD')
        except ImportError:
            pyfftw = None
            logger.warning("You do not have pyfftw installed, falling back to numpy.fft.")
            ifft_input = np.zeros(number_of_time_samples, dtype=complex)
            ifft = np.fft.ifft
        earth_light_crossing_time = 2 * radius_of_earth / speed_of_light + 5 * time_space
        start_idx = max(0, int(np.floor((self.priors['{}_time'.format(self.time_reference)].minimum -
                        earth_light_crossing_time - self.interferometers.start_time) / time_space)))
        end_idx = min(number_of_time_samples - 1, int(np.ceil((
                      self.priors['{}_time'.format(self.time_reference)].maximum + earth_light_crossing_time -
                      self.interferometers.start_time) / time_space)))
        self.weights['time_samples'] = np.arange(start_idx, end_idx + 1) * time_space
        logger.info("Using {} ROQ time samples".format(len(self.weights['time_samples'])))

        for ifo in self.interferometers:
            if self.roq_params is not None:
                self.perform_roq_params_check(ifo)
                # Get scaled ROQ quantities
                roq_scaled_minimum_frequency = self.roq_params['flow'] * self.roq_scale_factor
                roq_scaled_maximum_frequency = self.roq_params['fhigh'] * self.roq_scale_factor
                roq_scaled_segment_length = self.roq_params['seglen'] / self.roq_scale_factor
                # Generate frequencies for the ROQ
                roq_frequencies = create_frequency_series(
                    sampling_frequency=roq_scaled_maximum_frequency * 2,
                    duration=roq_scaled_segment_length)
                roq_mask = roq_frequencies >= roq_scaled_minimum_frequency
                roq_frequencies = roq_frequencies[roq_mask]
                overlap_frequencies, ifo_idxs, roq_idxs = np.intersect1d(
                    ifo.frequency_array[ifo.frequency_mask], roq_frequencies,
                    return_indices=True)
            else:
                overlap_frequencies = ifo.frequency_array[ifo.frequency_mask]
                roq_idxs = np.arange(linear_matrix.shape[0], dtype=int)
                ifo_idxs = np.arange(sum(ifo.frequency_mask))
                if len(ifo_idxs) != len(roq_idxs):
                    raise ValueError(
                        "Mismatch between ROQ basis and frequency array for "
                        "{}".format(ifo.name))
            logger.info(
                "Building ROQ weights for {} with {} frequencies between {} "
                "and {}.".format(
                    ifo.name, len(overlap_frequencies),
                    min(overlap_frequencies), max(overlap_frequencies)))

            ifft_input[:] *= 0.
            self.weights[ifo.name + '_linear'] = \
                np.zeros((len(self.weights['time_samples']), linear_matrix.shape[1]), dtype=complex)
            data_over_psd = ifo.frequency_domain_strain[ifo.frequency_mask][ifo_idxs] / \
                ifo.power_spectral_density_array[ifo.frequency_mask][ifo_idxs]
            nonzero_idxs = ifo_idxs + int(ifo.frequency_array[ifo.frequency_mask][0] * self.interferometers.duration)
            for i, basis_element in enumerate(linear_matrix[roq_idxs].T):
                ifft_input[nonzero_idxs] = data_over_psd * np.conj(basis_element)
                self.weights[ifo.name + '_linear'][:, i] = ifft(ifft_input)[start_idx:end_idx + 1]
            self.weights[ifo.name + '_linear'] *= 4. * number_of_time_samples / self.interferometers.duration

            self.weights[ifo.name + '_quadratic'] = build_roq_weights(
                1 /
                ifo.power_spectral_density_array[ifo.frequency_mask][ifo_idxs],
                quadratic_matrix[roq_idxs].real,
                1 / ifo.strain_data.duration)

            logger.info("Finished building weights for {}".format(ifo.name))

        if pyfftw is not None:
            pyfftw.forget_wisdom()

    def save_weights(self, filename, format='npz'):
        if format not in filename:
            filename += "." + format
        logger.info("Saving ROQ weights to {}".format(filename))
        if format == 'json':
            with open(filename, 'w') as file:
                json.dump(self.weights, file, indent=2, cls=BilbyJsonEncoder)
        elif format == 'npz':
            np.savez(filename, **self.weights)

    @staticmethod
    def load_weights(filename, format=None):
        if format is None:
            format = filename.split(".")[-1]
        if format not in ["json", "npz"]:
            raise IOError("Format {} not recognized.".format(format))
        logger.info("Loading ROQ weights from {}".format(filename))
        if format == "json":
            with open(filename, 'r') as file:
                weights = json.load(file, object_hook=decode_bilby_json)
        elif format == "npz":
            # Wrap in dict to load data into memory
            weights = dict(np.load(filename))
        return weights

    def _get_time_resolution(self):
        """
        This method estimates the time resolution given the optimal SNR of the
        signal in the detector. This is then used when constructing the weights
        for the ROQ.

        A minimum resolution is set by assuming the SNR in each detector is at
        least 10. When the SNR is not available the SNR is assumed to be 30 in
        each detector.

        Returns
        =======
        delta_t: float
            Time resolution
        """

        def calc_fhigh(freq, psd, scaling=20.):
            """

            Parameters
            ==========
            freq: array-like
                Frequency array
            psd: array-like
                Power spectral density
            scaling: float
                SNR dependent scaling factor

            Returns
            =======
            f_high: float
                The maximum frequency which must be considered
            """
            from scipy.integrate import simps
            integrand1 = np.power(freq, -7. / 3) / psd
            integral1 = simps(integrand1, freq)
            integrand3 = np.power(freq, 2. / 3.) / (psd * integral1)
            f_3_bar = simps(integrand3, freq)

            f_high = scaling * f_3_bar**(1 / 3)

            return f_high

        def c_f_scaling(snr):
            return (np.pi**2 * snr**2 / 6)**(1 / 3)

        inj_snr_sq = 0
        for ifo in self.interferometers:
            inj_snr_sq += max(10, ifo.meta_data.get('optimal_SNR', 30))**2

        psd = ifo.power_spectral_density_array[ifo.frequency_mask]
        freq = ifo.frequency_array[ifo.frequency_mask]
        fhigh = calc_fhigh(freq, psd, scaling=c_f_scaling(inj_snr_sq**0.5))

        delta_t = fhigh**-1

        # Apply a safety factor to ensure the time step is short enough
        delta_t = delta_t / 5

        # duration / delta_t needs to be a power of 2 for IFFT
        number_of_time_samples = max(
            self.interferometers.duration / delta_t,
            self.interferometers.frequency_array[-1] * self.interferometers.duration + 1)
        number_of_time_samples = int(2**np.ceil(np.log2(number_of_time_samples)))
        delta_t = self.interferometers.duration / number_of_time_samples
        logger.info("ROQ time-step = {}".format(delta_t))
        return delta_t

    def _rescale_signal(self, signal, new_distance):
        for kind in ['linear', 'quadratic']:
            for mode in signal[kind]:
                signal[kind][mode] *= self._ref_dist / new_distance


def get_binary_black_hole_likelihood(interferometers):
    """ A wrapper to quickly set up a likelihood for BBH parameter estimation

    Parameters
    ==========
    interferometers: {bilby.gw.detector.InterferometerList, list}
        A list of `bilby.detector.Interferometer` instances, typically the
        output of either `bilby.detector.get_interferometer_with_open_data`
        or `bilby.detector.get_interferometer_with_fake_noise_and_injection`

    Returns
    =======
    bilby.GravitationalWaveTransient: The likelihood to pass to `run_sampler`

    """
    waveform_generator = WaveformGenerator(
        duration=interferometers.duration,
        sampling_frequency=interferometers.sampling_frequency,
        frequency_domain_source_model=lal_binary_black_hole,
        waveform_arguments={'waveform_approximant': 'IMRPhenomPv2',
                            'reference_frequency': 50})
    return GravitationalWaveTransient(interferometers, waveform_generator)


class BilbyROQParamsRangeError(Exception):
    pass


class MBGravitationalWaveTransient(GravitationalWaveTransient):
    """A multi-banded likelihood object

    This uses the method described in S. Morisaki, 2021, arXiv: 2104.07813.

    Parameters
    ----------
    interferometers: list, bilby.gw.detector.InterferometerList
        A list of `bilby.detector.Interferometer` instances - contains the detector data and power spectral densities
    waveform_generator: `bilby.waveform_generator.WaveformGenerator`
        An object which computes the frequency-domain strain of the signal, given some set of parameters
    reference_chirp_mass: float
        A reference chirp mass for determining the frequency banding
    highest_mode: int, optional
        The maximum magnetic number of gravitational-wave moments. Default is 2
    linear_interpolation: bool, optional
        If True, the linear-interpolation method is used for the computation of (h, h). If False, the IFFT-FFT method
        is used. Default is True.
    accuracy_factor: float, optional
        A parameter to determine the accuracy of multi-banding. The larger this factor is, the more accurate the
        approximation is. This corresponds to L in the paper. Default is 5.
    time_offset: float, optional
        (end time of data) - (maximum arrival time). If None, it is inferred from the prior of geocent time.
    delta_f_end: float, optional
        The frequency scale with which waveforms at the high-frequency end are smoothed. If None, it is determined from
        the prior of geocent time.
    maximum_banding_frequency: float, optional
        A maximum frequency for multi-banding. If specified, the low-frequency limit of a band does not exceed it.
    minimum_banding_duration: float, optional
        A minimum duration for multi-banding. If specified, the duration of a band is not smaller than it.
    distance_marginalization: bool, optional
        If true, marginalize over distance in the likelihood. This uses a look up table calculated at run time. The
        distance prior is set to be a delta function at the minimum distance allowed in the prior being marginalised
        over.
    phase_marginalization: bool, optional
        If true, marginalize over phase in the likelihood. This is done analytically using a Bessel function. The phase
        prior is set to be a delta function at phase=0.
    priors: dict, bilby.prior.PriorDict
        A dictionary of priors containing at least the geocent_time prior
    distance_marginalization_lookup_table: (dict, str), optional
        If a dict, dictionary containing the lookup_table, distance_array, (distance) prior_array, and
        reference_distance used to construct the table. If a string the name of a file containing these quantities. The
        lookup table is stored after construction in either the provided string or a default location:
        '.distance_marginalization_lookup_dmin{}_dmax{}_n{}.npz'
    reference_frame: (str, bilby.gw.detector.InterferometerList, list), optional
        Definition of the reference frame for the sky location.
        - "sky": sample in RA/dec, this is the default
        - e.g., "H1L1", ["H1", "L1"], InterferometerList(["H1", "L1"]):
          sample in azimuth and zenith, `azimuth` and `zenith` defined in the frame where the z-axis is aligned the the
          vector connecting H1 and L1.
    time_reference: str, optional
        Name of the reference for the sampled time parameter.
        - "geocent"/"geocenter": sample in the time at the Earth's center, this is the default
        - e.g., "H1": sample in the time of arrival at H1

    Returns
    -------
    Likelihood: `bilby.core.likelihood.Likelihood`
        A likelihood object, able to compute the likelihood of the data given some model parameters

    """
    def __init__(
        self, interferometers, waveform_generator, reference_chirp_mass, highest_mode=2, linear_interpolation=True,
        accuracy_factor=5, time_offset=None, delta_f_end=None, maximum_banding_frequency=None,
        minimum_banding_duration=0., distance_marginalization=False, phase_marginalization=False, priors=None,
        distance_marginalization_lookup_table=None, reference_frame="sky", time_reference="geocenter"
    ):
        super(MBGravitationalWaveTransient, self).__init__(
            interferometers=interferometers, waveform_generator=waveform_generator, priors=priors,
            distance_marginalization=distance_marginalization, phase_marginalization=phase_marginalization,
            time_marginalization=False, distance_marginalization_lookup_table=distance_marginalization_lookup_table,
            jitter_time=False, reference_frame=reference_frame, time_reference=time_reference
        )
        self.reference_chirp_mass = reference_chirp_mass
        self.highest_mode = highest_mode
        self.linear_interpolation = linear_interpolation
        self.accuracy_factor = accuracy_factor
        self.time_offset = time_offset
        self.delta_f_end = delta_f_end
        self.minimum_frequency = np.min([i.minimum_frequency for i in self.interferometers])
        self.maximum_frequency = np.max([i.maximum_frequency for i in self.interferometers])
        self.maximum_banding_frequency = maximum_banding_frequency
        self.minimum_banding_duration = minimum_banding_duration
        self.setup_multibanding()

    @property
    def reference_chirp_mass(self):
        return self._reference_chirp_mass

    @property
    def reference_chirp_mass_in_second(self):
        return gravitational_constant * self._reference_chirp_mass * solar_mass / speed_of_light**3.

    @reference_chirp_mass.setter
    def reference_chirp_mass(self, reference_chirp_mass):
        if isinstance(reference_chirp_mass, int) or isinstance(reference_chirp_mass, float):
            self._reference_chirp_mass = reference_chirp_mass
        else:
            raise TypeError("reference_chirp_mass must be a number")

    @property
    def highest_mode(self):
        return self._highest_mode

    @highest_mode.setter
    def highest_mode(self, highest_mode):
        if isinstance(highest_mode, int) or isinstance(highest_mode, float):
            self._highest_mode = highest_mode
        else:
            raise TypeError("highest_mode must be a number")

    @property
    def linear_interpolation(self):
        return self._linear_interpolation

    @linear_interpolation.setter
    def linear_interpolation(self, linear_interpolation):
        if isinstance(linear_interpolation, bool):
            self._linear_interpolation = linear_interpolation
        else:
            raise TypeError("linear_interpolation must be a bool")

    @property
    def accuracy_factor(self):
        return self._accuracy_factor

    @accuracy_factor.setter
    def accuracy_factor(self, accuracy_factor):
        if isinstance(accuracy_factor, int) or isinstance(accuracy_factor, float):
            self._accuracy_factor = accuracy_factor
        else:
            raise TypeError("accuracy_factor must be a number")

    @property
    def time_offset(self):
        return self._time_offset

    @time_offset.setter
    def time_offset(self, time_offset):
        """
        This sets the time offset assumed when frequency bands are constructed. The default value is (the
        maximum offset of geocent time in the prior range) +  (light-traveling time of the Earth). If the
        prior does not contain 'geocent_time', 2.12 seconds is used. It is calculated assuming that the
        maximum offset of geocent time is 2.1 seconds, which is the value for the standard prior used by
        LIGO-Virgo-KAGRA.
        """
        if time_offset is not None:
            if isinstance(time_offset, int) or isinstance(time_offset, float):
                self._time_offset = time_offset
            else:
                raise TypeError("time_offset must be a number")
        elif self.priors is not None and 'geocent_time' in self.priors:
            self._time_offset = self.interferometers.start_time + self.interferometers.duration \
                - self.priors['geocent_time'].minimum + radius_of_earth / speed_of_light
        else:
            self._time_offset = 2.12
            logger.warning("time offset can not be inferred. Use the standard time offset of {} seconds.".format(
                self._time_offset))

    @property
    def delta_f_end(self):
        return self._delta_f_end

    @delta_f_end.setter
    def delta_f_end(self, delta_f_end):
        """
        This sets the frequency scale of tapering the high-frequency end of waveform, to avoid the issues of
        abrupt termination of waveform described in Sec. 2. F of arXiv: 2104.07813. This needs to be much
        larger than the inverse of the minimum time offset, and the default value is 100 times of that. If
        the prior does not contain 'geocent_time' and the minimum time offset can not be computed, 53Hz is
        used. It is computed assuming that the minimum offset of geocent time is 1.9 seconds, which is the
        value for the standard prior used by LIGO-Virgo-KAGRA.
        """
        if delta_f_end is not None:
            if isinstance(delta_f_end, int) or isinstance(delta_f_end, float):
                self._delta_f_end = delta_f_end
            else:
                raise TypeError("delta_f_end must be a number")
        elif self.priors is not None and 'geocent_time' in self.priors:
            self._delta_f_end = 100. / (self.interferometers.start_time + self.interferometers.duration
                                        - self.priors['geocent_time'].maximum - radius_of_earth / speed_of_light)
        else:
            self._delta_f_end = 53.
            logger.warning("delta_f_end can not be inferred. Use the standard delta_f_end of {} Hz.".format(
                self._delta_f_end))

    @property
    def maximum_banding_frequency(self):
        return self._maximum_banding_frequency

    @maximum_banding_frequency.setter
    def maximum_banding_frequency(self, maximum_banding_frequency):
        """
        This sets the upper limit on a starting frequency of a band. The default value is the frequency at
        which f - 1 / \sqrt(- d\tau / df) starts to decrease, because the bisection search of the starting
        frequency does not work from that frequency. The stationary phase approximation is not valid at such
        a high frequency, which can break down the approximation. It is calculated from the 0PN formula of
        time-to-merger \tau(f). The user-specified frequency is used if it is lower than that frequency.
        """
        fmax_tmp = (15. / 968.)**(3. / 5.) * (self.highest_mode / (2. * np.pi))**(8. / 5.) \
            / self.reference_chirp_mass_in_second
        if maximum_banding_frequency is not None:
            if isinstance(maximum_banding_frequency, int) or isinstance(maximum_banding_frequency, float):
                if maximum_banding_frequency < fmax_tmp:
                    fmax_tmp = maximum_banding_frequency
                else:
                    logger.warning("The input maximum_banding_frequency is too large."
                                   "It is set to be {} Hz.".format(fmax_tmp))
            else:
                raise TypeError("maximum_banding_frequency must be a number")
        self._maximum_banding_frequency = fmax_tmp

    @property
    def minimum_banding_duration(self):
        return self._minimum_banding_duration

    @minimum_banding_duration.setter
    def minimum_banding_duration(self, minimum_banding_duration):
        if isinstance(minimum_banding_duration, int) or isinstance(minimum_banding_duration, float):
            self._minimum_banding_duration = minimum_banding_duration
        else:
            raise TypeError("minimum_banding_duration must be a number")

    def setup_multibanding(self):
        """Set up frequency bands and coefficients needed for likelihood evaluations"""
        self._setup_frequency_bands()
        self._setup_integers()
        self._setup_waveform_frequency_points()
        self._setup_linear_coefficients()
        if self.linear_interpolation:
            self._setup_quadratic_coefficients_linear_interp()
        else:
            self._setup_quadratic_coefficients_ifft_fft()

    def _tau(self, f):
        """Compute time-to-merger from the input frequency. This uses the 0PN formula.

        Parameters
        ----------
        f: float
            input frequency

        Returns
        -------
        tau: float
            time-to-merger

        """
        f_22 = 2. * f / self.highest_mode
        return 5. / 256. * self.reference_chirp_mass_in_second * \
            (np.pi * self.reference_chirp_mass_in_second * f_22)**(-8. / 3.)

    def _dtaudf(self, f):
        """Compute the derivative of time-to-merger with respect to a starting frequency. This uses the 0PN formula.

        Parameters
        ----------
        f: float
            input frequency

        Returns
        -------
        dtaudf: float
            derivative of time-to-merger

        """
        f_22 = 2. * f / self.highest_mode
        return -5. / 96. * self.reference_chirp_mass_in_second * \
            (np.pi * self.reference_chirp_mass_in_second * f_22)**(-8. / 3.) / f

    def _find_starting_frequency(self, duration, fnow):
        """Find the starting frequency of the next band satisfying (10) and
        (51) of arXiv: 2104.07813.

        Parameters
        ----------
        duration: float
            duration of the next band
        fnow: float
            starting frequency of the current band

        Returns
        -------
        fnext: float or None
            starting frequency of the next band. None if a frequency satisfying the conditions does not exist.
        dfnext: float or None
            frequency scale with which waveforms are smoothed. None if a frequency satisfying the conditions does not
            exist.

        """
        def _is_above_fnext(f):
            "This function returns True if f > fnext"
            cond1 = duration - self.time_offset - self._tau(f) - \
                self.accuracy_factor * np.sqrt(-self._dtaudf(f)) > 0.
            cond2 = f - 1. / np.sqrt(-self._dtaudf(f)) - fnow > 0.
            return cond1 and cond2
        # Bisection search for fnext
        fmin, fmax = fnow, self.maximum_banding_frequency
        if not _is_above_fnext(fmax):
            return None, None
        while fmax - fmin > 1e-2 / duration:
            f = (fmin + fmax) / 2.
            if _is_above_fnext(f):
                fmax = f
            else:
                fmin = f
        return f, 1. / np.sqrt(-self._dtaudf(f))

    def _setup_frequency_bands(self):
        """Set up frequency bands. The durations of bands geometrically decrease T, T/2. T/4, ..., where T is the
        original duration. This sets the following instance variables.

        durations: durations of bands (T^(b) in the paper)
        fb_dfb: the list of tuples, which contain starting frequencies (f^(b) in the paper) and frequency scales for
        smoothing waveforms (\Delta f^(b) in the paper) of bands

        """
        self.durations = [self.interferometers.duration]
        self.fb_dfb = [(self.minimum_frequency, 0.)]
        dnext = self.interferometers.duration / 2
        while dnext > max(self.time_offset, self.minimum_banding_duration):
            fnow, _ = self.fb_dfb[-1]
            fnext, dfnext = self._find_starting_frequency(dnext, fnow)
            if fnext is not None and fnext < min(self.maximum_frequency, self.maximum_banding_frequency):
                self.durations.append(dnext)
                self.fb_dfb.append((fnext, dfnext))
                dnext /= 2
            else:
                break
        self.fb_dfb.append((self.maximum_frequency + self.delta_f_end, self.delta_f_end))
        logger.info("The total frequency range is divided into {} bands with frequency intervals of {}.".format(
            len(self.durations), ", ".join(["1/{} Hz".format(d) for d in self.durations])))

    def _setup_integers(self):
        """Set up integers needed for likelihood evaluations. This sets the following instance variables.

        Nbs: the numbers of samples of downsampled data (N^(b) in the paper)
        Mbs: the numbers of samples of shortened data (M^(b) in the paper)
        Ks_Ke: start and end frequency indices of bands (K^(b)_s and K^(b)_e in the paper)

        """
        self.Nbs = []
        self.Mbs = []
        self.Ks_Ke = []
        for b in range(len(self.durations)):
            dnow = self.durations[b]
            fnow, dfnow = self.fb_dfb[b]
            fnext, _ = self.fb_dfb[b + 1]
            Nb = max(round_up_to_power_of_two(2. * (fnext * self.interferometers.duration + 1.)), 2**b)
            self.Nbs.append(Nb)
            self.Mbs.append(Nb // 2**b)
            self.Ks_Ke.append((math.ceil((fnow - dfnow) * dnow), math.floor(fnext * dnow)))

    def _setup_waveform_frequency_points(self):
        """Set up frequency points where waveforms are evaluated. Frequency points are reordered because some waveform
        models raise an error if the input frequencies are not increasing. This adds frequency_points into the
        waveform_arguments of waveform_generator. This sets the following instance variables.

        banded_frequency_points: ndarray of total banded frequency points
        start_end_idxs: list of tuples containing start and end indices of each band
        unique_to_original_frequencies: indices converting unique frequency
        points into the original duplicated banded frequencies

        """
        self.banded_frequency_points = np.array([])
        self.start_end_idxs = []
        start_idx = 0
        for i in range(len(self.fb_dfb) - 1):
            d = self.durations[i]
            Ks, Ke = self.Ks_Ke[i]
            self.banded_frequency_points = np.append(self.banded_frequency_points, np.arange(Ks, Ke + 1) / d)
            end_idx = start_idx + Ke - Ks
            self.start_end_idxs.append((start_idx, end_idx))
            start_idx = end_idx + 1
        unique_frequencies, idxs = np.unique(self.banded_frequency_points, return_inverse=True)
        self.waveform_generator.waveform_arguments['frequencies'] = unique_frequencies
        self.unique_to_original_frequencies = idxs
        logger.info("The number of frequency points where waveforms are evaluated is {}.".format(
            len(unique_frequencies)))
        logger.info("The speed-up gain of multi-banding is {}.".format(
            (self.maximum_frequency - self.minimum_frequency) * self.interferometers.duration /
            len(unique_frequencies)))

    def _window(self, f, b):
        """Compute window function in the b-th band

        Parameters
        ----------
        f: float or ndarray
            frequency at which the window function is computed
        b: int

        Returns
        -------
        window: float
            window function at f
        """
        fnow, dfnow = self.fb_dfb[b]
        fnext, dfnext = self.fb_dfb[b + 1]

        @np.vectorize
        def _vectorized_window(f):
            if fnow - dfnow < f < fnow:
                return (1. + np.cos(np.pi * (f - fnow) / dfnow)) / 2.
            elif fnow <= f <= fnext - dfnext:
                return 1.
            elif fnext - dfnext < f < fnext:
                return (1. - np.cos(np.pi * (f - fnext) / dfnext)) / 2.
            else:
                return 0.

        return _vectorized_window(f)

    def _setup_linear_coefficients(self):
        """Set up coefficients by which waveforms are multiplied to compute (d, h)"""
        self.linear_coeffs = dict((ifo.name, np.array([])) for ifo in self.interferometers)
        N = self.Nbs[-1]
        for ifo in self.interferometers:
            logger.info("Pre-computing linear coefficients for {}".format(ifo.name))
            fddata = np.zeros(N // 2 + 1, dtype=complex)
            fddata[:len(ifo.frequency_domain_strain)][ifo.frequency_mask] += \
                ifo.frequency_domain_strain[ifo.frequency_mask] / ifo.power_spectral_density_array[ifo.frequency_mask]
            for b in range(len(self.fb_dfb) - 1):
                start_idx, end_idx = self.start_end_idxs[b]
                windows = self._window(self.banded_frequency_points[start_idx:end_idx + 1], b)
                fddata_in_ith_band = np.copy(fddata[:int(self.Nbs[b] / 2 + 1)])
                fddata_in_ith_band[-1] = 0.  # zeroing data at the Nyquist frequency
                tddata = np.fft.irfft(fddata_in_ith_band)[-self.Mbs[b]:]
                Ks, Ke = self.Ks_Ke[b]
                fddata_in_ith_band = np.fft.rfft(tddata)[Ks:Ke + 1]
                self.linear_coeffs[ifo.name] = np.append(
                    self.linear_coeffs[ifo.name], (4. / self.durations[b]) * windows * np.conj(fddata_in_ith_band))

    def _setup_quadratic_coefficients_linear_interp(self):
        """Set up coefficients by which the squares of waveforms are multiplied to compute (h, h) for the
        linear-interpolation algorithm"""
        logger.info("Linear-interpolation algorithm is used for (h, h).")
        self.quadratic_coeffs = dict((ifo.name, np.array([])) for ifo in self.interferometers)
        N = self.Nbs[-1]
        for ifo in self.interferometers:
            logger.info("Pre-computing quadratic coefficients for {}".format(ifo.name))
            full_frequencies = np.arange(N // 2 + 1) / ifo.duration
            full_inv_psds = np.zeros(N // 2 + 1)
            full_inv_psds[:len(ifo.power_spectral_density_array)][ifo.frequency_mask] = \
                1. / ifo.power_spectral_density_array[ifo.frequency_mask]
            for i in range(len(self.fb_dfb) - 1):
                start_idx, end_idx = self.start_end_idxs[i]
                banded_frequencies = self.banded_frequency_points[start_idx:end_idx + 1]
                coeffs = np.zeros(len(banded_frequencies))
                for k in range(len(coeffs) - 1):
                    if k == 0:
                        start_idx_in_sum = 0
                    else:
                        start_idx_in_sum = math.ceil(ifo.duration * banded_frequencies[k])
                    if k == len(coeffs) - 2:
                        end_idx_in_sum = len(full_frequencies) - 1
                    else:
                        end_idx_in_sum = math.ceil(ifo.duration * banded_frequencies[k + 1]) - 1
                    window_over_psd = full_inv_psds[start_idx_in_sum:end_idx_in_sum + 1] \
                        * self._window(full_frequencies[start_idx_in_sum:end_idx_in_sum + 1], i)
                    frequencies_in_sum = full_frequencies[start_idx_in_sum:end_idx_in_sum + 1]
                    coeffs[k] += 4. * self.durations[i] / ifo.duration * np.sum(
                        (banded_frequencies[k + 1] - frequencies_in_sum) * window_over_psd)
                    coeffs[k + 1] += 4. * self.durations[i] / ifo.duration \
                        * np.sum((frequencies_in_sum - banded_frequencies[k]) * window_over_psd)
                self.quadratic_coeffs[ifo.name] = np.append(self.quadratic_coeffs[ifo.name], coeffs)

    def _setup_quadratic_coefficients_ifft_fft(self):
        """Set up coefficients needed for the IFFT-FFT algorithm to compute (h, h)"""
        logger.info("IFFT-FFT algorithm is used for (h, h).")
        N = self.Nbs[-1]
        # variables defined below correspond to \hat{N}^(b), \hat{T}^(b), \tilde{I}^(b)_{c, k}, h^(b)_{c, m} and
        # \sqrt{w^(b)(f^(b)_k)} \tilde{h}(f^(b)_k) in the paper
        Nhatbs = [min(2 * Mb, Nb) for Mb, Nb in zip(self.Mbs, self.Nbs)]
        self.Tbhats = [self.interferometers.duration * Nbhat / Nb for Nb, Nbhat in zip(self.Nbs, Nhatbs)]
        self.Ibcs = dict((ifo.name, []) for ifo in self.interferometers)
        self.hbcs = dict((ifo.name, []) for ifo in self.interferometers)
        self.wths = dict((ifo.name, []) for ifo in self.interferometers)
        for ifo in self.interferometers:
            logger.info("Pre-computing quadratic coefficients for {}".format(ifo.name))
            full_inv_psds = np.zeros(N // 2 + 1)
            full_inv_psds[:len(ifo.power_spectral_density_array)][ifo.frequency_mask] = 1. / \
                ifo.power_spectral_density_array[ifo.frequency_mask]
            for b in range(len(self.fb_dfb) - 1):
                Imb = np.fft.irfft(full_inv_psds[:self.Nbs[b] // 2 + 1])
                half_length = Nhatbs[b] // 2
                Imbc = np.append(Imb[:half_length + 1], Imb[-(Nhatbs[b] - half_length - 1):])
                self.Ibcs[ifo.name].append(np.fft.rfft(Imbc))
                # Allocate arrays for IFFT-FFT operations
                self.hbcs[ifo.name].append(np.zeros(Nhatbs[b]))
                self.wths[ifo.name].append(np.zeros(self.Mbs[b] // 2 + 1, dtype=complex))
        # precompute windows and their squares
        self.windows = np.array([])
        self.square_root_windows = np.array([])
        for b in range(len(self.fb_dfb) - 1):
            start, end = self.start_end_idxs[b]
            ws = self._window(self.banded_frequency_points[start:end + 1], b)
            self.windows = np.append(self.windows, ws)
            self.square_root_windows = np.append(self.square_root_windows, np.sqrt(ws))

    def calculate_snrs(self, waveform_polarizations, interferometer):
        """
        Compute the snrs for multi-banding

        Parameters
        ----------
        waveform_polarizations: waveform
        interferometer: bilby.gw.detector.Interferometer

        Returns
        -------
        snrs: named tuple of snrs

        """
        f_plus = interferometer.antenna_response(
            self.parameters['ra'], self.parameters['dec'],
            self.parameters['geocent_time'], self.parameters['psi'], 'plus')
        f_cross = interferometer.antenna_response(
            self.parameters['ra'], self.parameters['dec'],
            self.parameters['geocent_time'], self.parameters['psi'], 'cross')

        dt = interferometer.time_delay_from_geocenter(
            self.parameters['ra'], self.parameters['dec'],
            self.parameters['geocent_time'])
        dt_geocent = self.parameters['geocent_time'] - interferometer.strain_data.start_time
        ifo_time = dt_geocent + dt

        calib_factor = interferometer.calibration_model.get_calibration_factor(
            self.banded_frequency_points, prefix='recalib_{}_'.format(interferometer.name), **self.parameters)

        h = f_plus * waveform_polarizations['plus'][self.unique_to_original_frequencies] \
            + f_cross * waveform_polarizations['cross'][self.unique_to_original_frequencies]
        h *= np.exp(-1j * 2. * np.pi * self.banded_frequency_points * ifo_time)
        h *= np.conjugate(calib_factor)

        d_inner_h = np.dot(h, self.linear_coeffs[interferometer.name])

        if self.linear_interpolation:
            optimal_snr_squared = np.vdot(np.real(h * np.conjugate(h)), self.quadratic_coeffs[interferometer.name])
        else:
            optimal_snr_squared = 0.
            for b in range(len(self.fb_dfb) - 1):
                Ks, Ke = self.Ks_Ke[b]
                start_idx, end_idx = self.start_end_idxs[b]
                Mb = self.Mbs[b]
                if b == 0:
                    optimal_snr_squared += (4. / self.interferometers.duration) * np.vdot(
                        np.real(h[start_idx:end_idx + 1] * np.conjugate(h[start_idx:end_idx + 1])),
                        interferometer.frequency_mask[Ks:Ke + 1] * self.windows[start_idx:end_idx + 1]
                        / interferometer.power_spectral_density_array[Ks:Ke + 1])
                else:
                    self.wths[interferometer.name][b][Ks:Ke + 1] = self.square_root_windows[start_idx:end_idx + 1] \
                        * h[start_idx:end_idx + 1]
                    self.hbcs[interferometer.name][b][-Mb:] = np.fft.irfft(self.wths[interferometer.name][b])
                    thbc = np.fft.rfft(self.hbcs[interferometer.name][b])
                    optimal_snr_squared += (4. / self.Tbhats[b]) * np.vdot(
                        np.real(thbc * np.conjugate(thbc)), self.Ibcs[interferometer.name][b])

        complex_matched_filter_snr = d_inner_h / (optimal_snr_squared**0.5)

        return self._CalculatedSNRs(
            d_inner_h=d_inner_h, optimal_snr_squared=optimal_snr_squared,
            complex_matched_filter_snr=complex_matched_filter_snr,
            d_inner_h_squared_tc_array=None,
            d_inner_h_array=None,
            optimal_snr_squared_array=None)

    def _rescale_signal(self, signal, new_distance):
        for mode in signal:
            signal[mode] *= self._ref_dist / new_distance
