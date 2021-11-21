import os
import numpy as np
import argparse
import json
import pandas as pd

from ast import literal_eval

from astropy import time

import bilby
import bilby.core
from bilby.core.likelihood import ZeroLikelihood

from .model import SVDLightCurveModel, GRBLightCurveModel, SupernovaLightCurveModel
from .model import SupernovaGRBLightCurveModel, KilonovaGRBLightCurveModel
from .model import ShockCoolingLightCurveModel, SupernovaShockCoolingLightCurveModel
from .model import GenericCombineLightCurveModel
from .utils import loadEvent, getFilteredMag
from .injection import create_light_curve_data
from .likelihood import OpticalLightCurve


def main():

    parser = argparse.ArgumentParser(description="Inference on kilonova ejecta parameters.")
    parser.add_argument("--model", type=str, required=True, help="Name of the kilonova model to be used")
    parser.add_argument("--svd-path", type=str, help="Path to the SVD directory, with {model}_mag.pkl and {model}_lbol.pkl")
    parser.add_argument("--outdir", type=str, required=True, help="Path to the output directory")
    parser.add_argument("--label", type=str, required=True, help="Label for the run")
    parser.add_argument("--trigger-time", type=float, help="Trigger time in modified julian day, not required if injection set is provided")
    parser.add_argument("--data", type=str, help="Path to the data file in [time(isot) filter magnitude error] format")
    parser.add_argument("--prior", type=str, required=True, help="Path to the prior file")
    parser.add_argument("--tmin", type=float, default=0., help="Days to be started analysing from the trigger time (default: 0)")
    parser.add_argument("--tmax", type=float, default=14., help="Days to be stoped analysing from the trigger time (default: 14)")
    parser.add_argument("--dt", type=float, default=0.1, help="Time step in day (default: 0.1)")
    parser.add_argument("--photometric-error-budget", type=float, default=0.1, help="Photometric error (mag) (default: 0.1)")
    parser.add_argument("--svd-mag-ncoeff", type=int, default=10, help="Number of eigenvalues to be taken for mag evaluation (default: 10)")
    parser.add_argument("--svd-lbol-ncoeff", type=int, default=10, help="Number of eigenvalues to be taken for lbol evaluation (default: 10)")
    parser.add_argument("--filters", type=str, help="A comma seperated list of filters to use (e.g. g,r,i). If none is provided, will use all the filters available")
    parser.add_argument("--Ebv-max", type=float, default=0.5724, help="Maximum allowed value for Ebv (default:0.5724)")
    parser.add_argument("--grb-resolution", type=float, default=5, help="The upper bound on the ratio between thetaWing and thetaCore (default: 5)")
    parser.add_argument("--jet-type", type=int, default=0, help="Jet type to used used for GRB afterglow light curve (default: 0)")
    parser.add_argument("--error-budget", type=float, default=1., help="Additionaly statistical error (mag) to be introduced (default: 1)")
    parser.add_argument("--sampler", type=str, default='pymultinest', help="Sampler to be used (default: pymultinest)")
    parser.add_argument("--cpus", type=int, default=1, help="Number of cores to be used, only needed for dynesty (default: 1)")
    parser.add_argument("--nlive", type=int, default=2048, help="Number of live points (default: 2048)")
    parser.add_argument("--seed", metavar="seed", type=int, default=42, help="Sampling seed (default: 42)")
    parser.add_argument("--injection", metavar="PATH", type=str, help="Path to the injection json file")
    parser.add_argument("--injection-num", metavar="eventnum", type=int, help="The injection number to be taken from the injection set")
    parser.add_argument("--injection-detection-limit", metavar="mAB", type=str, help="The highest mAB to be presented in the injection data set, any mAB higher than this will become a non-detection limit. Should be comma delimited list same size as injection set.")
    parser.add_argument("--injection-outfile", metavar="PATH", type=str, help="Path to the output injection lightcurve")
    parser.add_argument("--remove-nondetections", action="store_true", default=False, help="remove non-detections from fitting analysis")
    parser.add_argument("--detection-limit", metavar="DICT", type=str, default=None, help="Dictionary for detection limit per filter, e.g., {'r':22, 'g':23}, put a double quotation marks around the dictionary")
    parser.add_argument("--with-grb-injection", help="If the injection has grb included", action="store_true")
    parser.add_argument("--prompt-collapse", help="If the injection simulates prompt collapse and therefore only dynamical", action="store_true")
    parser.add_argument("--generation-seed", metavar="seed", type=int, default=42, help="Injection generation seed (default: 42)")
    parser.add_argument("--plot", action="store_true", default=False, help="add best fit plot")
    parser.add_argument("--bilby_zero_likelihood_mode", action="store_true", default=False, help="enable prior run")
    parser.add_argument("--verbose", action="store_true", default=False, help="print out log likelihoods")
    args = parser.parse_args()

    bilby.core.utils.setup_logger(outdir=args.outdir, label=args.label)
    bilby.core.utils.check_directory_exists_and_if_not_mkdir(args.outdir)

    # initialize light curve model
    sample_times = np.arange(args.tmin, args.tmax + args.dt, args.dt)

    models = []
    # check if there are more than one model
    if ',' in args.model:
        print("Running with combination of multiple light curve models")
        model_names = args.model.split(',')
    else:
        model_names = [args.model]

    for model_name in model_names:
        if model_name == 'TrPi2018':
            lc_model = GRBLightCurveModel(sample_times=sample_times,
                                          resolution=args.grb_resolution,
                                          jetType=args.jet_type)

        elif model_name == 'nugent-hyper':
            lc_model = SupernovaLightCurveModel(sample_times=sample_times)

        elif model_name == 'Piro2021':
            lc_model = ShockCoolingLightCurveModel(sample_times=sample_times)

        elif model_name == 'Me2017':
            lc_model = SimpleKilonovaLightCurveModel(sample_times=sample_times)

        else:
            lc_kwargs = dict(model=model_name, sample_times=sample_times,
                             svd_path=args.svd_path, mag_ncoeff=args.svd_mag_ncoeff,
                             lbol_ncoeff=args.svd_lbol_ncoeff)
            lc_model = SVDLightCurveModel(**lc_kwargs)

        models.append(lc_model)

        if len(models) > 1:
            light_curve_model = GenericCombineLightCurveModel(models, sample_times)
        else:
            light_curve_model = models[0]

    # create the kilonova data if an injection set is given
    if args.injection:
        with open(args.injection, 'r') as f:
            injection_dict = json.load(
                f, object_hook=bilby.core.utils.decode_bilby_json
            )
        injection_df = injection_dict["injections"]
        injection_parameters = injection_df.iloc[args.injection_num].to_dict()

        tc_gps = time.Time(
            injection_parameters['geocent_time_x'],
            format='gps'
        )
        trigger_time = tc_gps.mjd

        injection_parameters['kilonova_trigger_time'] = trigger_time
        if args.prompt_collapse:
            injection_parameters['log10_mej_wind'] = -3.0

        args.kilonova_tmin = args.tmin
        args.kilonova_tmax = args.tmax
        args.kilonova_tstep = args.dt
        args.kilonova_error = args.photometric_error_budget

        args.kilonova_injection_model = args.model
        args.kilonova_injection_svd = args.svd_path
        args.injection_svd_mag_ncoeff = args.svd_mag_ncoeff
        args.injection_svd_lbol_ncoeff = args.svd_lbol_ncoeff

        data = create_light_curve_data(
            injection_parameters, args
        )
        print("Injection generated")

        if args.injection_outfile is not None:
            detection_limit = {x: float(y) for x, y in zip(args.filters.split(","),args.injection_detection_limit.split(","))}
            data_out = np.empty((0, 6))
            for filt in data.keys():
                if args.filters:
                    if filt not in args.filters.split(','):
                        continue
                for row in data[filt]:
                    mjd, mag, mag_unc = row
                    if not np.isfinite(mag_unc):
                        data_out = np.append(data_out, np.array([[mjd, 99.0, 99.0, filt, mag, 0.0]]), axis=0)
                    else:
                        data_out = np.append(data_out, np.array([[mjd, mag, mag_unc, filt, detection_limit[filt], 0.0]]), axis=0)

            columns = ["jd", "mag", "mag_unc", "filter", "limmag", "programid"]
            lc = pd.DataFrame(data=data_out, columns=columns)
            lc.sort_values("jd", inplace=True)
            lc = lc.reset_index(drop=True)
            lc.to_csv(args.injection_outfile)

        if args.remove_nondetections:
            for filt in data.keys():
                idx = np.where(np.isfinite(data[filt][:,2]))[0]
                data[filt] = data[filt][idx,:]

    else:
        # load the kilonova afterglow data
        data = loadEvent(args.data)

        trigger_time = args.trigger_time

    if not args.filters:
        filters = data.keys()
    else:
        filters = args.filters.split(',')

    print("Running with filters {0}".format(filters))
    # setup the prior
    priors = bilby.gw.prior.PriorDict(args.prior)

    # setup for Ebv
    if args.Ebv_max > 0.:
        Ebv_c = 1. / (0.5 * args.Ebv_max)
        priors['Ebv'] = bilby.core.prior.Interped(name='Ebv',
                                                  minimum=0.,
                                                  maximum=args.Ebv_max,
                                                  latex_label='$E(B-V)$',
                                                  xx=[0, args.Ebv_max],
                                                  yy=[Ebv_c, 0])
    else:
        priors['Ebv'] = bilby.core.prior.DeltaFunction(name='Ebv', peak=0., latex_label='$E(B-V)$')

    # setup the likelihood
    if args.detection_limit:
        args.detection_limit = literal_eval(args.detection_limit)
    likelihood_kwargs = dict(light_curve_model=light_curve_model, filters=filters, light_curve_data=data,
                             trigger_time=trigger_time, tmin=args.tmin, tmax=args.tmax,
                             error_budget=args.error_budget, verbose=args.verbose,
                             detection_limit=args.detection_limit)

    likelihood = OpticalLightCurve(**likelihood_kwargs)
    if args.bilby_zero_likelihood_mode:
        likelihood = ZeroLikelihood(likelihood)

    result = bilby.run_sampler(
        likelihood, priors, sampler=args.sampler, outdir=args.outdir, label=args.label,
        nlive=args.nlive, seed=args.seed, soft_init=True, queue_size=args.cpus,
        check_point_delta_t=3600)
    if args.injection:
        if args.model in ["Bu2019nsbh"]:
            injlist = ['luminosity_distance', 'inclination_EM',
                       'log10_mej_dyn', 'log10_mej_wind']
        else:
            injlist = ['luminosity_distance', 'inclination_EM', 'KNphi',
                       'log10_mej_dyn', 'log10_mej_wind']
        injection = {key: injection_parameters[key] for key in injlist}
        result.plot_corner(parameters=injection)
    else:
        result.plot_corner()
    result.save_posterior_samples()

    if args.plot:
        import matplotlib.pyplot as plt
        from matplotlib.pyplot import cm

        posterior_file = os.path.join(args.outdir, 'injection_' + args.model + '_posterior_samples.dat')

        ##########################
        # Fetch bestfit parameters
        ##########################
        posterior_samples = pd.read_csv(posterior_file, header=0, delimiter=' ')
        bestfit_idx = np.argmax(posterior_samples.log_likelihood.to_numpy())
        bestfit_params = posterior_samples.to_dict(orient='list')
        for key in bestfit_params.keys():
            bestfit_params[key] = bestfit_params[key][bestfit_idx]

        #########################
        # Generate the lightcurve
        #########################
        _, mag = light_curve_model.generate_lightcurve(sample_times, bestfit_params)
        for filt in mag.keys():
            mag[filt] += 5. * np.log10(bestfit_params['luminosity_distance'] * 1e6 / 10.)
        mag['bestfit_sample_times'] = sample_times

        colors = cm.Spectral(np.linspace(0, 1, len(filters)))[::-1]

        plotName = os.path.join(args.outdir, 'injection_' + args.model + '_lightcurves.png')
        plt.figure(figsize=(20, 16))

        color2 = 'coral'

        cnt = 0
        for filt, color in zip(filters, colors):
            cnt = cnt + 1
            if cnt == 1:
                ax1 = plt.subplot(len(filters), 1, cnt)
            else:
                ax2 = plt.subplot(len(filters), 1, cnt, sharex=ax1, sharey=ax1)

            if filt not in data:
                continue
            samples = data[filt]
            t, y, sigma_y = samples[:, 0], samples[:, 1], samples[:, 2]
            t -= trigger_time
            idx = np.where(~np.isnan(y))[0]
            t, y, sigma_y = t[idx], y[idx], sigma_y[idx]
            if len(t) == 0:
                continue

            idx = np.where(np.isfinite(sigma_y))[0]
            plt.errorbar(t[idx],
                         y[idx],
                         sigma_y[idx],
                         fmt='o',
                         color='k',
                         markersize=16,
                         label='%s-band' % filt)  # or color=color

            idx = np.where(~np.isfinite(sigma_y))[0]
            plt.errorbar(t[idx],
                         y[idx],
                         sigma_y[idx],
                         fmt='v',
                         color='k',
                         markersize=16)  # or color=color

            mag_plot = getFilteredMag(mag, filt)

            plt.plot(sample_times, mag_plot, color=color2, linewidth=3, linestyle='--')
            plt.fill_between(sample_times,
                             mag_plot + args.error_budget,
                             mag_plot - args.error_budget,
                             facecolor=color2,
                             alpha=0.2)

            plt.ylabel('%s' % filt, fontsize=48, rotation=0, labelpad=40)

            plt.xlim([0.0, 10.0])
            plt.ylim([26.0, 14.0])
            plt.grid()

            if cnt == 1:
                ax1.set_yticks([26, 22, 18, 14])
                plt.setp(ax1.get_xticklabels(), visible=False)
                # l = plt.legend(loc="upper right",prop={'size':36},numpoints=1,shadow=True, fancybox=True)
            elif not cnt == len(filters):
                plt.setp(ax2.get_xticklabels(), visible=False)
            plt.xticks(fontsize=36)
            plt.yticks(fontsize=36)

        ax1.set_zorder(1)
        plt.xlabel('Time [days]', fontsize=48)
        plt.tight_layout()
        plt.savefig(plotName)
        plt.close()