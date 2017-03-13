import os
from os.path import join

import numpy as np
import pandas as pd
from sacred import Experiment
from sacred.observers import MongoObserver
from sklearn.externals.joblib import Memory
from sklearn.externals.joblib import dump
from sklearn.model_selection import GridSearchCV
from sklearn.linear_model import LogisticRegressionCV, LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder

from modl.classification import make_loadings_extractor
from modl.datasets import get_data_dirs
from modl.classification import FactoredLogistic
from modl.input_data.fmri.unmask import build_design
from modl.utils.system import get_cache_dirs

predict_contrast = Experiment('predict_contrast')
observer = MongoObserver.create(db_name='amensch', collection='runs')
predict_contrast.observers.append(observer)


@predict_contrast.config
def config():
    alphas = [0.01]
    standardize = True
    scale_importance = 'sqrt'
    n_jobs = 30
    verbose = 2
    seed = 2
    max_iter = 300
    tol = 1e-7
    alpha = 1e-4
    multi_class = 'multinomial'
    fit_intercept = True
    identity = False
    refit = False
    n_components_list = [16, 64, 256]
    test_size = 0.1
    train_size = None
    n_subjects = 788
    penalty = 'l1'
    datasets = ['hcp']
    factored = True
    latent_dim_list = [100]

    hcp_unmask_contrast_dir = join(get_data_dirs()[0], 'pipeline',
                                   'unmask', 'contrast', 'hcp', '23')
    archi_unmask_contrast_dir = join(get_data_dirs()[0], 'pipeline',
                                     'unmask', 'contrast', 'archi', '30')
    datasets_dir = {'archi': archi_unmask_contrast_dir,
                    'hcp': hcp_unmask_contrast_dir}

    del hcp_unmask_contrast_dir
    del archi_unmask_contrast_dir


@predict_contrast.automain
def run(alphas,
        alpha,
        n_components_list,
        max_iter, n_jobs,
        test_size,
        train_size,
        tol,
        identity,
        fit_intercept,
        multi_class,
        n_subjects,
        scale_importance,
        standardize,
        penalty,
        datasets,
        datasets_dir,
        factored,
        latent_dim_list,
        _run,
        _seed):
    memory = Memory(cachedir=get_cache_dirs()[0], verbose=2)
    print('Fetch and split data')
    X, masker = memory.cache(build_design)(datasets,
                                           datasets_dir,
                                           n_subjects, test_size,
                                           train_size)
    print('Retrieve components')
    components_dir = join(get_data_dirs()[0], 'pipeline', 'components', 'hcp')
    components_imgs = [join(components_dir, str(this_n_components), str(alpha),
                            'components.nii.gz')
                       for this_n_components in n_components_list]

    components = masker.transform(components_imgs)

    print('Transform and fit data')
    X_train = X.loc['train']
    train_samples = len(X_train)
    sample_weight = 1 / X_train[0].groupby(
        level=['dataset', 'contrast']).transform('count')
    sample_weight /= np.min(sample_weight)
    print('sample_weight', sample_weight)

    pipeline = make_loadings_extractor(components,
                                       standardize=standardize,
                                       scale_importance=scale_importance,
                                       identity=identity,
                                       scale_bases=True,
                                       n_jobs=n_jobs,
                                       memory=memory)

    artifact_dir = join(get_data_dirs()[0], 'pipeline',
                        'contrast', 'prediction', str(_run._id))

    if not factored:
        if len(alphas) > 1 or len(latent_dim_list) > 1:
            classifier = LogisticRegressionCV(solver='saga',
                                              multi_class=multi_class,
                                              fit_intercept=fit_intercept,
                                              random_state=_seed,
                                              refit=True,
                                              tol=tol,
                                              max_iter=max_iter,
                                              n_jobs=n_jobs,
                                              penalty=penalty,
                                              cv=10,
                                              verbose=True,
                                              Cs=1. / train_samples / np.array(
                                                  alphas))
        else:
            classifier = LogisticRegression(solver='saga',
                                            multi_class=multi_class,
                                            fit_intercept=fit_intercept,
                                            random_state=_seed,
                                            tol=tol,
                                            max_iter=max_iter,
                                            n_jobs=n_jobs,
                                            penalty=penalty,
                                            verbose=True,
                                            C=1. / train_samples / alphas[0])

    else:
        classifier = FactoredLogistic(optimizer='adam',
                                      latent_dim=latent_dim_list[0],
                                      max_iter=max_iter,
                                      activation='linear',
                                      penalty=penalty,
                                      alpha=alphas[0],
                                      batch_size=200,
                                      n_jobs=n_jobs,
                                      log_dir=join(artifact_dir,
                                                   'logs'))
        if len(alphas) > 1 or len(latent_dim_list) > 1:
            classifier.set_params(n_jobs=1)
            classifier = GridSearchCV(classifier,
                                      {'alpha': alphas,
                                       'latent_dim': latent_dim_list},
                                      cv=10,
                                      refit=True,
                                      verbose=1,
                                      n_jobs=n_jobs)

    pipeline.append(('logistic_regression', classifier))
    estimator = Pipeline(pipeline, memory=memory)

    true_labels = X.index.get_level_values('contrast').values
    label_encoder = LabelEncoder()
    y = label_encoder.fit_transform(true_labels)
    y = pd.Series(index=X.index, data=y, name='label')
    y_train = y.loc['train']

    estimator.fit(X_train, y_train,
                  logistic_regression__sample_weight=sample_weight
                  )

    predicted_proba = pd.DataFrame(index=X.index,
                                   data=estimator.predict_proba(X))
    predictions = []
    datasets = []
    for dataset, group in predicted_proba.groupby(level='dataset'):
        labels = group.index.get_level_values('contrast').unique().values
        idx_labels = label_encoder.transform(labels)
        proba_subset = group.values[:, idx_labels]
        prediction = np.argmax(proba_subset, axis=1)
        prediction = idx_labels[prediction]
        predictions.append(pd.Series(prediction, index=group.index))
        datasets.append(dataset)
    predicted_y = pd.concat(predictions)
    predicted_y = predicted_y.reindex(X.index)

    predicted_labels = label_encoder.inverse_transform(predicted_y)
    prediction = pd.DataFrame({'true_label': true_labels,
                               'predicted_label': predicted_labels},
                              index=X.index)

    print('Compute score')
    train_score = np.sum(prediction.loc['train']['predicted_label']
                         == prediction.loc['train']['true_label'])
    train_score /= prediction.loc['train'].shape[0]

    _run.info['train_score'] = float(train_score)

    test_score = np.sum(prediction.loc['test']['predicted_label']
                        == prediction.loc['test']['true_label'])
    test_score /= prediction.loc['test'].shape[0]

    _run.info['test_score'] = float(test_score)

    print('Write task prediction artifacts')
    artifact_dir = join(get_data_dirs()[0], 'pipeline',
                        'contrast', 'prediction', str(_run._id))
    if not os.path.exists(artifact_dir):
        os.makedirs(artifact_dir)

    prediction.to_csv(join(artifact_dir, 'prediction.csv'))
    _run.add_artifact(join(artifact_dir, 'prediction.csv'),
                      name='prediction.csv')

    dump(label_encoder, join(artifact_dir, 'label_encoder.pkl'))
    dump(estimator, join(artifact_dir, 'estimator.pkl'))
