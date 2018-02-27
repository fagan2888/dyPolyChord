#!/usr/bin/envython
"""
Functions for using PyPolyChord, including using it to perform dynamic nested
sampling.
"""
import os
import functools
import time
import shutil
import copy
import numpy as np
from mpi4py import MPI
import PyPolyChord
import PyPolyChord.likelihoods
import nestcheck.analyse_run as ar
import nestcheck.data_processing
import dypypolychord.priors as priors
import dypypolychord.save_load_utils


def run_standard_polychord(pc_settings, **kwargs):
    """
    Wrapper function of same format as run_dynamic_polychord for running
    standard polychord runs.
    """
    comm = MPI.COMM_WORLD
    ndims = kwargs.pop('ndims', 2)
    nderived = kwargs.pop('nderived', 0)
    likelihood = kwargs.pop('likelihood', PyPolyChord.likelihoods.gaussian)
    prior = kwargs.pop('prior', functools.partial(priors.uniform_prior,
                                                  ndims=ndims, prior_scale=5))
    assert not pc_settings.nlives
    start_time = time.time()
    # do initial run
    # --------------
    output = PyPolyChord.run_polychord(likelihood, ndims, nderived,
                                       pc_settings, prior)
    if comm.rank == 0:
        dypypolychord.save_load_utils.save_info(pc_settings, output)
        end_time = time.time()
        print('####################################')
        print('run_standard_polychord took %.3f sec' % (end_time - start_time))
        print('####################################')


def run_dynamic_polychord(pc_settings_in, **kwargs):
    """
    Dynamic nested sampling using polychord.
    """
    comm = MPI.COMM_WORLD
    ninit = kwargs.pop('ninit', 10)
    init_step = kwargs.pop('init_step', ninit)
    dyn_nlive_step = kwargs.pop('dyn_nlive_step', 1)
    nlive_const = kwargs.pop('nlive_const', pc_settings_in.nlive)
    # n_samples_max = kwargs.pop('n_samples_max', None)
    ndims = kwargs.pop('ndims', 2)
    nderived = kwargs.pop('nderived', 0)
    likelihood = kwargs.pop('likelihood', PyPolyChord.likelihoods.gaussian)
    prior = kwargs.pop('prior', functools.partial(priors.uniform_prior,
                                                  ndims=ndims, prior_scale=5))
    if kwargs:
        raise TypeError('Unexpected **kwargs: %r' % kwargs)
    # if comm.rank == 0:
    start_time = time.time()
    assert not pc_settings_in.nlives
    assert not pc_settings_in.read_resume
    # do initial run
    # --------------
    pc_settings = copy.deepcopy(pc_settings_in)  # so we dont edit settings
    pc_settings.file_root = pc_settings_in.file_root + '_init'
    pc_settings.nlive = ninit
    pc_settings.write_resume = True
    pc_settings.read_resume = False
    add_points = True
    step_ndead = []
    runs_at_resumes = {}
    while add_points:
        if len(step_ndead) == 1:
            pc_settings.read_resume = True
        pc_settings.max_ndead = (len(step_ndead) + 1) * init_step
        output = PyPolyChord.run_polychord(likelihood, ndims, nderived,
                                           pc_settings, prior)
        step_ndead.append(output.ndead - pc_settings.nlive)
        # TESTING
        run = nestcheck.data_processing.process_polychord_run(
            pc_settings.base_dir + '/' + pc_settings.file_root)
        assert run['thread_labels'].shape[0] == output.ndead
        assert np.unique(run['thread_labels']).shape[0] == ninit
        runs_at_resumes[output.ndead] = run
        # END TESTING
        shutil.copyfile(pc_settings.base_dir + '/' +
                        pc_settings.file_root + '.resume',
                        pc_settings.base_dir + '/' +
                        pc_settings.file_root +
                        '_' + str(step_ndead[-1]) + '.resume')
        if len(step_ndead) >= 2:
            if step_ndead[-1] == step_ndead[-2] + 1:
                add_points = False
    init_run = nestcheck.data_processing.process_polychord_run(
        pc_settings.base_dir + '/' + pc_settings.file_root)
    # Work out a new allocation of live points
    # ----------------------------------------
    pc_settings = copy.deepcopy(pc_settings_in)  # remove edits from init
    logx_init = ar.get_logx(init_run['nlive_array'])
    w_rel = ar.rel_posterior_mass(logx_init, init_run['logl'])
    # calculate a distribution of nlive points in proportion to w_rel
    if pc_settings_in.max_ndead > 0:
        nlives_array = w_rel * (pc_settings_in.max_ndead -
                                init_run['logl'].shape[0])
    else:
        nlives_array = (w_rel * init_run['logl'].shape[0] *
                        (nlive_const - ninit) / ninit)
    # make sure it does not dip below ninit until it reaches the peak
    peak_start_ind = np.where(nlives_array > ninit)[0][0]
    nlives_array[:peak_start_ind] = ninit
    # get nlives dict
    nlives_dict = {-1. * 10e100: ninit}
    steps = dyn_nlive_step * np.asarray(range(w_rel.shape[0] //
                                              dyn_nlive_step))
    for i, step in enumerate(steps[:-1]):
        # Assign nlives to the logl one step before to make sure the number of
        # live points is increased in time
        nlive_i = int(nlives_array[steps[i + 1]])
        if nlive_i >= 1:
            nlives_dict[init_run['logl'][step]] = nlive_i
    # subtract 1 as ndead=1 corresponds to point 0
    resume_steps = np.asarray(step_ndead) - 1
    # Load the last resume before we reach the peak
    resume_ndead = step_ndead[np.where(
        resume_steps < peak_start_ind)[0][-1]]
    pc_settings.nlive = dyn_nlive_step
    pc_settings.nlives = nlives_dict
    pc_settings.read_resume = True
    # copy resume step to dynamic file root
    shutil.copyfile(pc_settings.base_dir + '/' + pc_settings.file_root +
                    '_init_' + str(resume_ndead) + '.resume',
                    pc_settings.base_dir + '/' +
                    pc_settings.file_root + '_dyn.resume')
    # Remove the mess of other resume files
    # update settings for the dynamic step
    pc_settings.file_root = pc_settings_in.file_root + '_dyn'
    # # broadcast dynamic settings to other threads
    # comm.Barrier()
    # pc_settings = comm.bcast(pc_settings, root=0)
    dyn_output = PyPolyChord.run_polychord(likelihood, ndims, nderived,
                                           pc_settings, prior)
    if comm.rank == 0:
        for snd in step_ndead:
            os.remove(pc_settings_in.base_dir + '/' +
                      pc_settings_in.file_root +
                      '_init_' + str(snd) + '.resume')
        dypypolychord.save_load_utils.save_info(
            pc_settings, dyn_output, resume_ndead=resume_ndead)
        end_time = time.time()
        print('####################################')
        print('run_dynamic_polychord took %.3f sec' % (end_time - start_time))
        print('####################################')