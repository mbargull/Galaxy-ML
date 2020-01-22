import ast
import collections
import json
import imblearn
import numpy as np
import pandas
import pickle
import re
import scipy
import sklearn
import skrebate
import sys
import time
import warnings
import xgboost

from asteval import Interpreter, make_symbol_table
from imblearn import under_sampling, over_sampling, combine
from imblearn.pipeline import Pipeline as imbPipeline
from mlxtend import regressor, classifier
from scipy.io import mmread
from sklearn import (
    cluster, compose, decomposition, ensemble, feature_extraction,
    feature_selection, gaussian_process, kernel_approximation, metrics,
    model_selection, naive_bayes, neighbors, pipeline, preprocessing,
    svm, linear_model, tree, discriminant_analysis)
from sklearn.pipeline import Pipeline
# TODO remove following imports after scikit-learn v0.22
from sklearn.experimental import enable_hist_gradient_boosting
from .externals.selene_sdk.utils import compute_score


# handle pickle white list file
WL_FILE = __import__('os').path.join(
    __import__('os').path.dirname(__file__), 'pk_whitelist.json')

N_JOBS = int(__import__('os').environ.get('GALAXY_SLOTS', 1))


__all__ = ('load_model', 'read_columns', 'feature_selector', 'get_X_y',
           'SafeEval', 'get_estimator', 'get_cv', 'balanced_accuracy_score',
           'get_scoring', 'get_search_params', 'check_def')


class _SafePickler(pickle.Unpickler, object):
    """
    Used to safely deserialize scikit-learn model objects
    Usage:
        eg.: _SafePickler.load(pickled_file_object)
    """
    def __init__(self, file):
        super(_SafePickler, self).__init__(file)
        # load global white list
        with open(WL_FILE, 'r') as f:
            self.pk_whitelist = json.load(f)

        self.bad_names = (
            'and', 'as', 'assert', 'break', 'class', 'continue',
            'def', 'del', 'elif', 'else', 'except', 'exec',
            'finally', 'for', 'from', 'global', 'if', 'import',
            'in', 'is', 'lambda', 'not', 'or', 'pass', 'print',
            'raise', 'return', 'try', 'system', 'while', 'with',
            'True', 'False', 'None', 'eval', 'execfile', '__import__',
            '__package__', '__subclasses__', '__bases__', '__globals__',
            '__code__', '__closure__', '__func__', '__self__', '__module__',
            '__dict__', '__class__', '__call__', '__get__',
            '__getattribute__', '__subclasshook__', '__new__',
            '__init__', 'func_globals', 'func_code', 'func_closure',
            'im_class', 'im_func', 'im_self', 'gi_code', 'gi_frame',
            '__asteval__', 'f_locals', '__mro__')

        # unclassified good globals
        self.good_names = [
            'copy_reg._reconstructor', '__builtin__.object',
            '__builtin__.bytearray', 'builtins.object',
            'builtins.bytearray']

        self.keras_names = [
            'keras.engine.sequential.Sequential',
            'keras.engine.sequential.Model']

        # custom module in Galaxy-ML
        self.custom_modules = [
            'keras_galaxy_models',
            'feature_selectors', 'preprocessors',
            'iraps_classifier', 'model_validations']

    # override
    def find_class(self, module, name):
        # balack list first
        if name in self.bad_names:
            raise pickle.UnpicklingError("global '%s.%s' is forbidden"
                                         % (module, name))

        # custom module in Galaxy-ML
        # compatible with models from versions before 1.0.7.0
        if module in self.custom_modules:
            return try_get_attr('galaxy_ml.' + module, name)

        # Load objects serialized in older versions
        # TODO make this deprecate
        if module.startswith('__main__.'):
            module = 'galaxy_ml.' + module[9:]
        if module.startswith('galaxy_ml.'):
            splits = module.split('.')
            if len(splits) > 2:
                module = splits[0] + '.' + splits[1]
            return try_get_attr(module, name)

        fullname = module + '.' + name
        # keras names
        keras_names = self.keras_names
        if fullname in keras_names:
            # dynamic import, suppress message:
            # "Using TensorFlow backend."
            exec("import keras")
            mod = sys.modules[module]
            return getattr(mod, name)

        # For objects from outside libraries, it's necessary to verify
        # both module and name. Currently only a blacklist checker
        # is working.
        # TODO: replace with a whitelist checker.
        good_names = self.good_names
        pk_whitelist = self.pk_whitelist
        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            if (fullname in good_names)\
                or (module.startswith(('sklearn.', 'xgboost.', 'skrebate.',
                                       'imblearn.', 'mlxtend.', 'numpy.'))
                    or module == 'numpy'):
                if fullname not in (pk_whitelist['SK_NAMES'] +
                                    pk_whitelist['SKR_NAMES'] +
                                    pk_whitelist['XGB_NAMES'] +
                                    pk_whitelist['NUMPY_NAMES'] +
                                    pk_whitelist['IMBLEARN_NAMES'] +
                                    pk_whitelist['MLXTEND_NAMES'] +
                                    keras_names +
                                    good_names):
                    # raise pickle.UnpicklingError
                    print("Warning: global %s is not in pickler whitelist "
                          "yet and will loss support soon. Contact tool "
                          "author or leave a message at github.com" % fullname)
                mod = sys.modules[module]
                return getattr(mod, name)

        raise pickle.UnpicklingError("global '%s' is forbidden" % fullname)


def load_model(file):
    """Load pickled object with `_SafePicker`
    """
    return _SafePickler(file).load()


def read_columns(f, c=None, c_option='by_index_number',
                 return_df=False, **args):
    """Return array from a tabular dataset by various columns selection

    Parameters
    ----------
    f : str or DataFrame object
        If str, file path.
    c : list of int or str
        When integers, column indices; when str, list of column names.
    c_option : str
        One of ['by_index_number', 'by_header_name',
                'all_but_by_index_number', 'all_but_by_header_name'].
    return_df : bool
        Whether to return the DataFrame object.
    args : params for `pandas.read_csv`
    """
    if isinstance(f, str):
        data = pandas.read_csv(f, **args)
    else:
        data = f
    if c_option == 'by_index_number':
        cols = list(map(lambda x: x - 1, c))
        data = data.iloc[:, cols]
    elif c_option == 'all_but_by_index_number':
        cols = list(map(lambda x: x - 1, c))
        data = data.drop(data.columns[cols], axis=1, inplace=False)
    elif c_option == 'by_header_name':
        cols = [e.strip() for e in c.split(',')]
        data = data[cols]
    elif c_option == 'all_but_by_header_name':
        cols = [e.strip() for e in c.split(',')]
        data = data.drop(cols, axis=1, inplace=False)
    y = data.values
    if return_df:
        return y, data
    else:
        return y


def feature_selector(inputs, X=None, y=None):
    """generate an instance of sklearn.feature_selection classes

    Parameters
    ----------
    inputs : dict
        From galaxy tool parameters.
    X : array
        Containing training features.
    y : array or list
        Target values.
    """
    selector = inputs['selected_algorithm']
    if selector != 'DyRFECV':
        selector = getattr(sklearn.feature_selection, selector)
    options = inputs['options']

    if inputs['selected_algorithm'] == 'SelectFromModel':
        if not options['threshold'] or options['threshold'] == 'None':
            options['threshold'] = None
        else:
            try:
                options['threshold'] = float(options['threshold'])
            except ValueError:
                pass
        if inputs['model_inputter']['input_mode'] == 'prefitted':
            model_file = inputs['model_inputter']['fitted_estimator']
            with open(model_file, 'rb') as model_handler:
                fitted_estimator = load_model(model_handler)
            new_selector = selector(fitted_estimator, prefit=True, **options)
        else:
            estimator_json = inputs['model_inputter']['estimator_selector']
            estimator = get_estimator(estimator_json)
            check_feature_importances = try_get_attr(
                'galaxy_ml.feature_selectors', 'check_feature_importances')
            estimator = check_feature_importances(estimator)
            new_selector = selector(estimator, **options)

    elif inputs['selected_algorithm'] == 'RFE':
        step = options.get('step', None)
        if step and step >= 1.0:
            options['step'] = int(step)
        estimator = get_estimator(inputs["estimator_selector"])
        check_feature_importances = try_get_attr(
            'galaxy_ml.feature_selectors', 'check_feature_importances')
        estimator = check_feature_importances(estimator)
        new_selector = selector(estimator, **options)

    elif inputs['selected_algorithm'] == 'RFECV':
        options['scoring'] = get_scoring(options['scoring'])
        options['n_jobs'] = N_JOBS
        splitter, groups = get_cv(options.pop('cv_selector'))
        if groups is None:
            options['cv'] = splitter
        else:
            options['cv'] = list(splitter.split(X, y, groups=groups))
        step = options.get('step', None)
        if step and step >= 1.0:
            options['step'] = int(step)
        estimator = get_estimator(inputs['estimator_selector'])
        check_feature_importances = try_get_attr(
            'galaxy_ml.feature_selectors', 'check_feature_importances')
        estimator = check_feature_importances(estimator)
        new_selector = selector(estimator, **options)

    elif inputs['selected_algorithm'] == 'DyRFECV':
        options['scoring'] = get_scoring(options['scoring'])
        options['n_jobs'] = N_JOBS
        splitter, groups = get_cv(options.pop('cv_selector'))
        if groups is None:
            options['cv'] = splitter
        else:
            options['cv'] = list(splitter.split(X, y, groups=groups))
        step = options.get('step')
        if not step or step == 'None':
            step = None
        else:
            step = ast.literal_eval(step)
        options['step'] = step
        estimator = get_estimator(inputs["estimator_selector"])
        check_feature_importances = try_get_attr(
            'galaxy_ml.feature_selectors', 'check_feature_importances')
        estimator = check_feature_importances(estimator)
        DyRFECV = try_get_attr('galaxy_ml.feature_selectors', 'DyRFECV')

        new_selector = DyRFECV(estimator, **options)

    elif inputs['selected_algorithm'] == 'VarianceThreshold':
        new_selector = selector(**options)

    else:
        score_func = inputs['score_func']
        score_func = getattr(sklearn.feature_selection, score_func)
        new_selector = selector(score_func, **options)

    return new_selector


def get_X_y(params, file1, file2):
    """Return machine learning inputs X, y from tabluar inputs
    """
    input_type = (params['selected_tasks']['selected_algorithms']
                  ['input_options']['selected_input'])
    if input_type == 'tabular':
        header = 'infer' if (params['selected_tasks']['selected_algorithms']
                             ['input_options']['header1']) else None
        column_option = (params['selected_tasks']['selected_algorithms']
                         ['input_options']['column_selector_options_1']
                         ['selected_column_selector_option'])
        if column_option in ['by_index_number', 'all_but_by_index_number',
                             'by_header_name', 'all_but_by_header_name']:
            c = (params['selected_tasks']['selected_algorithms']
                 ['input_options']['column_selector_options_1']['col1'])
        else:
            c = None
        X = read_columns(
            file1,
            c=c,
            c_option=column_option,
            sep='\t',
            header=header,
            parse_dates=True).astype(float)
    else:
        X = mmread(file1)

    header = 'infer' if (params['selected_tasks']['selected_algorithms']
                         ['input_options']['header2']) else None
    column_option = (params['selected_tasks']['selected_algorithms']
                     ['input_options']['column_selector_options_2']
                     ['selected_column_selector_option2'])
    if column_option in ['by_index_number', 'all_but_by_index_number',
                         'by_header_name', 'all_but_by_header_name']:
        c = (params['selected_tasks']['selected_algorithms']
             ['input_options']['column_selector_options_2']['col2'])
    else:
        c = None
    y = read_columns(
        file2,
        c=c,
        c_option=column_option,
        sep='\t',
        header=header,
        parse_dates=True)
    y = y.ravel()

    return X, y


class SafeEval(Interpreter):
    """Customized symbol table for safely literal eval

    Parameters
    ----------
    load_scipy : bool, default=False
        Whether to load globals from scipy
    load_numpy : bool, default=False
        Whether to load globals from numpy
    load_estimators : bool, default=False
        Whether to load globals for sklearn estimators
    """
    def __init__(self, load_scipy=False, load_numpy=False,
                 load_estimators=False):

        # File opening and other unneeded functions could be dropped
        unwanted = ['open', 'type', 'dir', 'id', 'str', 'repr']

        # Allowed symbol table. Add more if needed.
        new_syms = {
            'np_arange': getattr(np, 'arange'),
            'ensemble_ExtraTreesClassifier':
                getattr(ensemble, 'ExtraTreesClassifier')
        }

        syms = make_symbol_table(use_numpy=False, **new_syms)

        if load_scipy:
            scipy_distributions = scipy.stats.distributions.__dict__
            for k, v in scipy_distributions.items():
                if isinstance(v, (scipy.stats.rv_continuous,
                                  scipy.stats.rv_discrete)):
                    syms['scipy_stats_' + k] = v

        if load_numpy:
            from_numpy_random = [
                'beta', 'binomial', 'bytes', 'chisquare', 'choice',
                'dirichlet', 'division', 'exponential', 'f', 'gamma',
                'geometric', 'gumbel', 'hypergeometric', 'laplace',
                'logistic', 'lognormal', 'logseries', 'mtrand',
                'multinomial', 'multivariate_normal', 'negative_binomial',
                'noncentral_chisquare', 'noncentral_f', 'normal', 'pareto',
                'permutation', 'poisson', 'power', 'rand', 'randint',
                'randn', 'random', 'random_integers', 'random_sample',
                'ranf', 'rayleigh', 'sample', 'seed', 'set_state',
                'shuffle', 'standard_cauchy', 'standard_exponential',
                'standard_gamma', 'standard_normal', 'standard_t',
                'triangular', 'uniform', 'vonmises', 'wald', 'weibull', 'zipf']
            for f in from_numpy_random:
                syms['np_random_' + f] = getattr(np.random, f)

        if load_estimators:
            estimator_table = {
                'sklearn_svm': getattr(sklearn, 'svm'),
                'sklearn_tree': getattr(sklearn, 'tree'),
                'sklearn_ensemble': getattr(sklearn, 'ensemble'),
                'sklearn_neighbors': getattr(sklearn, 'neighbors'),
                'sklearn_naive_bayes': getattr(sklearn, 'naive_bayes'),
                'sklearn_linear_model': getattr(sklearn, 'linear_model'),
                'sklearn_cluster': getattr(sklearn, 'cluster'),
                'sklearn_decomposition': getattr(sklearn, 'decomposition'),
                'sklearn_preprocessing': getattr(sklearn, 'preprocessing'),
                'sklearn_feature_selection':
                    getattr(sklearn, 'feature_selection'),
                'sklearn_kernel_approximation':
                    getattr(sklearn, 'kernel_approximation'),
                'skrebate_ReliefF': getattr(skrebate, 'ReliefF'),
                'skrebate_SURF': getattr(skrebate, 'SURF'),
                'skrebate_SURFstar': getattr(skrebate, 'SURFstar'),
                'skrebate_MultiSURF': getattr(skrebate, 'MultiSURF'),
                'skrebate_MultiSURFstar': getattr(skrebate, 'MultiSURFstar'),
                'skrebate_TuRF': getattr(skrebate, 'TuRF'),
                'xgboost_XGBClassifier': getattr(xgboost, 'XGBClassifier'),
                'xgboost_XGBRegressor': getattr(xgboost, 'XGBRegressor'),
                'imblearn_over_sampling': getattr(imblearn, 'over_sampling'),
                'imblearn_combine': getattr(imblearn, 'combine')
            }
            syms.update(estimator_table)

        for key in unwanted:
            syms.pop(key, None)

        super(SafeEval, self).__init__(
            symtable=syms, use_numpy=False, minimal=False,
            no_if=True, no_for=True, no_while=True, no_try=True,
            no_functiondef=True, no_ifexp=True, no_listcomp=False,
            no_augassign=False, no_assert=True, no_delete=True,
            no_raise=True, no_print=True)


def get_estimator(estimator_json):
    """Return a sklearn or compatible estimator from Galaxy tool inputs
    """
    estimator_module = estimator_json['selected_module']

    if estimator_module == 'custom_estimator':
        c_estimator = estimator_json['c_estimator']
        with open(c_estimator, 'rb') as model_handler:
            new_model = load_model(model_handler)
        return new_model

    if estimator_module == "binarize_target":
        wrapped_estimator = estimator_json['wrapped_estimator']
        with open(wrapped_estimator, 'rb') as model_handler:
            wrapped_estimator = load_model(model_handler)
        options = {}
        if estimator_json['z_score'] is not None:
            options['z_score'] = estimator_json['z_score']
        if estimator_json['value'] is not None:
            options['value'] = estimator_json['value']
        options['less_is_positive'] = estimator_json['less_is_positive']
        if estimator_json['clf_or_regr'] == 'BinarizeTargetClassifier':
            klass = try_get_attr('galaxy_ml.iraps_classifier',
                                 'BinarizeTargetClassifier')
        else:
            klass = try_get_attr('galaxy_ml.iraps_classifier',
                                 'BinarizeTargetRegressor')
        return klass(wrapped_estimator, **options)

    estimator_cls = estimator_json['selected_estimator']

    if estimator_module == 'xgboost':
        klass = getattr(xgboost, estimator_cls)
    else:
        module = getattr(sklearn, estimator_module)
        klass = getattr(module, estimator_cls)

    estimator = klass()

    estimator_params = estimator_json['text_params'].strip()
    if estimator_params != '':
        try:
            safe_eval = SafeEval()
            params = safe_eval('dict(' + estimator_params + ')')
        except ValueError:
            sys.exit("Unsupported parameter input: `%s`" % estimator_params)
        estimator.set_params(**params)
    if 'n_jobs' in estimator.get_params():
        estimator.set_params(n_jobs=N_JOBS)

    return estimator


def get_cv(cv_json):
    """ Return CV splitter from Galaxy tool inputs

    Parameters
    ----------
    cv_json : dict
        From Galaxy tool inputs.
        e.g.:
            {
                'selected_cv': 'StratifiedKFold',
                'n_splits': 3,
                'shuffle': True,
                'random_state': 0
            }
    """
    cv = cv_json.pop('selected_cv')
    if cv == 'default':
        return cv_json['n_splits'], None

    groups = cv_json.pop('groups_selector', None)
    # if groups is array, return it
    if groups is not None and isinstance(groups, collections.Mapping):
        infile_g = groups['infile_g']
        header = 'infer' if groups['header_g'] else None
        column_option = (groups['column_selector_options_g']
                         ['selected_column_selector_option_g'])
        if column_option in ['by_index_number', 'all_but_by_index_number',
                             'by_header_name', 'all_but_by_header_name']:
            c = groups['column_selector_options_g']['col_g']
        else:
            c = None
        groups = read_columns(
                infile_g,
                c=c,
                c_option=column_option,
                sep='\t',
                header=header,
                parse_dates=True)
        groups = groups.ravel()

    for k, v in cv_json.items():
        if v == '':
            cv_json[k] = None

    test_fold = cv_json.get('test_fold', None)
    if test_fold:
        if test_fold.startswith('__ob__'):
            test_fold = test_fold[6:]
        if test_fold.endswith('__cb__'):
            test_fold = test_fold[:-6]
        cv_json['test_fold'] = [int(x.strip()) for x in test_fold.split(',')]

    test_size = cv_json.get('test_size', None)
    if test_size and test_size > 1.0:
        cv_json['test_size'] = int(test_size)

    if cv == 'OrderedKFold':
        cv_class = try_get_attr(
            'galaxy_ml.model_validations', 'OrderedKFold')
    elif cv == 'RepeatedOrderedKFold':
        cv_class = try_get_attr(
            'galaxy_ml.model_validations', 'RepeatedOrderedKFold')
    else:
        cv_class = getattr(model_selection, cv)
    splitter = cv_class(**cv_json)

    return splitter, groups


# needed when sklearn < v0.20
def balanced_accuracy_score(y_true, y_pred):
    """Compute balanced accuracy score, which is now available in
        scikit-learn from v0.20.0.
    """
    C = metrics.confusion_matrix(y_true, y_pred)
    with np.errstate(divide='ignore', invalid='ignore'):
        per_class = np.diag(C) / C.sum(axis=1)
    if np.any(np.isnan(per_class)):
        warnings.warn('y_pred contains classes not in y_true')
        per_class = per_class[~np.isnan(per_class)]
    score = np.mean(per_class)
    return score


def get_scoring(scoring_json):
    """Return single sklearn scorer class
        or multiple scoers in dictionary

    Returns
    -------
    single scorer instance or multiple scorers in dict
    """
    if scoring_json['primary_scoring'] == 'default':
        return None

    my_scorers = metrics.SCORERS
    my_scorers['binarize_auc_scorer'] =\
        try_get_attr('galaxy_ml.iraps_classifier',
                     'binarize_auc_scorer')
    my_scorers['binarize_average_precision_scorer'] =\
        try_get_attr('galaxy_ml.iraps_classifier',
                     'binarize_average_precision_scorer')
    if 'balanced_accuracy' not in my_scorers:
        my_scorers['balanced_accuracy'] =\
            metrics.make_scorer(balanced_accuracy_score)

    if scoring_json['secondary_scoring'] not in ('None', '')\
            and scoring_json['secondary_scoring'] !=\
            scoring_json['primary_scoring']:
        return_scoring = {}
        primary_scoring = scoring_json['primary_scoring']
        return_scoring[primary_scoring] = my_scorers[primary_scoring]
        for scorer in scoring_json['secondary_scoring'].split(','):
            if scorer != scoring_json['primary_scoring']:
                return_scoring[scorer] = my_scorers[scorer]
        return return_scoring

    return my_scorers[scoring_json['primary_scoring']]


def get_search_params(estimator):
    """Format the output of `estimator.get_params()`

    Parameters
    ----------
    estimator : python object

    Returns
    -------
    list of list, i.e., [`mark`, `param_name`, `param_value`].
    """
    res = estimator.get_params()
    SearchParam = try_get_attr('galaxy_ml.keras_galaxy_models',
                               'SearchParam')
    params = [SearchParam(k, v) for k, v in res.items()]
    params = sorted(params, key=lambda x: (x.sort_depth, x.s_param))

    results = []
    for param in params:
        # params below won't be shown for search in the searchcv tool
        # And show partial `repr` for values which are dictionary,
        # especially useful for keras models
        k, v = param.s_param, param.value
        keywords = ('n_jobs', 'pre_dispatch', 'memory', 'name', 'nthread',
                    '_path')
        exact_keywords = ('steps')
        if k.endswith(keywords) or k in exact_keywords:
            results.append(['*', k, k+": "+repr(v)])
        elif type(v) is dict:
            results.append(['@', k, k+": "+repr(v)[:100]])
        else:
            results.append(['@', k, k+": "+repr(v)])
    results.append(
        ["", "Note:",
         "@, params eligible for search in searchcv tool."])

    return results


def try_get_attr(module, name):
    """try to get attribute from a custom module

    Parameters
    ----------
    module : str
        Module name
    name : str
        Attribute (class/function) name.

    Returns
    -------
    class or function
    """
    if module.split('.')[-1] not in (
            'keras_galaxy_models', 'feature_selectors', 'preprocessors',
            'iraps_classifier', 'model_validations', 'binarize_target'):
        raise NameError("%s is not recognized as a valid custom "
                        "module in Galaxy-ML!" % module)

    mod = sys.modules.get(module, None)
    if not mod:
        exec("import %s" % module)  # might raise ImportError
        mod = sys.modules[module]

    if hasattr(mod, '__all__') and name not in mod.__all__:
        raise NameError("%s is not in __all__ of module %s"
                        % (name, module))

    return getattr(mod, name)


# Make it strict, only allow Functiondef and Classdef names
# TODO: list all importable names in custom modules
# Apply this security check to pickle whitelist checker
def check_def(mod, name):
    """ Check whether name is a defined function or class in the
    module.

    Parameters
    ----------
    mod : module name

    name : str
        Name of a class, function or variable

    Returns
    -------
    Raise NameError if the name is not a defined function or class in
    the module file.
    """
    mod_file = mod.__file__
    with open(mod_file, 'rt') as f:
        nodes = ast.parse(f.read(), filename=mod_file)
    val_names = [x.name for x in nodes.body
                 if isinstance(x, (ast.FunctionDef,
                                   ast.ClassDef))]
    if name not in val_names:
        raise NameError("%s is not a defined class or "
                        "function in module file %s"
                        % (name, mod_file))


def get_module(module):
    """ return module from a module name.

    Parameters
    ----------
    module : str
        module name
    """
    mod = sys.modules.get(module, None)
    if mod:
        return mod

    # suggest install this library manually
    if module == 'pyfaidx':
        try:
            exec('import pyfaidx')
        except ImportError:
            __import__('os').system(
                "pip install pyfaidx==0.5.5.2")
            time.sleep(10)
            try:
                exec('import pyfaidx')
            except ImportError:
                raise ImportError(
                    "module pyfaidx is not installed. "
                    "Galaxy attemped to install but failed."
                    "Please Contact Admin for manual "
                    "installation.")

        return sys.modules['pyfaidx']

    if module == 'externals.selene_sdk':
        exec('from externals import selene_sdk')
        return sys.modules['externals.selene_sdk']


def get_main_estimator(estimator):
    """return main estimator. For pipeline, main estimator's
    final estimator. For fitted SearchCV object, that's the
    best_estimator_. For stacking ensemble family estimator,
    main estimator is the meta estimator.
    """
    est_name = estimator.__class__.__name__
    # support pipeline object
    if isinstance(estimator, Pipeline):
        return get_main_estimator(estimator.steps[-1][-1])
    # support GridSearchCV/RandomSearchCV
    elif isinstance(estimator, model_selection._search.BaseSearchCV):
        return get_main_estimator(estimator.best_estimator_)
    # support stacking ensemble estimators
    # TODO support nested pipeline/stacking estimators
    elif est_name in ('StackingCVClassifier', 'StackingClassifier'):
        return get_main_estimator(estimator.meta_clf_)
    elif est_name in ('StackingCVRegressor', 'StackingRegressor'):
        return get_main_estimator(estimator.meta_regr_)
    else:
        return estimator


def clean_params(estimator, n_jobs=None):
    """clean unwanted hyperparameter settings

    If n_jobs is not None, set it into the estimator, if applicable

    Return
    ------
    Cleaned estimator object
    """
    ALLOWED_CALLBACKS = ('EarlyStopping', 'TerminateOnNaN',
                         'ReduceLROnPlateau', 'CSVLogger', 'None')

    estimator_params = estimator.get_params()

    for name, p in estimator_params.items():
        # all potential unauthorized file write
        if name == 'memory' or name.endswith('__memory') \
                or name.endswith(('_path', '_dir')):
            new_p = {name: None}
            estimator.set_params(**new_p)
        elif n_jobs is not None and (name == 'n_jobs' or
                                     name.endswith('__n_jobs')):
            new_p = {name: n_jobs}
            estimator.set_params(**new_p)
        elif name.endswith('callbacks'):
            for cb in p:
                cb_type = cb['callback_selection']['callback_type']
                if cb_type not in ALLOWED_CALLBACKS:
                    raise ValueError(
                        "Prohibited callback type: %s!" % cb_type)

    return estimator


def gen_compute_scores(y_true, pred_probas, scorer, is_multimetric=True):
    """ general score computing based on input scorers

    Parameters
    ----------
    y_true : array
        True label or target values
    pred_probas : array
        Prediction values, probability for classification problem
    scorer : dict
        dict of `sklearn.metrics.scorer.SCORER`
    is_multimetric : bool, default is True
    """
    if y_true.ndim == 1 or y_true.shape[-1] == 1:
        pred_probas = pred_probas.ravel()
        pred_labels = (pred_probas > 0.5).astype('int32')
        targets = y_true.ravel().astype('int32')
        if not is_multimetric:
            preds = pred_labels if scorer.__class__.__name__ == \
                '_PredictScorer' else pred_probas
            score = scorer._score_func(targets, preds, **scorer._kwargs)

            return score
        else:
            scores = {}
            for name, one_scorer in scorer.items():
                preds = pred_labels if one_scorer.__class__.__name__\
                    == '_PredictScorer' else pred_probas
                score = one_scorer._score_func(targets, preds,
                                               **one_scorer._kwargs)
                scores[name] = score

    # TODO: multi-class metrics
    # multi-label
    else:
        pred_labels = (pred_probas > 0.5).astype('int32')
        targets = y_true.astype('int32')
        if not is_multimetric:
            preds = pred_labels if scorer.__class__.__name__ == \
                '_PredictScorer' else pred_probas
            score, _ = compute_score(preds, targets,
                                     scorer._score_func)
            return score
        else:
            scores = {}
            for name, one_scorer in scorer.items():
                preds = pred_labels if one_scorer.__class__.__name__\
                    == '_PredictScorer' else pred_probas
                score, _ = compute_score(preds, targets,
                                         one_scorer._score_func)
                scores[name] = score

    return scores
