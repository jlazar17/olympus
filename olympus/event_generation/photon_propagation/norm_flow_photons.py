import pickle

import awkward as ak
import jax
import jax.numpy as jnp
from jax.lax import cond
import numpy as np
from hyperion.models.photon_arrival_time_nflow.net import (
    make_counts_net_fn,
    make_shape_conditioner_fn,
    sample_shape_model,
    traf_dist_builder,
    eval_log_prob,
)
from jax import random

from .utils import sources_to_model_input, sources_to_model_input_per_module


def make_generate_norm_flow_photons(shape_model_path, counts_model_path, c_medium):
    shape_config, shape_params = pickle.load(open(shape_model_path, "rb"))
    counts_config, counts_params = pickle.load(open(counts_model_path, "rb"))

    shape_conditioner = make_shape_conditioner_fn(
        shape_config["mlp_hidden_size"],
        shape_config["mlp_num_layers"],
        shape_config["flow_num_bins"],
        shape_config["flow_num_layers"],
    )

    @jax.jit
    def apply_fn(params, x):
        return shape_conditioner.apply(params, x)

    dist_builder = traf_dist_builder(
        shape_config["flow_num_layers"],
        (shape_config["flow_rmin"], shape_config["flow_rmax"]),
        return_base=True,
    )

    counts_net = make_counts_net_fn(counts_config)

    def sample_model(traf_params, key):
        return sample_shape_model(dist_builder, traf_params, traf_params.shape[0], key)

    def generate_norm_flow_photons(
        module_coords,
        module_efficiencies,
        source_pos,
        source_dir,
        source_time,
        source_nphotons,
        seed=31337,
    ):

        # TODO: Reimplement using padding / bucket compile (jax.mask???)

        if isinstance(seed, int):
            key = random.PRNGKey(seed)
        else:
            key = seed

        inp_pars, time_geo = sources_to_model_input(
            module_coords,
            source_pos,
            source_dir,
            source_time,
            c_medium,
        )

        inp_pars = jnp.swapaxes(inp_pars, 0, 1)
        time_geo = jnp.swapaxes(time_geo, 0, 1)

        # flatten [densely pack [modules, sources] in 1D array]
        inp_pars = inp_pars.reshape(
            (source_pos.shape[0] * module_coords.shape[0], inp_pars.shape[-1])
        )
        time_geo = time_geo.reshape(
            (source_pos.shape[0] * module_coords.shape[0], time_geo.shape[-1])
        )
        source_photons = jnp.tile(source_nphotons, module_coords.shape[0]).T.ravel()
        mod_eff_factor = jnp.repeat(module_efficiencies, source_pos.shape[0])

        # Normalizing flows only built up to 300
        # TODO: Check lower bound as well
        distance_mask = inp_pars[:, 0] < np.log10(300)

        inp_params_masked = inp_pars[distance_mask]
        time_geo_masked = time_geo[distance_mask]
        source_photons_masked = source_photons[distance_mask]
        mod_eff_factor_masked = mod_eff_factor[distance_mask]

        # Eval count net to obtain survival fraction
        ph_frac = jnp.power(
            10, counts_net.apply(counts_params, inp_params_masked)
        ).squeeze()

        # Sample number of detected photons
        n_photons_masked = ph_frac * source_photons_masked * mod_eff_factor_masked

        key, subkey = random.split(key)
        n_photons_masked = random.poisson(
            subkey, n_photons_masked, shape=n_photons_masked.shape
        ).squeeze()

        if jnp.all(n_photons_masked == 0):
            times = [] * module_coords.shape[0]
            return ak.Array(times)

        # Obtain flow parameters and repeat them for each detected photon
        traf_params = apply_fn(shape_params, inp_params_masked)
        traf_params_rep = jnp.repeat(traf_params, n_photons_masked, axis=0)
        # Also repeat the geometric time for each detected photon
        time_geo_rep = jnp.repeat(time_geo_masked, n_photons_masked, axis=0).squeeze()

        # Calculate number of photons per module
        # Start with zero array and fill in the poisson samples using distance mask
        n_photons = jnp.zeros(
            source_pos.shape[0] * module_coords.shape[0], dtype=jnp.int32
        )
        n_photons = n_photons.at[distance_mask].set(n_photons_masked)
        n_photons = n_photons.reshape(module_coords.shape[0], source_pos.shape[0])
        n_ph_per_mod = np.sum(n_photons, axis=1)

        # Sample times from flow
        key, subkey = random.split(key)
        samples = sample_model(traf_params_rep, subkey)
        times = np.atleast_1d(np.asarray(samples.squeeze() + time_geo_rep))

        if len(times) == 1:
            ix = np.argwhere(n_ph_per_mod).squeeze()
            times = [[] if i != ix else times for i in range(module_coords.shape[0])]
        else:
            # Split per module and covnert to awkward array
            times = np.split(times, np.cumsum(n_ph_per_mod)[:-1])

        return ak.Array(times)

    return generate_norm_flow_photons


def make_nflow_photon_likelihood_per_module(
    shape_model_path,
    counts_model_path,
    split_shape_counts=False,
    mode="full",
):
    shape_config, shape_params = pickle.load(open(shape_model_path, "rb"))
    counts_config, counts_params = pickle.load(open(counts_model_path, "rb"))

    shape_conditioner = make_shape_conditioner_fn(
        shape_config["mlp_hidden_size"],
        shape_config["mlp_num_layers"],
        shape_config["flow_num_bins"],
        shape_config["flow_num_layers"],
    )

    @jax.jit
    def apply_fn(params, x):
        return shape_conditioner.apply(params, x)

    dist_builder = traf_dist_builder(
        shape_config["flow_num_layers"],
        (shape_config["flow_rmin"], shape_config["flow_rmax"]),
    )

    counts_net = make_counts_net_fn(counts_config)

    @jax.jit
    def counts_net_apply_fn(params, x):
        return counts_net.apply(params, x)

    @jax.jit
    def eval_l_p(traf_params, samples):
        return eval_log_prob(dist_builder, traf_params, samples)

    def eval_per_module_likelihood(
        times,
        module_coords,
        source_pos,
        source_dir,
        source_time,
        source_photons,
        c_medium,
        noise_rate,
    ):

        inp_pars, time_geo = sources_to_model_input_per_module(
            module_coords,
            source_pos,
            source_dir,
            source_time,
            c_medium,
        )

        inp_pars = inp_pars.reshape((source_pos.shape[0], inp_pars.shape[-1]))
        time_geo = time_geo.reshape((source_pos.shape[0], time_geo.shape[-1]))

        ph_frac = jnp.power(10, counts_net_apply_fn(counts_params, inp_pars)).reshape(
            source_pos.shape[0]
        )

        noise_window_len = 6000
        noise_photons = noise_rate * noise_window_len

        n_photons = jnp.reshape(
            ph_frac * source_photons.squeeze(), (source_pos.shape[0],)
        )
        n_ph_pred_per_mod = jnp.sum(n_photons)
        n_ph_pred_per_mod_total = n_ph_pred_per_mod + noise_photons

        n_ph_meas = jnp.isfinite(times).sum()

        counts_lh = jnp.sum(
            -n_ph_pred_per_mod_total + n_ph_meas * jnp.log(n_ph_pred_per_mod_total)
        )

        if mode == "counts":
            return counts_lh

        traf_params = apply_fn(shape_params, inp_pars)
        traf_params = traf_params.reshape((source_pos.shape[0], traf_params.shape[-1]))

        t_res = times - time_geo

        # We have several masks. First, we need to mask all source-module pairs which are not
        # covered by the flow model (distance mask)
        # Second, we have to mask non-finite times, which are created when padding the event.
        # Last, we have to mask time-residuals < -4 (unphysical, will recieve penalty)
        distance_mask = (inp_pars[..., 0] < np.log10(300))[:, np.newaxis]
        finite_times = jnp.isfinite(t_res)
        physical = t_res > -4

        mask = distance_mask & finite_times & physical

        if mode == "tfirst":
            # only select the first measured photon
            first_mask = jnp.zeros_like(mask)
            first_mask = first_mask.at[jnp.argmin(t_res, axis=1), :].set(True)
            mask = mask & first_mask

        traf_params = traf_params.reshape(
            (traf_params.shape[0], 1, traf_params.shape[1])
        )

        # Sanitize likelihood evaluation to avoid nans.
        sanitized_times = jnp.where(mask, t_res, jnp.zeros_like(t_res))
        shape_lh = eval_l_p(traf_params, sanitized_times)

        """
        # Penalty for unphysical times
        unphysical_all_src = jnp.all(~physical, axis=0)  # has shape (nevents,)
        shape_lh = jnp.where(
            unphysical_all_src[np.newaxis, :], jnp.full_like(shape_lh, -1e10), shape_lh
        )
        """

        source_weight = n_photons / jnp.sum(n_photons)
        # Mask the scale factor. This will remove unwanted source-time pairs from the logsumexp
        scale_factor = source_weight[:, np.newaxis] * mask + 1e-10

        # weighted_lh = scale_factor + shape_lh

        shape_lh = jax.scipy.special.logsumexp(shape_lh, b=scale_factor, axis=0)

        noise_lh = -jnp.log(noise_window_len) * n_ph_meas

        total_shape_lh = jnp.logaddexp(
            noise_lh + jnp.log(noise_photons / n_ph_pred_per_mod_total),
            shape_lh + jnp.log(n_ph_pred_per_mod / n_ph_pred_per_mod_total),
        )

        # total_lh = shape_lh

        if split_shape_counts:
            return total_shape_lh, counts_lh, n_ph_pred_per_mod_total

        shape_lh_sum = cond(
            jnp.any(finite_times), lambda tlh: tlh.sum(), lambda _: 0.0, total_shape_lh
        )

        return shape_lh_sum + counts_lh

    return eval_per_module_likelihood


def make_nflow_photon_likelihood(shape_model_path, counts_model_path):
    raise RuntimeError("Add noise")

    shape_config, shape_params = pickle.load(open(shape_model_path, "rb"))
    counts_config, counts_params = pickle.load(open(counts_model_path, "rb"))

    shape_conditioner = make_shape_conditioner_fn(
        shape_config["mlp_hidden_size"],
        shape_config["mlp_num_layers"],
        shape_config["flow_num_bins"],
        shape_config["flow_num_layers"],
    )

    @jax.jit
    def apply_fn(params, x):
        return shape_conditioner.apply(params, x)

    dist_builder = traf_dist_builder(
        shape_config["flow_num_layers"],
        (shape_config["flow_rmin"], shape_config["flow_rmax"]),
    )

    counts_net = make_counts_net_fn(counts_config)

    @jax.jit
    def counts_net_apply_fn(params, x):
        return counts_net.apply(params, x)

    @jax.jit
    def eval_l_p(traf_params, samples):
        return eval_log_prob(dist_builder, traf_params, samples)

    def eval_likelihood(
        event,
        module_coords,
        source_pos,
        source_dir,
        source_time,
        source_photons,
        c_medium,
    ):
        inp_pars, time_geo = sources_to_model_input(
            module_coords,
            source_pos,
            source_dir,
            source_time,
            c_medium,
        )

        distance_mask = inp_pars[..., 0] < np.log10(300)
        inp_pars = inp_pars.reshape(
            (source_pos.shape[0] * module_coords.shape[0], inp_pars.shape[-1])
        )

        traf_params = apply_fn(shape_params, inp_pars)
        traf_params = traf_params.reshape(
            (source_pos.shape[0], module_coords.shape[0], traf_params.shape[-1])
        )

        hits_per_mod = jnp.asarray(ak.count(event, axis=1))

        flat_ev = jnp.asarray(ak.ravel(event))
        traf_params_rep = jnp.repeat(traf_params, hits_per_mod, axis=1)
        time_geo_rep = jnp.repeat(time_geo, hits_per_mod, axis=1).squeeze()
        distance_mask_rep = jnp.repeat(distance_mask, hits_per_mod, axis=1)

        t_res = flat_ev - time_geo_rep

        mask = distance_mask_rep & (t_res >= -4)
        shape_lh = jnp.where(
            mask, eval_l_p(traf_params_rep, t_res), jnp.zeros_like(distance_mask_rep)
        )

        ph_frac = jnp.power(10, counts_net_apply_fn(counts_params, inp_pars)).reshape(
            source_pos.shape[0], module_coords.shape[0]
        )

        n_photons = ph_frac * source_photons
        n_ph_pred_per_mod = jnp.sum(n_photons, axis=0)

        counts_lh = -n_ph_pred_per_mod + hits_per_mod * jnp.log(n_ph_pred_per_mod)

        return shape_lh.sum() + counts_lh.sum()

        lhsum = 0
        for imod in range(module_coords.shape[0]):
            if ak.count(event[imod]) == 0:
                continue

            dist_pars = traf_params[:, imod]
            mask = distance_mask[:, imod]

            if jnp.all(~mask):
                continue
            masked_pars = dist_pars[mask]

            t_res = jnp.asarray(event[imod]) - time_geo[:, imod][mask]

            per_mod_lh = eval_l_p(masked_pars, t_res.T)
            t_res_mask = t_res > -4

            zero_fill = jnp.zeros_like(per_mod_lh)

            lhsum += jnp.sum(jnp.where(t_res_mask.T, per_mod_lh, zero_fill))

            # lhsum += jnp.sum(per_mod_lh[t_res_mask.T])

        return lhsum

    return eval_likelihood
