"""evaluator.py is a collection of functions for making and testing predictions about expression fold change after genetic perturbations.
It dedicates particular attention to interfacing with CellOracle, a thought-provoking and flexible perturbation prediction method.
"""
from joblib import Parallel, delayed, cpu_count
import numpy as np
import pandas as pd
import anndata
from scipy.stats import spearmanr as spearmanr
from scipy.stats import rankdata as rank
import os 
import sys
import gc
try:
    import gseapy
except:
    print("GSEAPY is unavailable; will skip some enrichment results.")
import altair as alt
from contextlib import redirect_stdout

def makeMainPlots(
    evaluationPerPert: pd.DataFrame, 
    evaluationPerTarget: pd.DataFrame, 
    outputs: str, 
    factor_varied:str, 
    facet_by: str = None, 
    color_by: str = None, 
    metrics = [
        'spearman', 'mse', 'mae', 'mae_benefit',
        "mse_top_20", "mse_top_100", "mse_top_200",
    ]
    ):
    """Redo the main plots summarizing an experiment.
    Args:
        evaluationPerPert (pd.DataFrame)
        evaluationPerTarget (pd.DataFrame)
        factor_varied (str): Plots are automatically colored based on this column of "evaluationPerPert". 
        facet_by (str): Plots are automatically stratified based on this column of "evaluationPerPert". 
        outputs (str): folder to save plots in
        metrics: How to measure performance. 
    """
    # Sometimes the index is too complex for Altair to handle correctly (tuples)
    evaluationPerPert = evaluationPerPert.copy()
    try:
        evaluationPerPert.index = [p[1] for p in evaluationPerPert.index]
    except IndexError:
        pass
    vlnplot = {}
    _ = alt.data_transformers.disable_max_rows()
    if color_by is not None:
        evaluationPerPert[factor_varied + " "] = [str(a) + str(b) for a,b in zip(evaluationPerPert[factor_varied], evaluationPerPert[color_by])]
        group_mean_by = [factor_varied + " "]
    else:
        group_mean_by = [factor_varied]
    if facet_by is not None:
        group_mean_by.append(facet_by)
    for metric in metrics:
        means = evaluationPerPert.groupby(group_mean_by, as_index=False)[[metric]].mean()
        vlnplot[metric] = alt.Chart(
                data = evaluationPerPert, 
                title = f"{metric} (predicted log fold change vs observed)"
            ).mark_boxplot(extent='min-max')
        # Faceting fights with layering, so skip the means if faceting.
        if facet_by is None:
            vlnplot[metric] = vlnplot[metric] + alt.Chart(data = means).mark_point(color="black")
        if color_by is not None:
            vlnplot[metric]=vlnplot[metric].encode(
                y=alt.Y(f'{metric}:Q'),
                color=color_by + ':N',
                x=alt.X(
                    factor_varied + " " + ':N'
                )
            ).properties(
                width=400,
                height=400
            )
        else:
            vlnplot[metric] = vlnplot[metric].encode(
                y=alt.Y(f'{metric}:Q'),
                x=alt.X(
                    factor_varied + ':N'
                )
            ).properties(
                width=400,
                height=400
            )
        if facet_by is not None:
            vlnplot[metric] = vlnplot[metric].facet(
                facet_by + ':N',
                columns=int(np.ceil(np.sqrt(len(evaluationPerPert[facet_by].unique())))), 
            )
        try:
            vlnplot[metric].save(f'{outputs}/{metric}.svg')
        except Exception as e:
            print(f"Got error {repr(e)} during svg saving; trying instead with html and interactive html.", flush = True)
            vlnplot[metric].save(f'{outputs}/{metric}.html')    
    return vlnplot

def addGeneMetadata(df: pd.DataFrame, 
                    adata: anndata.AnnData,
                    adata_test: anndata.AnnData,
                    genes_considered_as: str) -> pd.DataFrame:
    """Add metadata related to evo conservation and network connectivity


    Args:
        df (pd.DataFrame): Gene names and associated performance metrics
        adata (anndata.AnnData): training expression data
        adata_test (anndata.AnnData): test-set expression data
        genes_considered_as (str): "targets" or "perturbations"

    Returns:
        pd.DataFrame: df with additional columns describing evo conservation and network connectivity
    """
    # Measures derived from the test data, e.g. effect size
    if genes_considered_as == "perturbations":
        perturbation_characteristics = [
        'fraction_missing',
        'logFC',
        'spearmanCorr',
        'pearsonCorr',
        'logFCNorm2',
        ]
        perturbation_characteristics_available = []
        for x in perturbation_characteristics:
            if x not in df.columns: 
                if x in adata_test.obs.columns:
                    perturbation_characteristics_available.append(x)
                    # If this column is duplicated, it causes problems: pert_x pert_y etc. 
                    df = df[[c for c in df.columns if c != "perturbation"]]
                    df = pd.merge(
                        adata_test.obs.loc[:,["perturbation", x]],
                        df.copy(),
                        how = "outer", # Will deal with missing info later
                        left_on="perturbation", 
                        right_on="gene")
            else:
                perturbation_characteristics_available.append(x)
        perturbation_characteristics = perturbation_characteristics_available
        

    # Measures derived from the training data, e.g. overdispersion
    expression_characteristics = [
        'highly_variable', 'highly_variable_rank', 'means',
        'variances', 'variances_norm'
    ]
    expression_characteristics = [e for e in expression_characteristics if e in adata.var.columns]
    if any(not x in df.columns for x in expression_characteristics):
        df = pd.merge(
            adata.var[expression_characteristics],
            df.copy(),
            how = "outer", # This yields missing values. Will deal with that later
            left_index=True, 
            right_on="gene")
    # Proteoform diversity information is not yet used because it would be hard to summarize this into a numeric measure of complexity.
    # But this code may be useful if we aim to continue that work later on.
    proteoform_diversity = pd.read_csv("../accessory_data/uniprot-compressed_true_download_true_fields_accession_2Cid_2Cprotei-2023.02.02-15.27.12.44.tsv.gz", sep = "\t")
    proteoform_diversity.head()
    proteoform_diversity_summary = pd.DataFrame(
        {
            "is_glycosylated": ~proteoform_diversity["Glycosylation"].isnull(),
            "has_ptm": ~proteoform_diversity["Post-translational modification"].isnull(),
        },
        index = proteoform_diversity.index,
    )
    proteoform_diversity_characteristics = proteoform_diversity_summary.columns.copy()

    # measures of evolutionary constraint 
    evolutionary_characteristics = ["pLI"]
    evolutionary_constraint = pd.read_csv("../accessory_data/forweb_cleaned_exac_r03_march16_z_data_pLI_CNV-final.txt.gz", sep = "\t")
    evolutionary_constraint = evolutionary_constraint.groupby("gene").agg(func = max)
    if any(not x in df.columns for x in evolutionary_characteristics):
        df = pd.merge(
            evolutionary_constraint,
            df.copy(),
            how = "outer", # This yields missing values. Will deal with that later
            left_on="gene", 
            right_on="gene")
    
    # measures of connectedness
    degree = pd.read_csv("../accessory_data/degree_info.csv.gz")
    degree = degree.rename({"Unnamed: 0":"gene"}, axis = 1)
    degree["gene"] = [str(g).upper() for g in degree["gene"]]
    degree = degree.pivot_table(
        index=['gene'], 
        values=['in-degree', 'out-degree'], 
        columns=['network']
    )
    degree.fillna(0)
    degree.columns = ['_'.join(col) for col in degree.columns.values]
    degree_characteristics = list(degree.columns)
    if any(not x in df.columns for x in degree_characteristics):
        df = pd.merge(
            degree,
            df.copy(),
            how = "outer", # This yields missing values. Will deal with that later
            left_on="gene", 
            right_on="gene")
    try:
        df.reset_index(inplace=True)
    except:
        pass
    types_of_gene_data = {
        "evolutionary_characteristics":evolutionary_characteristics,
        "expression_characteristics": expression_characteristics, 
        "degree_characteristics": degree_characteristics,
    }
    if genes_considered_as == "perturbations":
        types_of_gene_data["perturbation_characteristics"] = perturbation_characteristics
    
    # Remove missing values from outer joins.
    # These are genes where we have various annotations, but they are not actually
    # perturbed or not actually measured on the test set.
    df = df.loc[df["mae_benefit"].notnull(), :]
    return df, types_of_gene_data

def studyPredictableGenes(evaluationPerTarget: pd.DataFrame, 
                          train_data: anndata.AnnData, 
                          test_data: anndata.AnnData, 
                          save_path: str, 
                          factor_varied: str, 
                          genes_considered_as: str) -> pd.DataFrame:
    """Plot various factors against our per-gene measure of predictability 

    Args:
        evaluationPerTarget (pd.DataFrame):  Gene names and associated performance metrics
        train_data (anndata.AnnData): training expression data
        test_data (anndata.AnnData): test-set expression data
        save_path (str): where to save the plots
        factor_varied (str): what to use as the x axis in the plots
        genes_considered_as (str): "targets" or "perturbations"

    Returns:
        pd.DataFrame: evaluation results
    """
    evaluationPerTarget, types_of_gene_data = addGeneMetadata(evaluationPerTarget, train_data, test_data, genes_considered_as)
    types_of_gene_data["out-degree"] = [s for s in types_of_gene_data["degree_characteristics"] if "out-degree" in s]
    types_of_gene_data["in-degree"]  = [s for s in types_of_gene_data["degree_characteristics"] if "in-degree" in s]
    for t in types_of_gene_data.keys():
        if len(types_of_gene_data[t])==0:
            continue
        print(f"Plotting prediction error by {t}")
        long_data = pd.melt(
            evaluationPerTarget, 
            id_vars=[factor_varied, "mae_benefit"], 
            value_vars=types_of_gene_data[t], 
            var_name='property_of_gene', 
            value_name='value', 
            col_level=None, 
            ignore_index=True)
        long_data["value"] = [float(x) for x in long_data["value"]]
        long_data = long_data.loc[long_data["value"].notnull(), :]
        long_data = long_data.loc[long_data["mae_benefit"].notnull(), :]
        if long_data.shape[0]==0:
            print(f"No genes have info on {t}. Skipping.")
            continue
        long_data["value_binned"] = long_data.groupby(["property_of_gene", factor_varied])[["value"]].transform(lambda x: pd.cut(rank(x), bins=5))
        long_data = long_data.groupby(["property_of_gene", factor_varied, "value_binned"]).agg('median').reset_index()
        del long_data["value_binned"]
        chart = alt.Chart(long_data).mark_point().encode(
            x = "value:Q",
            y = "mae_benefit:Q",
            color=alt.Color(factor_varied, scale=alt.Scale(scheme='category20')),
        )
        chart = chart + chart.mark_line()
        chart = chart.facet(
            "property_of_gene", 
            columns= 5,
        ).resolve_scale(
            x='independent'        
        )
        _ = alt.data_transformers.disable_max_rows()
        os.makedirs(os.path.join(save_path, genes_considered_as), exist_ok=True)
        try:
            chart.save(os.path.join(save_path, genes_considered_as, f"predictability_vs_{t}.svg"))
        except Exception as e:
            chart.save(os.path.join(save_path, genes_considered_as, f"predictability_vs_{t}.html"))
            print(f"Exception when saving predictability versus {t}: {repr(e)}. Is the chart empty?")

    # How many genes are we just predicting a constant for?
    if genes_considered_as == "targets":
        cutoff = 0.01
        for condition in evaluationPerTarget[factor_varied].unique():
            subset = evaluationPerTarget.loc[evaluationPerTarget[factor_varied]==condition]
            n_constant = (subset["standard_deviation"] < cutoff).sum()
            n_total = subset.shape[0]  
            chart = alt.Chart(subset).mark_bar().encode(
                    x=alt.X("standard_deviation:Q", bin=alt.BinParams(maxbins=30), scale=alt.Scale(type="sqrt")),
                    y=alt.Y('count()'),
                ).properties(
                    title=f'Standard deviation of predictions ({n_constant}/{n_total} are within {cutoff} of 0)'
                )
            _ = alt.data_transformers.disable_max_rows()
            os.makedirs(os.path.join(save_path, genes_considered_as, "variety_in_predictions", f"{condition}"), exist_ok=True)
            try:
                chart.save( os.path.join(save_path, genes_considered_as, "variety_in_predictions", f"{condition}.svg"))
            except Exception as e:
                print(f"Saving svg failed with error {repr(e)}. Trying html, which may produce BIG-ASS files.")
                chart.save( os.path.join(save_path, genes_considered_as, "variety_in_predictions", f"{condition}.html"))

    # Gene set enrichments on best-predicted genes
    for condition in evaluationPerTarget[factor_varied].unique():
        os.makedirs(os.path.join(save_path, genes_considered_as, "enrichr_on_best", str(condition)), exist_ok=True)
        gl = evaluationPerTarget.loc[evaluationPerTarget[factor_varied]==condition]
        gl = list(gl.sort_values("mae_benefit", ascending=False).head(50)["gene"].unique())
        pd.DataFrame(gl).to_csv(os.path.join(save_path, genes_considered_as, "enrichr_on_best", str(condition), "input_genes.txt"), index = False,  header=False)
        for gene_sets in ['GO Molecular Function 2021', 'GO Biological Process 2021', 'Jensen TISSUES', 'ARCHS4 Tissues', 'Chromosome Location hg19']:
            try:
                _ = gseapy.enrichr(
                    gene_list=gl,
                    gene_sets=gene_sets.replace(" ", "_"), 
                    outdir=os.path.join(save_path, genes_considered_as, "enrichr_on_best", str(condition), f"{gene_sets}"), 
                    format='svg',
                )
            except Exception as e:
                print(f"While running enrichr via gseapy, encountered error {repr(e)}.")
                pass
    evaluationPerTarget = evaluationPerTarget.loc[evaluationPerTarget["gene"].notnull(), :]
    return evaluationPerTarget

def plotOneTargetGene(gene: str, 
                      outputs: str, 
                      conditions: pd.DataFrame, 
                      factor_varied: str,
                      train_data: anndata.AnnData, 
                      heldout_data: anndata.AnnData, 
                      fitted_values: anndata.AnnData, 
                      predictions: anndata.AnnData) -> None:
    """For one gene, plot predicted + observed logfc for train + test.

    Args:
        gene (str): gene name (usually the HGNC symbol)
        outputs (str): where to save the plots
        conditions (pd.DataFrame): Metadata from conditions.csv
        factor_varied (str): what to use as the x axis in the plot
        train_data (anndata.AnnData): training expression
        heldout_data (anndata.AnnData): test-set expression
        fitted_values (anndata.AnnData): predictions about perturbations in the training set
        predictions (anndata.AnnData): predictions about perturbations in the test set
    """
    expression = {
        e:pd.DataFrame({
            "index": [i for i in range(
                fitted_values[e][:,gene].shape[0] + 
                predictions[e][:,gene].shape[0]
            )],
            "experiment": e,
            "observed": np.concatenate([
                safe_squeeze(train_data[e][:,gene].X), 
                safe_squeeze(heldout_data[e][:,gene].X), 
            ]), 
            "predicted": np.concatenate([
                safe_squeeze(fitted_values[e][:,gene].X), 
                safe_squeeze(predictions[e][:,gene].X), 
            ]), 
            "is_trainset": np.concatenate([
                np.ones (fitted_values[e][:,gene].shape[0]), 
                np.zeros(  predictions[e][:,gene].shape[0]), 
            ]), 
        }) for e in predictions.keys() 
    }
    expression = pd.concat(expression)
    expression = expression.reset_index()
    expression = expression.merge(conditions, left_on="experiment", right_index=True)
    os.makedirs(os.path.join(outputs), exist_ok=True)
    alt.Chart(data=expression).mark_point().encode(
        x = "observed:Q",y = "predicted:Q", color = "is_trainset:N"
    ).properties(
        title=gene
    ).facet(
        facet = factor_varied, 
        columns=3,
    ).save(os.path.join(outputs, gene + ".svg"))
    return   

def postprocessEvaluations(evaluations: pd.DataFrame, 
                           conditions: pd.DataFrame)-> pd.DataFrame:
    """Compare MAE for each observation to that of a a user-specified baseline method.

    Args:
        evaluations (pd.DataFrame): evaluation results for each test-set observation
        conditions (pd.DataFrame): metadata from conditions.csv

    Returns:
        pd.DataFrame: evaluation results with additional columns 'mae_baseline' and 'mae_benefit'
    """
    evaluations   = pd.concat(evaluations)
    evaluations   = evaluations.merge(conditions,   how = "left", right_index = True, left_on = "index")
    evaluations   = pd.DataFrame(evaluations.to_dict())
    # Add some info on each evaluation-per-target, such as the baseline MAE
    evaluations["target"] = [i[1] for i in evaluations.index]
    baseline_conditions = set(evaluations["baseline_condition"].unique())
    is_baseline = [i in baseline_conditions for i in evaluations["condition"]]
    evaluations["mae_baseline"] = np.NaN
    evaluations.loc[is_baseline, "mae_baseline"] = evaluations.loc[is_baseline, "mae"]
    def fetch_baseline_mae(x):
        try:
            return x.loc[x["condition"] == x["baseline_condition"], "mae"].values[0]
        except:
            return np.NaN
    for target in evaluations["target"].unique():
        idx = evaluations.index[target == evaluations["target"]]
        evaluations.loc[idx, "mae_baseline"] = fetch_baseline_mae(evaluations.loc[idx,:])
    evaluations["mae_benefit"] = evaluations["mae_baseline"] - evaluations["mae"]
    # Fix a bug with parquet not handling mixed-type columns
    evaluations = evaluations.astype({'mae': float, 'mae_baseline': float, 'mae_benefit': float})

    evaluations = evaluations.sort_values("mae_benefit", ascending=False)
    # Sometimes these are processed by the same code downstream and it's convenient to have a "gene" column.
    try:
        evaluations["gene"] = evaluations["target"]
    except KeyError:
        pass
    try:
        evaluations["gene"] = evaluations["perturbation"]
    except KeyError:
        pass

    return evaluations

def evaluateCausalModel(
    get_current_data_split:callable, 
    predicted_expression: dict,
    is_test_set: bool,
    conditions: pd.DataFrame, 
    outputs: str, 
    classifier = None, 
    do_scatterplots = True):
    """Compile plots and tables comparing heldout data and predictions for same. 

    Args:
        get_current_data_split: function to retrieve tuple of anndatas (train, test)
        predicted_expression: dict with keys equal to the index in "conditions" and values being anndata objects. 
        is_test_set: True if the predicted_expression is on the test set and False if predicted_expression is on the training data.
        classifier (sklearn.LogisticRegression): Optional, to judge results on cell type accuracy. 
        conditions (pd.DataFrame): Metadata for the different combinations used in this experiment. 
        outputs (String): Saves output here.
    """
    evaluationPerPert = {}
    evaluationPerTarget = {}

    evaluations  = []
    for i in predicted_expression.keys():
        perturbed_expression_data_train_i, perturbed_expression_data_heldout_i = get_current_data_split(i)
        evaluations.append(
            evaluateOnePrediction(
                expression = perturbed_expression_data_heldout_i if is_test_set else perturbed_expression_data_train_i,
                predictedExpression = predicted_expression[i],
                baseline = perturbed_expression_data_train_i[[bool(b) for b in perturbed_expression_data_train_i.obs["is_control"]], :],
                doPlots=do_scatterplots,
                outputs = outputs,
                experiment_name = i,
                classifier=classifier,        
            )
        )
    # That parallel code returns a list of tuples. I want a pair of dicts instead. 
    for i,condition in enumerate(predicted_expression.keys()):
        evaluationPerPert[condition], evaluationPerTarget[condition] = evaluations[i]
        evaluationPerPert[condition]["index"]   = condition
        evaluationPerTarget[condition]["index"] = condition
    del evaluations
    # Concatenate and add some extra info
    evaluationPerPert = postprocessEvaluations(evaluationPerPert, conditions)
    evaluationPerTarget = postprocessEvaluations(evaluationPerTarget, conditions)
    return evaluationPerPert, evaluationPerTarget

def safe_squeeze(X):
    """Squeeze a matrix when you don't know if it's sparse-format or not.

    Args:
        X (np.matrix or scipy.sparse.csr_matrix): _description_

    Returns:
        np.array: 1-d version of the input
    """
    try:
        X = X.toarray().squeeze()
    except:
        X = X.squeeze()
    return X

def evaluate_per_target(i: int, target: str, expression, predictedExpression):
    """Evaluate performance on a single target gene.

    Args:
        i (int): index of target gene to check
        target (str): name of target gene
        expression (np or scipy matrix): true expression or logfc
        predictedExpression (np or scipy matrix): predicted expression or logfc

    Returns:
        tuple: target, std_dev, mae, mse where target is the gene name, std_dev is the standard deviation of the 
            predictions (to check if they are constant), and mae and mse are mean absolute or squared error
    """
    observed  = safe_squeeze(expression[:, i])
    predicted = safe_squeeze(predictedExpression[:, i])
    std_dev = np.std(predicted)
    mae = np.abs(observed - predicted).sum().copy()
    mse = np.linalg.norm(observed - predicted) ** 2
    return target, std_dev, mae, mse

def evaluate_across_targets(expression: anndata.AnnData, predictedExpression: anndata.AnnData) -> pd.DataFrame:
    """Evaluate performance for each target gene.

    Args:
        expression (anndata.AnnData): actual expression or logfc
        predictedExpression (anndata.AnnData): predicted expression or logfc

    Returns:
        pd.DataFrame: _description_
    """
    targets = predictedExpression.var.index
    predictedExpression = predictedExpression.to_memory()
    results = Parallel(n_jobs=cpu_count()-1)(delayed(evaluate_per_target)(i, target, expression.X, predictedExpression.X) for i,target in enumerate(targets))
    metrics_per_target = pd.DataFrame(results, columns=["target", "standard_deviation", "mae", "mse"]).set_index("target")
    return metrics_per_target

def evaluate_per_pert(i: int, 
                      pert: str,
                      expression: anndata.AnnData, 
                      predictedExpression: anndata.AnnData,
                      baseline: anndata.AnnData, 
                      classifier=None) -> pd.DataFrame:
    """Calculate evaluation metrics for one perturbations. 

    Args:
        i (int): index of the perturbation to be examined
        pert (str): name(s) of perturbed gene(s)
        expression (anndata.AnnData): actual expression, log1p-scale
        predictedExpression (anndata.AnnData): predicted expression, log1p-scale
        baseline (anndata.AnnData): baseline expression, log1p-scale
        classifier (optional): Optional sklearn classifier to judge results by cell type label accuracy

    Returns:
        pd.DataFrame: Evaluation results for each perturbation
    """
    predicted = safe_squeeze(predictedExpression[i, :])
    observed = safe_squeeze(expression[i, :])
    def is_constant(x):
        return np.std(x) < 1e-12
    if type(predicted) is float and np.isnan(predicted) or is_constant(predicted - baseline) or is_constant(observed - baseline):
        return pert, [0, 1, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan]
    else:
        spearman, spearmanp = [x for x in spearmanr(observed - baseline, predicted - baseline)]
        mse = np.linalg.norm(observed - predicted)**2
        mae = np.abs(observed - predicted).mean()
        proportion_correct_direction = np.mean((observed >= 0) == (predicted >= 0))
        cell_fate_correct = np.nan
        if classifier is not None:
            class_observed = classifier.predict(np.reshape(observed, (1, -1)))[0]
            class_predicted = classifier.predict(np.reshape(predicted, (1, -1)))[0]
            cell_fate_correct = 1.0 * (class_observed == class_predicted)
        # Some GEARS metrics from their extended data figure 1
        def mse_top_n(n, p=predicted, o=observed):
            top_n = rank(-np.abs(o)) <= n
            return np.linalg.norm(o[top_n] - p[top_n]) ** 2
        mse_top_20  = mse_top_n(n=20)
        mse_top_100 = mse_top_n(n=100)
        mse_top_200 = mse_top_n(n=200)
        return pert, [spearman, spearmanp, cell_fate_correct, 
                        mse_top_20, mse_top_100, mse_top_200,
                        mse, mae, proportion_correct_direction]

def evaluate_across_perts(expression: anndata.AnnData, 
                          predictedExpression: anndata.AnnData, 
                          baseline: anndata.AnnData, 
                          experiment_name: str, 
                          classifier=None, 
                          do_careful_checks: bool=False) -> pd.DataFrame:
    """Evaluate performance for each perturbation.

    Args:
        expression (anndata.AnnData): actual expression, log1p-scale
        predictedExpression (anndata.AnnData): predicted expression, log1p-scale
        baseline (anndata.AnnData): baseline expression, log1p-scale
        experiment_name (str): name of the experiment
        classifier (optional): Optional sklearn classifier to judge results by cell type label instead of logfc
        do_careful_checks (bool, optional): ensure that perturbation and dose match between observed
            and predicted expression. Defaults to False.

    Returns:
        pd.DataFrame: _description_
    """
    perts = predictedExpression.obs.index
    predictedExpression = predictedExpression.to_memory()
    if do_careful_checks:
        elap = "expression_level_after_perturbation"
        predictedExpression.obs[elap] = pd.to_numeric(predictedExpression.obs[elap], errors='coerce')
        expression.obs[elap] = pd.to_numeric(expression.obs[elap], errors='coerce')
        expression.obs[         "perturbation"] = expression.obs[         "perturbation"].astype(str)
        predictedExpression.obs["perturbation"] = predictedExpression.obs["perturbation"].astype(str)
        if not all(
            expression.obs.loc         [:, ["perturbation", elap]].fillna(0) == 
            predictedExpression.obs.loc[:, ["perturbation", elap]].fillna(0)
        ):
            raise ValueError(f"Expression and predicted expression are different sizes or are differently named in experiment {experiment_name}.")
    results = Parallel(n_jobs=cpu_count())(
        delayed(evaluate_per_pert)(i, pert, expression.X, predictedExpression.X, baseline, classifier) 
        for i,pert in enumerate(perts)
    )
    metrics_per_pert = pd.DataFrame(results, columns=["pert", "metrics"]).set_index("pert")
    metrics_per_pert = pd.DataFrame(metrics_per_pert["metrics"].tolist(), index=metrics_per_pert.index, columns=[
        "spearman", "spearmanp", "cell_fate_correct", 
        "mse_top_20", "mse_top_100", "mse_top_200",
        "mse", "mae", "proportion_correct_direction"
    ])
    return metrics_per_pert

def evaluateOnePrediction(
    expression: anndata.AnnData, 
    predictedExpression: anndata.AnnData, 
    baseline: anndata.AnnData, 
    outputs,
    experiment_name: str,
    doPlots=False, 
    classifier = None, 
    do_careful_checks = True):
    '''Compare observed against predicted, for expression, fold-change, or cell type.

            Parameters:
                    expression (AnnData): 
                        the observed expression post-perturbation (log-scale in expression.X). 
                    predictedExpression (AnnData): 
                        the cellOracle prediction (log-scale). Elements of predictedExpression.X may be np.nan for 
                        missing predictions, often one gene missing from all samples or one sample missing for all genes.
                        predictedExpression.obs must contain columns "perturbation" (symbol of targeted gene) 
                        and "expression_level_after_perturbation" (e.g. 0 for knockouts). 
                    baseline (AnnData): 
                        control expression level (log-scale)
                    outputs (str): Folder to save output in
                    classifier (sklearn logistic regression classifier): 
                        optional machine learning classifier to assign cell fate. 
                        Must have a predict() method capable of taking a value from expression or predictedExpression and returning a single class label. 
                    doPlots (bool): Make a scatterplot showing observed vs predicted, one dot per gene. 
                    do_careful_checks (bool): check gene name and expression level associated with each perturbation.
                        They must match between expression and predictionExpression.
            Returns:
                    Pandas DataFrame with Spearman correlation between predicted and observed 
                    log fold change over control.
    '''
    "log fold change using Spearman correlation and (optionally) cell fate classification."""
    if not expression.X.shape == predictedExpression.X.shape:
        raise ValueError(f"expression shape is {expression.X.shape} and predictedExpression shape is {predictedExpression.X.shape} on {experiment_name}.")
    if not expression.X.shape[1] == baseline.X.shape[1]:
        raise ValueError(f"expression and baseline must have the same number of genes on experiment {experiment_name}.")
    if not len(predictedExpression.obs_names) == len(expression.obs_names):
        raise ValueError(f"expression and predictedExpression must have the same size .obs on experiment {experiment_name}.")
    if not all(predictedExpression.obs_names == expression.obs_names):
        raise ValueError(f"expression and predictedExpression must have the same indices on experiment {experiment_name}.")
    baseline = baseline.X.mean(axis=0).squeeze()
    metrics_per_target = evaluate_across_targets(expression, predictedExpression)
    metrics = evaluate_across_perts(expression, predictedExpression, baseline, experiment_name, classifier, do_careful_checks)

    print("\nMaking some example plots")
    metrics["spearman"] = metrics["spearman"].astype(float)
    hardest = metrics["spearman"].idxmin()
    easiest = metrics["spearman"].idxmax()
    perturbation_plot_path = os.path.join(outputs, "perturbations", str(experiment_name))
    for pert in metrics.index:
        is_hardest = hardest==pert
        is_easiest = easiest==pert
        if doPlots | is_hardest | is_easiest:
            observed  = safe_squeeze(expression[         pert,:].X)
            predicted = safe_squeeze(predictedExpression[pert,:].X)
            os.makedirs(perturbation_plot_path, exist_ok = True)
            diagonal = alt.Chart(
                pd.DataFrame({
                    "x":[-1, 1],
                    "y":[-1,1 ],
                })
            ).mark_line(color= 'black').encode(
                x= 'x',
                y= 'y',
            )
            scatterplot = alt.Chart(
                pd.DataFrame({
                    "Observed log fc": observed-baseline, 
                    "Predicted log fc": predicted-baseline, 
                    "Baseline expression":baseline,
                })
            ).mark_circle().encode(
                x="Observed log fc:Q",
                y="Predicted log fc:Q",
                color="Baseline expression:Q",
            ).properties(
                title=pert + " (Spearman rho="+ str(round(metrics.loc[pert,"spearman"], ndigits=2)) +")"
            ) + diagonal
            alt.data_transformers.disable_max_rows()
            pd.DataFrame().to_csv(os.path.join(perturbation_plot_path, f"{pert}.txt"))
            try:
                scatterplot.save(os.path.join(perturbation_plot_path, f"{pert}.svg"))
                if is_easiest:
                    scatterplot.save(os.path.join(perturbation_plot_path, f"_easiest({pert}).svg"))
                if is_hardest:
                    scatterplot.save(os.path.join(perturbation_plot_path, f"_hardest({pert}).svg"))
            except Exception as e:
                print(f"Altair saver failed with error {repr(e)}")
    metrics["perturbation"] = metrics.index
    return metrics, metrics_per_target
    