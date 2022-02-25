# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import copy
import itertools

import numpy as np
import pandas as pd
import scanpy as sc
import torch
from anndata import AnnData
from sklearn.metrics import r2_score
from sklearn.metrics.pairwise import cosine_distances, euclidean_distances

from ._model import CPA
from ._utils import _CE_CONSTANTS


class ComPertAPI:
    """
    API for CPA model to make it compatible with scanpy.
    """

    def __init__(self, adata: AnnData, model: CPA):
        """
        Parameters
        ----------
        adata : ComPertDataset
            Full dataset.
        model : ComPertModel
            Pre-trained ComPert model.
        """
        self.perturbation_key = _CE_CONSTANTS.DRUG_KEY
        self.dose_key = _CE_CONSTANTS.DOSE_KEY
        self.covars_key = _CE_CONSTANTS.COVARS_KEYS
        self.min_dose = adata.obs[_CE_CONSTANTS.DOSE_KEY].min().item()
        self.max_dose = adata.obs[_CE_CONSTANTS.DOSE_KEY].max().item()

        self.model = model
        self.adata = adata
        self.var_names = adata.var_names

        self.de_genes = adata.uns['rank_genes_groups_cov']

        self.split_key = model.split_key
        data_types = list(np.unique(adata.obs[self.split_key])) + ['all']

        self.unique_perts = list(model.drug_encoder.keys())
        self.unique_covars = {}
        for covar in model.covars_encoder.keys():
            self.unique_covars[covar] = list(model.covars_encoder[covar].keys())

        self.num_drugs = len(model.drug_encoder)

        self.perts_dict = model.drug_encoder.copy()
        self.covars_dict = model.covars_encoder.copy()

        self.emb_covars = None
        self.emb_perts = None
        self.seen_covars_perts = None
        self.comb_emb = None
        self.control_cat = None

        self.seen_covars_perts = {}
        self.adatas = {}
        self.pert_categories_key = 'cov_drug_dose_name'
        for k in data_types:
            if k == 'all':
                self.adatas[k] = adata.copy()
                self.seen_covars_perts[k] = np.unique(self.adatas[k].obs[self.pert_categories_key])
            else:
                self.adatas[k] = adata[adata.obs[self.split_key] == k]
                self.seen_covars_perts[k] = np.unique(self.adatas[k].obs[self.pert_categories_key])

        self.measured_points = {}
        self.num_measured_points = {}
        for k in data_types:
            self.measured_points[k] = {}
            self.num_measured_points[k] = {}
            for pert in self.seen_covars_perts[k]:
                num_points = len(np.where(self.adatas[k].obs[self.pert_categories_key] == pert)[0])
                self.num_measured_points[k][pert] = num_points

                cov, drug, dose = pert.split('_')
                if not ('+' in dose):
                    dose = float(dose)
                if cov in self.measured_points[k].keys():
                    if drug in self.measured_points[k][cov].keys():
                        self.measured_points[k][cov][drug].append(dose)
                    else:
                        self.measured_points[k][cov][drug] = [dose]
                else:
                    self.measured_points[k][cov] = {drug: [dose]}

        self.measured_points['all'] = copy.deepcopy(self.measured_points['train'])
        for cov in self.measured_points['ood'].keys():
            for pert in self.measured_points['ood'][cov].keys():
                if pert in self.measured_points['train'][cov].keys():
                    self.measured_points['all'][cov][pert] = \
                        self.measured_points['train'][cov][pert].copy() + \
                        self.measured_points['ood'][cov][pert].copy()
                else:
                    self.measured_points['all'][cov][pert] = \
                        self.measured_points['ood'][cov][pert].copy()

    @torch.no_grad()
    def get_drug_embeddings(self, dose=1.0):
        """
        Parameters
        ----------
        dose : int (default: 1.0)
            Dose at which to evaluate latent embedding vector.

        Returns
        -------
        If return_anndata is True, returns anndata object. Otherwise, doesn't
        return anything. Always saves embeddding in self.emb_perts.
        """
        embeddings = self.model.get_drug_embeddings(dose)
        return AnnData(X=embeddings, obs={self.perturbation_key: self.unique_perts})

    def get_covars_embeddings(self, covariate: str):
        """
        Parameters
        ----------
        covariate: str
            covariate column name in adata.obs dataframe

        Returns
        -------
        If return_anndata is True, returns anndata object. Otherwise, doesn't
        return anything. Always saves embeddding in self.emb_covars.
        """
        self.emb_covars = self.model.get_covar_embeddings(covariate)

        adata = sc.AnnData(self.emb_covars)
        adata.obs[covariate] = self.unique_covars[covariate]
        return adata

    def get_drug_encoding_(self, drugs, doses=None):
        """
        Parameters
        ----------
        drugs : str
            Drugs combination as a string, where individual drugs are separated
            with a plus.
        doses : str, optional (default: None)
            Doses corresponding to the drugs combination as a string. Individual
            drugs are separated with a plus.

        Returns
        -------
        One hot encodding for a mixture of drugs.
        """

        drug_mix = np.zeros([1, self.num_drugs])
        atomic_drugs = drugs.split('+')
        doses = str(doses)

        if doses is None:
            doses_list = [1.0] * len(atomic_drugs)
        else:
            doses_list = [float(d) for d in str(doses).split('+')]
        for j, drug in enumerate(atomic_drugs):
            drug_mix += doses_list[j] * self.perts_dict[drug]

        return drug_mix

    def mix_drugs(self, drugs_list, doses_list=None):
        """
        Gets a list of drugs combinations to mix, e.g. ['A+B', 'B+C'] and
        corresponding doses.

        Parameters
        ----------
        drugs_list : list
            List of drug combinations, where each drug combination is a string.
            Individual drugs in the combination are separated with a plus.

        doses_list : str, optional (default: None)
            List of corresponding doses, where each dose combination is a string.
            Individual doses in the combination are separated with a plus.

        Returns
        -------
        If return_anndata is True, returns anndata structure of the combinations,
        otherwise returns a np.array of corresponding embeddings.
        """

        drug_mix = np.zeros([len(drugs_list), self.num_drugs])
        for i, drug_combo in enumerate(drugs_list):
            drug_mix[i] = self.get_drug_encoding_(drug_combo, doses=doses_list[i])

        emb = self.model.get_drug_embeddings(torch.Tensor(drug_mix).to(
            self.model.device)).cpu().clone().detach().numpy()

        adata = sc.AnnData(emb)
        adata.obs[self.perturbation_key] = drugs_list
        adata.obs[self.dose_key] = doses_list
        return adata

    def latent_dose_response(self, perturbations=None, dose=None,
                             contvar_min=0, contvar_max=1, n_points=100):
        """
        Parameters
        ----------
        perturbations : list
            List containing two names for which to return complete pairwise
            dose-response.
        doses : np.array (default: None)
            Doses values. If None, default values will be generated on a grid:
            n_points in range [contvar_min, contvar_max].
        contvar_min : float (default: 0)
            Minimum dose value to generate for default option.
        contvar_max : float (default: 0)
            Maximum dose value to generate for default option.
        n_points : int (default: 100)
            Number of dose points to generate for default option.
        Returns
        -------
        pd.DataFrame
        """
        # dosers work only for atomic drugs. TODO add drug combinations

        if perturbations is None:
            perturbations = self.unique_perts

        if dose is None:
            dose = np.linspace(contvar_min, contvar_max, n_points)
        n_points = len(dose)

        df = pd.DataFrame(columns=[self.perturbation_key, self.dose_key, 'response'])

        for drug in perturbations:
            d = self.perts_dict[drug]
            this_drug = torch.Tensor(dose).to(self.model.device).view(-1, 1)
            if self.model.module.doser_type == 'mlp':
                response = (self.model.module.drug_network.dosers[d](this_drug).sigmoid() * this_drug.gt(
                    0)).cpu().clone().detach().numpy().reshape(-1)
            else:
                response = self.model.module.drug_network.dosers.one_drug(this_drug.view(-1),
                                                                          d).cpu().clone().detach().numpy().reshape(-1)

            df_drug = pd.DataFrame(list(zip([drug] * n_points, dose, list(response))),
                                   columns=[self.perturbation_key, self.dose_key, 'response'])
            df = pd.concat([df, df_drug], ignore_index=True)

        return df

    def latent_dose_response2D(self, perturbations, dose=None,
                               contvar_min=0, contvar_max=1, n_points=100, ):
        """
        Parameters
        ----------
        perturbations : list, optional (default: None)
            List of atomic drugs for which to return latent dose response.
            Currently drug combinations are not supported.
        doses : np.array (default: None)
            Doses values. If None, default values will be generated on a grid:
            n_points in range [contvar_min, contvar_max].
        contvar_min : float (default: 0)
            Minimum dose value to generate for default option.
        contvar_max : float (default: 0)
            Maximum dose value to generate for default option.
        n_points : int (default: 100)
            Number of dose points to generate for default option.
        Returns
        -------
        pd.DataFrame
        """
        # dosers work only for atomic drugs. TODO add drug combinations

        assert len(perturbations) == 2, "You should provide a list of 2 perturbations."

        if dose is None:
            dose = np.linspace(contvar_min, contvar_max, n_points)
        n_points = len(dose)

        df = pd.DataFrame(columns=perturbations + ['response'])
        response = {}

        for drug in perturbations:
            d = self.perts_dict[drug]
            this_drug = torch.Tensor(dose).to(self.model.device).view(-1, 1)
            if self.model.module.doser_type == 'mlp':
                response[drug] = (self.model.module.drug_network.dosers[d](this_drug).sigmoid() * this_drug.gt(
                    0)).cpu().clone().detach().numpy().reshape(-1)
            else:
                response[drug] = self.model.module.drug_network.dosers.one_drug(this_drug.view(-1),
                                                                                d).cpu().clone().detach().numpy().reshape(
                    -1)

        l = 0
        for i in range(len(dose)):
            for j in range(len(dose)):
                df.loc[l] = [dose[i], dose[j], response[perturbations[0]][i] + response[perturbations[1]][j]]
                l += 1

        return df

    def compute_comb_emb(self, thrh=30):
        """
        Generates an AnnData object containing all the latent vectors of the
        cov+dose*pert combinations seen during training.
        Called in api.compute_uncertainty(), stores the AnnData in self.comb_emb.

        Parameters
        ----------
        Returns
        -------
        """
        if self.seen_covars_perts['train'] is None:
            raise ValueError('Need to run parse_training_conditions() first!')

        emb_covars = None
        for cov in self.unique_covars.keys():
            emb_covars = self.get_covars_embeddings(cov) if emb_covars is None else emb_covars.concatenate(
                self.get_covars_embeddings(cov))

        # Generate adata with all cov+pert latent vect combinations
        tmp_ad_list = []
        for cov_pert in self.seen_covars_perts['train']:
            if self.num_measured_points['train'][cov_pert] > thrh:
                *covs_loop, pert_loop, dose_loop = cov_pert.split('_')
                emb_perts_loop = []
                # if '+' in pert_loop:
                #     pert_loop_list = pert_loop.split('+')
                #     dose_loop_list = dose_loop.split('+')
                #     for _dose in pd.Series(dose_loop_list).unique():
                #         tmp_ad = self.get_drug_embeddings(dose=float(_dose))
                #         tmp_ad.obs['pert_dose'] = tmp_ad.obs.condition + '_' + _dose
                #         emb_perts_loop.append(tmp_ad)
                #
                #     emb_perts_loop = emb_perts_loop[0].concatenate(emb_perts_loop[1:])
                #     X = (
                #             sum([emb_covars.X[emb_covars.obs.cell_type == cov_loop] for cov_loop in covs_loop])
                #             + np.expand_dims(
                #         emb_perts_loop.X[
                #             emb_perts_loop.obs.pert_dose.isin(
                #                 [
                #                     pert_loop_list[i] + '_' + dose_loop_list[i]
                #                     for i in range(len(pert_loop_list))
                #                 ]
                #             )
                #         ].sum(axis=0),
                #         axis=0
                #     )
                #     )
                #     if X.shape[0] > 1:
                #         raise ValueError('Error with comb computation')
                # else:
                emb_perts = self.get_drug_embeddings(dose=float(dose_loop))
                X = (
                        sum([emb_covars.X[emb_covars.obs.cell_type == cov_loop] for cov_loop in covs_loop])
                        + emb_perts.X[emb_perts.obs.condition == pert_loop]
                )
                tmp_ad = sc.AnnData(
                    X=X
                )
                tmp_ad.obs['cov_pert'] = '_'.join([*covs_loop, pert_loop, dose_loop])
            tmp_ad_list.append(tmp_ad)

        self.comb_emb = tmp_ad_list[0].concatenate(tmp_ad_list[1:])

    def compute_uncertainty(
            self,
            covs,
            pert,
            dose,
            thrh=30
    ):
        """
        Compute uncertainties for the queried covariate+perturbation combination.
        The distance from the closest condition in the training set is used as a
        proxy for uncertainty.

        Parameters
        ----------
        covs: list of strings
            Covariates (eg. cell_type) for the queried uncertainty
        pert: string
            Perturbation for the queried uncertainty. In case of combinations the
            format has to be 'pertA+pertB'
        dose: string
            String which contains the dose of the perturbation queried. In case
            of combinations the format has to be 'doseA+doseB'

        Returns
        -------
        min_cos_dist: float
            Minimum cosine distance with the training set.
        min_eucl_dist: float
            Minimum euclidean distance with the training set.
        closest_cond_cos: string
            Closest training condition wrt cosine distances.
        closest_cond_eucl: string
            Closest training condition wrt euclidean distances.
        """

        if self.comb_emb is None:
            self.compute_comb_emb(thrh=30)

        drug_emb = self.model.get_drug_embeddings(doses=1.0, drug=pert)
        cond_emb = drug_emb
        for cov in covs:
            for cov_col, cov_col_values in self.unique_covars.items():
                if cov in cov_col_values:
                    cond_emb += self.model.get_covar_embeddings(cov_col, cov).reshape(-1, )
                    break

        cos_dist = cosine_distances(cond_emb, self.comb_emb.X)[0]
        min_cos_dist = np.min(cos_dist)
        cos_idx = np.argmin(cos_dist)
        closest_cond_cos = self.comb_emb.obs.cov_pert[cos_idx]

        eucl_dist = euclidean_distances(cond_emb, self.comb_emb.X)[0]
        min_eucl_dist = np.min(eucl_dist)
        eucl_idx = np.argmin(eucl_dist)
        closest_cond_eucl = self.comb_emb.obs.cov_pert[eucl_idx]

        return min_cos_dist, min_eucl_dist, closest_cond_cos, closest_cond_eucl

    def predict(
            self,
            genes,
            df,
            uncertainty=True,
            sample=False,
            n_samples=10
    ):
        """Predict values of control 'genes' conditions specified in df.

        Parameters
        ----------
        genes : np.array
            Control cells.
        df : pd.DataFrame
            Values for perturbations and covariates to generate.
        uncertainty: bool (default: True)
            Compute uncertainties for the generated cells.
        sample : bool (default: False)
            If sample is True, returns samples from gausssian distribution with
            mean and variance estimated by the model. Otherwise, returns just
            means and variances estimated by the model.
        n_samples : int (default: 10)
            Number of samples to sample if sampling is True.
        Returns
        -------
        If return_anndata is True, returns anndata structure. Otherwise, returns
        np.arrays for gene_means, gene_vars and a data frame for the corresponding
        conditions df_obs.

        """
        num = genes.shape[0]
        dim = genes.shape[1]
        if sample:
            print('Careful! These are sampled values! Better use means and \
                variances for downstream tasks!')

        gene_means_list = []
        gene_vars_list = []
        df_list = []

        for i in range(len(df)):
            comb_name = df.loc[i][self.perturbation_key]
            dose_name = df.loc[i][self.dose_key]
            covars_name = list(df.loc[i][self.covars_key])

            feed_adata = AnnData(X=genes,
                                 obs={self.perturbation_key: [comb_name] * num,
                                      self.dose_key: [dose_name] * num,
                                      })

            for idx, covar in enumerate(covars_name):
                feed_adata.obs[self.covars_key[idx]] = [covar] * num

            pred_adata_mean, pred_adata_var = self.model.predict(feed_adata)

            gene_means_list.append(pred_adata_mean.X)
            gene_vars_list.append(pred_adata_var.X)

            if sample:
                df_list.append(
                    pd.DataFrame(
                        [df.loc[i].values] * num * n_samples,
                        columns=df.columns
                    )
                )
                dist = torch.distributions.normal.Normal(
                    torch.Tensor(pred_adata_mean.X),
                    torch.Tensor(pred_adata_var.X),
                )
                gene_means_list.append(
                    dist.sample(torch.Size([n_samples]))
                        .cpu()
                        .detach()
                        .numpy()
                        .reshape(-1, dim)
                )
            else:
                df_list.append(
                    pd.DataFrame(
                        [df.loc[i].values] * num,
                        columns=df.columns
                    )
                )

            if uncertainty:
                cos_dist, eucl_dist, closest_cond_cos, closest_cond_eucl = \
                    self.compute_uncertainty(
                        covs=covars_name,
                        pert=comb_name,
                        dose=dose_name
                    )
                df_list[-1] = df_list[-1].assign(
                    uncertainty_cosine=cos_dist,
                    uncertainty_euclidean=eucl_dist,
                    closest_cond_cosine=closest_cond_cos,
                    closest_cond_euclidean=closest_cond_eucl
                )

        gene_means = np.concatenate(gene_means_list)
        gene_vars = np.concatenate(gene_vars_list)
        df_obs = pd.concat(df_list)
        del df_list, gene_means_list, gene_vars_list

        adata = sc.AnnData(gene_means)
        adata.var_names = self.var_names
        adata.obs = df_obs
        if not sample:
            adata.layers["variance"] = gene_vars

        adata.obs.index = adata.obs.index.astype(str)  # type fix
        return adata

    def get_response(
            self,
            datasets,
            doses=None,
            contvar_min=None,
            contvar_max=None,
            n_points=50,
            ncells_max=100,
            perturbations=None,
            control_name='test_control'
    ):
        """Decoded dose response data frame.

        Parameters
        ----------
        dataset : CompPertDataset
            The file location of the spreadsheet
        doses : np.array (default: None)
            Doses values. If None, default values will be generated on a grid:
            n_points in range [contvar_min, contvar_max].
        contvar_min : float (default: 0)
            Minimum dose value to generate for default option.
        contvar_max : float (default: 0)
            Maximum dose value to generate for default option.
        n_points : int (default: 100)
            Number of dose points to generate for default option.
        perturbations : list (default: None)
            List of perturbations for dose response

        Returns
        -------
        pd.DataFrame
            of decoded response values of genes and average response.
        """

        if contvar_min is None:
            contvar_min = self.min_dose
        if contvar_max is None:
            contvar_max = self.max_dose

        # doses = torch.Tensor(np.linspace(contvar_min, contvar_max, n_points))
        if doses is None:
            doses = np.linspace(contvar_min, contvar_max, n_points)

        if perturbations is None:
            perturbations = self.unique_perts

        response = pd.DataFrame(columns=[self.covars_key,
                                         self.perturbation_key,
                                         self.dose_key,
                                         'response'] + list(self.var_names))

        i = 0
        for ict, ct in enumerate(self.unique_covars):
            genes_control = self.adatas[control_name].genes[datasets[control_name].cell_types_names == \
                                             ct].clone().detach()
            if len(genes_control) < 1:
                print('Warning! Not enought control cells for this covariate.\
                    Taking control cells from all covariates.')
                genes_control = datasets[control_name].genes

            if ncells_max < len(genes_control):
                ncells_max = min(ncells_max, len(genes_control))
                idx = torch.LongTensor(np.random.choice(range(len(genes_control)), \
                                                        ncells_max, replace=False))
                genes_control = genes_control[idx]

            num, dim = genes_control.size(0), genes_control.size(1)
            control_avg = genes_control.mean(dim=0).cpu().clone().detach().numpy().reshape(-1)

            for idr, drug in enumerate(perturbations):
                if not (drug in datasets[control_name].ctrl_name):
                    for dose in doses:
                        df = pd.DataFrame(data={self.covars_key: [ct],
                                                self.perturbation_key: [drug], self.dose_key: [dose]})

                        gene_means, _, _ = \
                            self.predict(genes_control.cpu().detach().numpy(), \
                                         df, return_anndata=False)
                        predicted_data = np.mean(gene_means, axis=0).reshape(-1)

                        response.loc[i] = [ct, drug, dose,
                                           np.linalg.norm(predicted_data - control_avg)] + \
                                          list(predicted_data - control_avg)
                        i += 1
        return response

    def get_response_reference(
            self,
            perturbations=None
    ):

        """Computes reference values of the response.

        Parameters
        ----------
        dataset : CompPertDataset
            The file location of the spreadsheet
        perturbations : list (default: None)
            List of perturbations for dose response

        Returns
        -------
        pd.DataFrame
            of decoded response values of genes and average response.
        """
        if perturbations is None:
            perturbations = self.unique_perts

        reference_response_curve = pd.DataFrame(columns=[self.covars_key,
                                                         self.perturbation_key,
                                                         self.dose_key,
                                                         'split',
                                                         'num_cells',
                                                         'response'] + \
                                                        list(self.var_names))

        dataset_ctr = self.adatas['train'][self.adatas['train'].obs[self.perturbation_key] == 'control']
        dataset_trt = self.adatas['train'][self.adatas['train'].obs[self.perturbation_key] != 'control']

        self.adatas['training_control'] = dataset_ctr
        self.adatas['training_treated'] = dataset_trt

        i = 0
        for split in ['training_treated', 'ood']:
            dataset = self.adatas[split]
            for pert in self.seen_covars_perts[split]:
                ct, drug, dose_val = pert.split('_')
                if drug in perturbations:
                    if not ('+' in dose_val):
                        dose = float(dose_val)
                    else:
                        dose = dose_val

                    genes_control = dataset_ctr[dataset_ctr.obs['cell_type'] == ct].X
                    if not isinstance(genes_control, np.ndarray):
                        genes_control = genes_control.toarray()
                    if len(genes_control) < 1:
                        print('Warning! Not enough control cells for this covariate. \
                            Taking control cells from all covariates.')
                        genes_control = dataset_ctr.X

                    num, dim = genes_control.size(0), genes_control.size(1)
                    control_avg = \
                        genes_control.mean(dim=0).cpu().clone().detach().numpy().reshape(-1)

                    idx = np.where((dataset.obs[self.pert_categories_key] == pert))[0]

                    if len(idx):
                        y_true = dataset.X[idx, :].numpy().mean(axis=0)
                        reference_response_curve.loc[i] = [ct, drug,
                                                           dose, split, len(idx),
                                                           np.linalg.norm(y_true - control_avg)] + \
                                                          list(y_true - control_avg)

                        i += 1

        return reference_response_curve

    def get_response2D(
            self,
            datasets,
            perturbations,
            covar,
            doses=None,
            contvar_min=None,
            contvar_max=None,
            n_points=10,
            ncells_max=100,
            fixed_drugs='',
            fixed_doses=''
    ):
        """Decoded dose response data frame.

        Parameters
        ----------
        dataset : CompPertDataset
            The file location of the spreadsheet
        perturbations : list
            List of length 2 of perturbations for dose response.
        covar : str
            Name of a covariate for which to compute dose-response.
        doses : np.array (default: None)
            Doses values. If None, default values will be generated on a grid:
            n_points in range [contvar_min, contvar_max].
        contvar_min : float (default: 0)
            Minimum dose value to generate for default option.
        contvar_max : float (default: 0)
            Maximum dose value to generate for default option.
        n_points : int (default: 100)
            Number of dose points to generate for default option.

        Returns
        -------
        pd.DataFrame
            of decoded response values of genes and average response.
        """

        assert len(perturbations) == 2, "You should provide a list of 2 perturbations."

        if contvar_min is None:
            contvar_min = self.min_dose

        if contvar_max is None:
            contvar_max = self.max_dose

        # doses = torch.Tensor(np.linspace(contvar_min, contvar_max, n_points))
        if doses is None:
            doses = np.linspace(contvar_min, contvar_max, n_points)

        # genes_control = dataset.genes[dataset.indices['control']]
        genes_control = \
            datasets['test_control'].genes[datasets['test_control'].cell_types_names == \
                                           covar].clone().detach()
        if len(genes_control) < 1:
            print('Warning! Not enought control cells for this covariate. \
                Taking control cells from all covariates.')
            genes_control = datasets['test_control'].genes

        ncells_max = min(ncells_max, len(genes_control))
        idx = torch.LongTensor(np.random.choice(range(len(genes_control)), ncells_max))
        genes_control = genes_control[idx]

        num, dim = genes_control.size(0), genes_control.size(1)
        control_avg = genes_control.mean(dim=0).cpu().clone().detach().numpy().reshape(-1)

        response = pd.DataFrame(columns=perturbations + ['response'] + \
                                        list(self.var_names))

        drug = perturbations[0] + '+' + perturbations[1]

        dose_vals = [f"{d[0]}+{d[1]}" for d in itertools.product(*[doses, doses])]
        dose_comb = [list(d) for d in itertools.product(*[doses, doses])]

        i = 0
        if not (drug in ['Vehicle', 'EGF', 'unst', 'control', 'ctrl']):
            for dose in dose_vals:
                df = pd.DataFrame(data={self.covars_key: [covar],
                                        self.perturbation_key: [drug + fixed_drugs],
                                        self.dose_key: [dose + fixed_doses]})

                gene_means, _, _ = self.predict(
                    genes_control.cpu().detach().numpy(), df,
                    return_anndata=False)

                predicted_data = np.mean(gene_means, axis=0).reshape(-1)

                response.loc[i] = dose_comb[i] + \
                                  [np.linalg.norm(control_avg - predicted_data)] + \
                                  list(predicted_data - control_avg)
                i += 1

        return response

    def get_cycle_uncertainty(
            self,
            genes_from,
            df_from,
            df_to,
            ncells_max=100,
            direction='forward'
    ):

        """Uncertainty for a single condition.

        Parameters
        ----------
        genes_from: torch.Tensor
            Genes for comparison.
        df_from: pd.DataFrame
            Full description of the condition.
        df_to: pd.DataFrame
            Full description of the control condition.
        ncells_max: int, optional (defaul: 100)
            Max number of cells to use.
        Returns
        -------
        tuple
            with uncertainty estimations: (MSE, 1-R2).
        """
        self.model.eval()
        genes_control = genes_from.clone().detach()

        if ncells_max < len(genes_control):
            idx = torch.LongTensor(np.random.choice(range(len(genes_control)), \
                                                    ncells_max, replace=False))
            genes_control = genes_control[idx]

        gene_condition, _, _ = self.predict(genes_control, df_to, \
                                            return_anndata=False, sample=False)
        gene_condition = torch.Tensor(gene_condition).clone().detach()
        gene_return, _, _ = self.predict(gene_condition, df_from, \
                                         return_anndata=False, sample=False)

        if direction == 'forward':
            # control -> condition -> control'
            genes_control = genes_control.numpy()
            ctr = np.mean(genes_control, axis=0)
            ret = np.mean(gene_return, axis=0)
            return np.mean((genes_control - gene_return) ** 2), 1 - r2_score(ctr, ret)
        else:
            # control -> condition -> control' -> condition'
            gene_return = torch.Tensor(gene_return).clone().detach()
            gene_condition_return, _, _ = self.predict(gene_return, df_to, \
                                                       return_anndata=False, sample=False)
            gene_condition = gene_condition.numpy()
            ctr = np.mean(gene_condition, axis=0)
            ret = np.mean(gene_condition_return, axis=0)
            return np.mean((gene_condition - gene_condition_return) ** 2), \
                   1 - r2_score(ctr, ret)

    def print_complete_cycle_uncertainty(
            self,
            datasets,
            datasets_ctr,
            ncells_max=1000,
            split_list=['test', 'ood'],
            direction='forward'
    ):
        uncert = pd.DataFrame(columns=[self.covars_key,
                                       self.perturbation_key,
                                       self.dose_key, 'split', 'MSE', '1-R2'])

        ctr_covar, ctrl_name, ctr_dose = datasets_ctr.pert_categories[0].split('_')
        df_ctrl = pd.DataFrame({self.perturbation_key: [ctrl_name],
                                self.dose_key: [ctr_dose],
                                self.covars_key: [ctr_covar]})

        i = 0
        for split in split_list:
            dataset = datasets[split]
            print(split)
            for pert_cat in np.unique(dataset.pert_categories):
                idx = np.where(dataset.pert_categories == pert_cat)[0]
                genes = dataset.genes[idx, :]

                covar, pert, dose = pert_cat.split('_')
                df_cond = pd.DataFrame({self.perturbation_key: [pert],
                                        self.dose_key: [dose],
                                        self.covars_key: [covar]})

                if direction == 'back':
                    # condition -> control -> condition
                    uncert.loc[i] = [covar, pert, dose, split] + \
                                    list(self.get_cycle_uncertainty(genes, df_cond, \
                                                                    df_ctrl, ncells_max=ncells_max))
                else:
                    # control -> condition -> control
                    uncert.loc[i] = [covar, pert, dose, split] + \
                                    list(self.get_cycle_uncertainty(datasets_ctr.genes, \
                                                                    df_ctrl, df_cond, ncells_max=ncells_max, \
                                                                    direction=direction))

                i += 1

        return uncert

    def evaluate_r2(
            self,
            dataset,
            genes_control
    ):
        """
        Measures different quality metrics about an ComPert `autoencoder`, when
        tasked to translate some `genes_control` into each of the drug/cell_type
        combinations described in `dataset`.

        Considered metrics are R2 score about means and variances for all genes, as
        well as R2 score about means and variances about differentially expressed
        (_de) genes.
        """
        scores = pd.DataFrame(columns=[self.covars_key,
                                       self.perturbation_key,
                                       self.dose_key,
                                       'R2_mean', 'R2_mean_DE', 'R2_var',
                                       'R2_var_DE', 'num_cells'])

        num, dim = genes_control.size(0), genes_control.size(1)

        total_cells = len(dataset)

        icond = 0
        for pert_category in np.unique(dataset.pert_categories):
            # pert_category category contains: 'celltype_perturbation_dose' info
            de_idx = np.where(
                dataset.var_names.isin(
                    np.array(dataset.de_genes[pert_category])))[0]

            idx = np.where(dataset.pert_categories == pert_category)[0]

            if len(idx) > 0:
                emb_drugs = dataset.drugs[idx][0].view(
                    1, -1).repeat(num, 1).clone()
                emb_cts = dataset.cell_types[idx][0].view(
                    1, -1).repeat(num, 1).clone()

                genes_predict = self.model.predict(
                    genes_control, emb_drugs, emb_cts).detach().cpu()

                mean_predict = genes_predict[:, :dim]
                var_predict = genes_predict[:, dim:]

                # estimate metrics only for reasonably-sized drug/cell-type combos

                y_true = dataset.genes[idx, :].numpy()

                # true means and variances
                yt_m = y_true.mean(axis=0)
                yt_v = y_true.var(axis=0)
                # predicted means and variances
                yp_m = mean_predict.mean(0)
                yp_v = var_predict.mean(0)

                mean_score = r2_score(yt_m, yp_m)
                var_score = r2_score(yt_v, yp_v)

                mean_score_de = r2_score(yt_m[de_idx], yp_m[de_idx])
                var_score_de = r2_score(yt_v[de_idx], yp_v[de_idx])
                scores.loc[icond] = pert_category.split('_') + \
                                    [mean_score, mean_score_de, var_score, var_score_de, len(idx)]
                icond += 1

        return scores


def get_reference_from_combo(
        perturbations_list,
        datasets,
        splits=['training', 'ood']
):
    """
        A simple function that produces a pd.DataFrame of individual
        drugs-doses combinations used among the splits (for a fixed covariate).
    """
    df_list = []
    for split_name in splits:
        full_dataset = datasets[split_name]
        ref = {'num_cells': []}
        for pp in perturbations_list:
            ref[pp] = []

        ndrugs = len(perturbations_list)
        for pert_cat in np.unique(full_dataset.pert_categories):
            _, pert, dose = pert_cat.split('_')
            pert_list = pert.split('+')
            if set(pert_list) == set(perturbations_list):
                dose_list = dose.split('+')
                ncells = len(full_dataset.pert_categories[
                                 full_dataset.pert_categories == pert_cat])
                for j in range(ndrugs):
                    ref[pert_list[j]].append(float(dose_list[j]))
                ref['num_cells'].append(ncells)
                print(pert, dose, ncells)
        df = pd.DataFrame.from_dict(ref)
        df['split'] = split_name
        df_list.append(df)

    return pd.concat(df_list)


def linear_interp(y1, y2, x1, x2, x):
    a = (y1 - y2) / (x1 - x2)
    b = y1 - a * x1
    y = a * x + b
    return y


def evaluate_r2_benchmark(
        compert_api,
        datasets,
        pert_category,
        pert_category_list
):
    scores = pd.DataFrame(columns=[compert_api.covars_key,
                                   compert_api.perturbation_key,
                                   compert_api.dose_key,
                                   'R2_mean', 'R2_mean_DE',
                                   'R2_var', 'R2_var_DE',
                                   'num_cells', 'benchmark', 'method'])

    de_idx = np.where(
        datasets['ood'].var_names.isin(
            np.array(datasets['ood'].de_genes[pert_category])))[0]
    idx = np.where(datasets['ood'].pert_categories == pert_category)[0]
    y_true = datasets['ood'].genes[idx, :].numpy()
    # true means and variances
    yt_m = y_true.mean(axis=0)
    yt_v = y_true.var(axis=0)

    icond = 0
    if len(idx) > 0:
        for pert_category_predict in pert_category_list:
            if '+' in pert_category_predict:
                pert1, pert2 = pert_category_predict.split('+')
                idx_pred1 = np.where(datasets['training'].pert_categories == \
                                     pert1)[0]
                idx_pred2 = np.where(datasets['training'].pert_categories == \
                                     pert2)[0]

                y_pred1 = datasets['training'].genes[idx_pred1, :].numpy()
                y_pred2 = datasets['training'].genes[idx_pred2, :].numpy()

                x1 = float(pert1.split('_')[2])
                x2 = float(pert2.split('_')[2])
                x = float(pert_category.split('_')[2])
                yp_m1 = y_pred1.mean(axis=0)
                yp_m2 = y_pred2.mean(axis=0)
                yp_v1 = y_pred1.var(axis=0)
                yp_v2 = y_pred2.var(axis=0)

                yp_m = linear_interp(yp_m1, yp_m2, x1, x2, x)
                yp_v = linear_interp(yp_v1, yp_v2, x1, x2, x)

            #                     yp_m = (y_pred1.mean(axis=0) + y_pred2.mean(axis=0))/2
            #                     yp_v = (y_pred1.var(axis=0) + y_pred2.var(axis=0))/2

            else:
                idx_pred = np.where(datasets['training'].pert_categories == \
                                    pert_category_predict)[0]
                print(pert_category_predict, len(idx_pred))
                y_pred = datasets['training'].genes[idx_pred, :].numpy()
                # predicted means and variances
                yp_m = y_pred.mean(axis=0)
                yp_v = y_pred.var(axis=0)

            mean_score = r2_score(yt_m, yp_m)
            var_score = r2_score(yt_v, yp_v)

            mean_score_de = r2_score(yt_m[de_idx], yp_m[de_idx])
            var_score_de = r2_score(yt_v[de_idx], yp_v[de_idx])
            scores.loc[icond] = pert_category.split('_') + \
                                [mean_score, mean_score_de, var_score, var_score_de, \
                                 len(idx), pert_category_predict, 'benchmark']
            icond += 1

    return scores