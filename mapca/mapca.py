"""
PCA based on Moving Average (stationary Gaussian) process
"""
import logging

import numpy as np

from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

from . import utils

LGR = logging.getLogger(__name__)


def ma_pca(data_nib, mask_nib, criteria="mdl"):
    """
    Run Singular Value Decomposition (SVD) on input data,
    automatically select components based on a Moving Average
    (stationary Gaussian) process. Finally perform PCA with
    selected number of components.

    Parameters
    ----------
    data_nib : 4D nibabel
               Unmasked data to compute the PCA on.
    mask_nib : 4D nibabel
               Mask to apply on data_nib.
    criteria : string in ['aic', 'kic', mdl']
               Criteria to select the number of components;
               default='mdl'.

    Returns
    -------
    u : (S [*E] x C) array-like
        Component weight map for each component.
    s : (C,) array-like
        Variance explained for each component.
    varex_norm : (n_components,) array-like
        Explained variance ratio.
    v : (T x C) array-like
        Component timeseries.

    Notes
    -----
    aic : Akaike Information Criterion. Least aggressive option.
    kic : Kullback-Leibler Information Criterion. Stands in the
          middle in terms of aggressiveness.
    mdl : Minimum Description Length. Most aggressive
          (and recommended) option.
    """

    data_nib = data_nib.get_fdata()
    mask_nib = mask_nib.get_fdata()
    [Nx, Ny, Nz, Nt] = data_nib.shape
    data_nib_V = np.reshape(data_nib, (Nx * Ny * Nz, Nt), order="F")
    maskvec = np.reshape(mask_nib, Nx * Ny * Nz, order="F")
    data_non_normalized = data_nib_V[maskvec == 1, :]
    scaler = StandardScaler(with_mean=True, with_std=True)
    # TODO: determine if tedana is already normalizing before this
    data = scaler.fit_transform(data_non_normalized)  # This was X_sc
    data = data_non_normalized

    LGR.info("Performing SVD on original OC data...")
    V, EigenValues = utils._icatb_svd(data, Nt)
    LGR.info("SVD done on original OC data")

    # Reordering of values
    EigenValues = EigenValues[::-1]
    dataN = np.dot(data, V[:, ::-1])
    # Potentially the small differences come from the different signs on V

    # Using 12 gaussian components from middle, top and bottom gaussian
    # components to determine the subsampling depth. Final subsampling depth is
    # determined using median
    kurtv1 = utils._kurtn(dataN)
    kurtv1[EigenValues > np.mean(EigenValues)] = 1000
    idx_gauss = np.where(
        (
            (kurtv1[:, 0] < 0.3)
            & (kurtv1[:, 0] > 0)
            & (EigenValues > np.finfo(float).eps)
        )
        == 1
    )[
        0
    ]  # DOUBT: make sure np.where is giving us just one tuple
    idx = np.array(idx_gauss[:]).T
    dfs = np.sum(EigenValues > np.finfo(float).eps)  # degrees of freedom
    minTp = 12

    if len(idx) >= minTp:
        middle = int(np.round(len(idx) / 2))
        idx = np.hstack([idx[0:4], idx[middle - 1 : middle + 3], idx[-4:]])
    else:
        minTp = np.min([minTp, dfs])
        idx = np.arange(dfs - minTp, dfs)

    idx = np.unique(idx)

    # Estimate the subsampling depth for effectively i.i.d. samples
    LGR.info("Estimating the subsampling depth for effective i.i.d samples...")
    mask_ND = np.reshape(maskvec, (Nx, Ny, Nz), order="F")
    sub_depth = len(idx)
    sub_iid_sp = np.zeros((sub_depth,))
    for i in range(sub_depth):
        x_single = np.zeros(Nx * Ny * Nz)
        x_single[maskvec == 1] = dataN[:, idx[i]]
        x_single = np.reshape(x_single, (Nx, Ny, Nz), order="F")
        sub_iid_sp[i] = utils._est_indp_sp(x_single)[0] + 1
        if i > 6:
            tmp_sub_sp = sub_iid_sp[0:i]
            tmp_sub_median = np.round(np.median(tmp_sub_sp))
            if np.sum(tmp_sub_sp == tmp_sub_median) > 6:
                sub_iid_sp = tmp_sub_sp
                break
        dim_n = x_single.ndim

    sub_iid_sp_median = int(np.round(np.median(sub_iid_sp)))
    if np.floor(np.power(np.sum(maskvec) / Nt, 1 / dim_n)) < sub_iid_sp_median:
        sub_iid_sp_median = int(np.floor(np.power(np.sum(maskvec) / Nt, 1 / dim_n)))
    N = np.round(np.sum(maskvec) / np.power(sub_iid_sp_median, dim_n))

    if sub_iid_sp_median != 1:
        mask_s = utils._subsampling(mask_ND, sub_iid_sp_median)
        mask_s_1d = np.reshape(mask_s, np.prod(mask_s.shape), order="F")
        dat = np.zeros((int(np.sum(mask_s_1d)), Nt))
        LGR.info("Generating subsampled i.i.d. OC data...")
        for i in range(Nt):
            x_single = np.zeros((Nx * Ny * Nz,))
            x_single[maskvec == 1] = data[:, i]
            x_single = np.reshape(x_single, (Nx, Ny, Nz), order="F")
            dat0 = utils._subsampling(x_single, sub_iid_sp_median)
            dat0 = np.reshape(dat0, np.prod(dat0.shape), order="F")
            dat[:, i] = dat0[mask_s_1d == 1]

        # Perform Variance Normalization
        dat = scaler.fit_transform(dat)

        # (completed)
        LGR.info("Performing SVD on subsampled i.i.d. OC data...")
        [V, EigenValues] = utils._icatb_svd(dat, Nt)
        LGR.info("SVD done on subsampled i.i.d. OC data")
        EigenValues = EigenValues[::-1]

    LGR.info("Effective number of i.i.d. samples %d" % N)

    # Make eigen spectrum adjustment
    LGR.info("Perform eigen spectrum adjustment ...")
    EigenValues = utils._eigensp_adj(EigenValues, N, EigenValues.shape[0])
    # (completed)
    if np.sum(np.imag(EigenValues)):
        raise ValueError("Invalid eigen value found for the subsampled data.")

    # Correction on the ill-conditioned results (when tdim is large,
    # some least significant eigenvalues become small negative numbers)
    if EigenValues[np.real(EigenValues) <= np.finfo(float).eps].shape[0] > 0:
        EigenValues[np.real(EigenValues) <= np.finfo(float).eps] = np.min(
            EigenValues[np.real(EigenValues) >= np.finfo(float).eps]
        )
    LGR.info("Estimating the dimension ...")
    p = Nt
    aic = np.zeros(p - 1)
    kic = np.zeros(p - 1)
    mdl = np.zeros(p - 1)

    for k_idx, k in enumerate(np.arange(1, p)):
        LH = np.log(
            np.prod(np.power(EigenValues[k:], 1 / (p - k))) / np.mean(EigenValues[k:])
        )
        mlh = 0.5 * N * (p - k) * LH
        df = 1 + 0.5 * k * (2 * p - k + 1)
        aic[k_idx] = (-2 * mlh) + (2 * df)
        kic[k_idx] = (-2 * mlh) + (3 * df)
        mdl[k_idx] = -mlh + (0.5 * df * np.log(N))

    itc = np.row_stack([aic, kic, mdl])

    if criteria == "aic":
        criteria_idx = 0
    elif criteria == "kic":
        criteria_idx = 1
    elif criteria == "mdl":
        criteria_idx = 2

    dlap = np.diff(itc[criteria_idx, :])
    a = np.where(dlap > 0)[0] + 1  # Plus 1 to
    if a.size == 0:
        comp_est = itc[criteria_idx, :].shape[0]
    else:
        comp_est = a[0]

    LGR.info("Estimated components is found out to be %d" % comp_est)

    # PCA with estimated number of components
    ppca = PCA(n_components=comp_est, svd_solver="full", copy=False)
    ppca.fit(data)
    v = ppca.components_.T
    s = ppca.explained_variance_
    u = np.dot(np.dot(data, v), np.diag(1.0 / s))
    varex_norm = ppca.explained_variance_ratio_

    return u, s, varex_norm, v