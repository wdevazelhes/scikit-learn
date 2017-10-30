# coding: utf-8
"""
Neighborhood Component Analysis
"""

# License: BSD 3 Clause

from __future__ import print_function

import numpy as np
import sys
import time
from scipy.misc import logsumexp
from scipy.optimize import minimize

from ..base import BaseEstimator, TransformerMixin
from ..preprocessing import LabelEncoder
from ..decomposition import PCA
from ..utils.multiclass import check_classification_targets
from ..utils.random import check_random_state
from ..utils.validation import check_is_fitted, check_array, check_X_y
from ..externals.six import integer_types


class NeighborhoodComponentAnalysis(BaseEstimator, TransformerMixin):
    """Neighborhood Component Analysis

    Parameters
    ----------
    n_features_out : int, optional (default=None)
        Preferred dimensionality of the embedding.

    init : string or numpy array, optional (default='pca')
        Initialization of the linear transformation. Possible options are
        'pca', 'identity', 'random', and a numpy array of shape
        (n_features_a, n_features_b).

        pca:
            ``n_features_out`` many principal components of the inputs passed
            to :meth:`fit` will be used to initialize the transformation.

        identity:
            If ``n_features_out`` is strictly smaller than the
            dimensionality of the inputs passed to :meth:`fit`, the identity
            matrix will be truncated to the first ``n_features_out`` rows.

        random:
            The initial transformation will be a random array of shape
            (n_features_out, n_features). Each value is sampled from the
            standard normal distribution.

        numpy array:
            n_features_b must match the dimensionality of the inputs passed to
            :meth:`fit` and n_features_a must be less than or equal to that.
            If ``n_features_out`` is not None, n_features_a must match it.

    max_iter : int, optional (default=50)
        Maximum number of iterations in the optimization.

    tol : float, optional (default=1e-5)
        Convergence tolerance for the optimization.

    callback : callable, optional (default=None)
        If not None, this function is called after every iteration of the
        optimizer, taking as arguments the current solution (transformation)
        and the number of iterations. This might be useful in case one wants
        to examine or store the transformation found after each iteration.

    store_opt_result : bool, optional (default=False)
        If True, the :class:`scipy.optimize.OptimizeResult` object returned by
        :meth:`minimize` of `scipy.optimize` will be stored as attribute
        ``opt_result_``.

    verbose : int, optional (default=0)
        If 0, no progress messages will be printed.
        If 1, progress messages will be printed to stdout.
        If > 1, progress messages will be printed and the ``iprint``
        parameter of :meth:`_minimize_lbfgsb` of `scipy.optimize` will be set
        to ``verbose - 2``.

    random_state : int or numpy.RandomState or None, optional (default=None)
        A pseudo random number generator object or a seed for it if int. If
        ``init='random'``, ``random_state`` is used to initialize the random
        transformation. If ``init='pca'``, ``random_state`` is passed as an
        argument to PCA when initializing the transformation.

    Attributes
    ----------
    transformation_ : array, shape (n_features_out, n_features)
        The linear transformation learned during fitting.

    n_iter_ : int
        Counts the number of iterations performed by the optimizer.

    opt_result_ : scipy.optimize.OptimizeResult (optional)
        A dictionary of information representing the optimization result.
        This is stored only if ``store_opt_result`` was True.

    Examples
    --------
    >>> from sklearn.neighbors.nca import NeighborhoodComponentAnalysis
    >>> from sklearn.neighbors import KNeighborsClassifier
    >>> from sklearn.datasets import load_iris
    >>> from sklearn.model_selection import train_test_split
    >>> X, y = load_iris(return_X_y=True)
    >>> X_train, X_test, y_train, y_test = train_test_split(X, y,
    ... stratify=y, test_size=0.7, random_state=42)
    >>> nca = NeighborhoodComponentAnalysis(None,random_state=42)
    >>> nca.fit(X_train, y_train) # doctest: +ELLIPSIS
    NeighborhoodComponentAnalysis(...)
    >>> knn = KNeighborsClassifier(n_neighbors=3)
    >>> knn.fit(X_train, y_train) # doctest: +ELLIPSIS
    KNeighborsClassifier(...)
    >>> print(knn.score(X_test, y_test))
    0.933333333333
    >>> knn.fit(nca.transform(X_train), y_train) # doctest: +ELLIPSIS
    KNeighborsClassifier(...)
    >>> print(knn.score(nca.transform(X_test), y_test))
    0.961904761905

    Notes
    -----
    Neighborhood Component Analysis (NCA) is a machine learning algorithm for
    metric learning. It learns a linear transformation in a supervised fashion
    to improve the classification accuracy of a stochastic nearest neighbors
    rule in the new space.

    .. warning::

        As NCA is optimizing a non-convex objective function, it will
        likely end up in a local optimum. Several runs with independent random
        init might be necessary to get a good convergence.

    References
    ----------
    .. [1] J. Goldberger, G. Hinton, S. Roweis, R. Salakhutdinov.
           "Neighbourhood Components Analysis". Advances in Neural Information
           Processing Systems. 17, 513-520, 2005.
           http://www.cs.nyu.edu/~roweis/papers/ncanips.pdf
    """

    def __init__(self, n_features_out=None, init='identity', max_iter=50,
                 tol=1e-5, callback=None, store_opt_result=False, verbose=0,
                 random_state=None):

        # Parameters
        self.n_features_out = n_features_out
        self.init = init
        self.max_iter = max_iter
        self.tol = tol
        self.callback = callback
        self.store_opt_result = store_opt_result
        self.verbose = verbose
        self.random_state = random_state

    def fit(self, X, y):
        """Fit the model according to the given training data.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            The training samples.

        y : array-like, shape (n_samples,)
            The corresponding training labels.

        Returns
        -------
        self : object
            returns a trained NeighborhoodComponentAnalysis model.
        """

        # Verify inputs X and y and NCA parameters, and transform a copy if
        # needed
        X_valid, y_valid, init = self._validate_params(X, y)

        # Initialize the random generator
        self.random_state_ = check_random_state(self.random_state)

        # Measure the total training time
        t_train = time.time()

        # Compute arrays that stay fixed during optimization:
        # mask for fast lookup of same-class samples
        masks = _make_masks(y_valid)
        # pairwise differences
        diffs = X_valid[:, np.newaxis] - X_valid[np.newaxis]

        # Initialize the transformation
        transformation = self._initialize(X_valid, init)

        # Create a dictionary of parameters to be passed to the optimizer
        disp = self.verbose - 2 if self.verbose > 1 else -1
        optimizer_params = {'method': 'L-BFGS-B',
                            'fun': self._loss_grad_lbfgs,
                            'args': (X_valid, y_valid, diffs, masks),
                            'jac': True,
                            'x0': transformation,
                            'tol': self.tol,
                            'options': dict(maxiter=self.max_iter, disp=disp),
                            'callback': self._callback
                            }

        # Call the optimizer
        self.n_iter_ = 0
        opt_result = minimize(**optimizer_params)

        # Reshape the solution found by the optimizer
        self.transformation_ = opt_result.x.reshape(-1, X_valid.shape[1])

        # Stop timer
        t_train = time.time() - t_train
        if self.verbose:
            print('[{}] Training took {:8.2f}s.'.format(
                self.__class__.__name__, t_train))

        # Optionally store information returned by the optimizer
        if self.store_opt_result:
            self.opt_result_ = opt_result

        return self

    def transform(self, X):
        """Applies the learned transformation to the given data.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Data samples.

        Returns
        -------
        X_embedded: array, shape (n_samples, n_features_out)
            The data samples transformed.

        Raises
        ------
        NotFittedError
            If :meth:`fit` has not been called before.
        """

        check_is_fitted(self, ['transformation_'])
        X = check_array(X)

        return np.dot(X, self.transformation_.T)

    def _validate_params(self, X, y):
        """Validate parameters as soon as :meth:`fit` is called.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            The training samples.

        y : array-like, shape (n_samples,)
            The corresponding training labels.

        Returns
        -------
        X_valid : array, shape (n_samples, n_features)
            The validated training samples.

        y_valid : array, shape (n_samples,)
            The validated training labels, encoded to be integers in
            the range(0, n_classes).

        init : string or numpy array of shape (n_features_a, n_features_b)
            The validated initialization of the linear transformation.

        Raises
        -------
        TypeError
            If a parameter is not an instance of the desired type.

        ValueError
            If a parameter's value violates its legal value range or if the
            combination of two or more given parameters is incompatible.
        """

        # Validate the inputs X and y, and converts y to numerical classes.
        X_valid, y_valid = check_X_y(X, y)
        check_classification_targets(y_valid)
        y_valid = LabelEncoder().fit_transform(y_valid)

        # Check the preferred embedding dimensionality
        if self.n_features_out is not None:
            _check_scalar(self.n_features_out, 'n_features_out',
                          integer_types, 1)

            if self.n_features_out > X.shape[1]:
                raise ValueError('The preferred embedding dimensionality '
                                 '`n_features_out` ({}) cannot be greater '
                                 'than the given data dimensionality ({})!'
                                 .format(self.n_features_out, X.shape[1]))

        _check_scalar(self.max_iter, 'max_iter', integer_types, 1)
        _check_scalar(self.tol, 'tol', float, 0.)
        _check_scalar(self.verbose, 'verbose', integer_types, 0)

        if self.callback is not None:
            if not callable(self.callback):
                raise ValueError('`callback` is not callable.')

        # Check how the linear transformation should be initialized
        init = self.init

        if isinstance(init, np.ndarray):
            init = check_array(init)

            # Assert that init.shape[1] = X.shape[1]
            if init.shape[1] != X_valid.shape[1]:
                raise ValueError(
                    'The input dimensionality ({}) of the given '
                    'linear transformation `init` must match the '
                    'dimensionality of the given inputs `X` ({}).'
                    .format(init.shape[1], X_valid.shape[1]))

            # Assert that init.shape[0] <= init.shape[1]
            if init.shape[0] > init.shape[1]:
                raise ValueError(
                    'The output dimensionality ({}) of the given '
                    'linear transformation `init` cannot be '
                    'greater than its input dimensionality ({}).'
                    .format(init.shape[0], init.shape[1]))

            if self.n_features_out is not None:
                # Assert that self.n_features_out = init.shape[0]
                if self.n_features_out != init.shape[0]:
                    raise ValueError(
                        'The preferred embedding dimensionality '
                        '`n_features_out` ({}) does not match '
                        'the output dimensionality of the given '
                        'linear transformation `init` ({})!'
                        .format(self.n_features_out,
                                init.shape[0]))

        elif init in ['pca', 'identity', 'random']:
            pass
        else:
            raise ValueError(
                "`init` must be 'pca', 'identity', 'random' or a numpy "
                "array of shape (n_features_out, n_features).")

        return X_valid, y_valid, init

    def _initialize(self, X, init):
        """Initialize the transformation.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Data samples.

        init : string or numpy array of shape (n_features_a, n_features_b)
            The validated initialization of the linear transformation.

        Returns
        -------
        transformation : array, shape (n_features_out, n_features)
            The initialized linear transformation.

        """

        transformation = init

        if isinstance(init, np.ndarray):
            pass
        else:
            n_features_out = self.n_features_out or X.shape[1]
            if init == 'identity':
                transformation = np.eye(n_features_out, X.shape[1])
            elif init == 'random':
                transformation = self.random_state_.randn(n_features_out,
                                                          X.shape[1])
            elif init == 'pca':
                pca = PCA(n_components=n_features_out,
                          random_state=self.random_state_)
                t_pca = time.time()
                if self.verbose:
                    print('Finding principal components... ', end='')
                    sys.stdout.flush()

                pca.fit(X)
                if self.verbose:
                    print('done in {:5.2f}s'.format(time.time() - t_pca))

                transformation = pca.components_
        return transformation

    def _callback(self, transformation):
        """Called after each iteration of the optimizer.

        Parameters
        ----------
        transformation : array, shape(n_features_out, n_features)
            The solution computed by the optimizer in this iteration.
        """
        if self.callback is not None:
            self.callback(transformation, self.n_iter_)

        self.n_iter_ += 1

    def _loss_grad_lbfgs(self, transformation, X, y, diffs,
                         masks):
        """Compute the loss and the loss gradient w.r.t. ``transformation``.

        Parameters
        ----------
        transformation : array, shape (n_features_out, n_features)
            The linear transformation on which to compute loss and evaluate
            gradient
        X : array, shape (n_samples, n_features)
            The training samples.

        y : array, shape (n_samples,)
            The corresponding training labels.

        diffs : array, shape (n_samples, n_samples, n_features)
            Pairwise differences between training samples.

        masks : array, shape (n_samples, n_classes)
            One-hot encoding of y.

        Returns
        -------
        loss : float
            The loss computed for the given transformation.

        gradient : array, shape (n_features_out * n_features,)
            The new (flattened) gradient of the loss.
        """

        if self.n_iter_ == 0:
            self.n_iter_ += 1
            if self.verbose:
                header_fields = ['Iteration', 'Objective Value', 'Time(s)']
                header_fmt = '{:>10} {:>20} {:>10}'
                header = header_fmt.format(*header_fields)
                cls_name = self.__class__.__name__
                print('[{}]'.format(cls_name))
                print('[{}] {}\n[{}] {}'.format(cls_name, header,
                                                cls_name, '-' * len(header)))

        t_funcall = time.time()

        transformation = transformation.reshape(-1, X.shape[1])
        loss = 0
        gradient = np.zeros(transformation.shape)
        X_embedded = transformation.dot(X.T).T

        # for every sample, compute its contribution to loss and gradient
        for i in range(X.shape[0]):
            diff_embedded = X_embedded[i] - X_embedded
            sum_of_squares = np.einsum('ij,ij->i', diff_embedded,
                                       diff_embedded)
            sum_of_squares[i] = np.inf
            soft = np.exp(-sum_of_squares - logsumexp(-sum_of_squares))
            ci = masks[:, y[i]]
            p_i_j = soft[ci]
            not_ci = np.logical_not(ci)
            diff_ci = diffs[i, ci, :]
            diff_not_ci = diffs[i, not_ci, :]
            sum_ci = diff_ci.T.dot(
                (p_i_j[:, np.newaxis] * diff_embedded[ci, :]))
            sum_not_ci = diff_not_ci.T.dot((soft[not_ci][:, np.newaxis] *
                                            diff_embedded[not_ci, :]))
            p_i = np.sum(p_i_j)
            gradient += 2 * (p_i * (sum_ci.T + sum_not_ci.T) - sum_ci.T)
            loss += p_i

        if self.verbose:
            t_funcall = time.time() - t_funcall
            values_fmt = '[{}] {:>10} {:>20.6e} {:>10.2f}'
            print(values_fmt.format(self.__class__.__name__, self.n_iter_,
                                    loss, t_funcall))
            sys.stdout.flush()

        return - loss, - gradient.ravel()


##########################
# Some helper functions #
#########################


def _check_scalar(x, name, target_type, min_val=None, max_val=None):
    """Validate scalar parameters type and value.

    Parameters
    ----------
    x : object
        The scalar parameter to validate.

    name : str
        The name of the parameter to be printed in error messages.

    target_type : type or tuple
        Acceptable data types for the parameter.

    min_val : float or int, optional (default=None)
        The minimum value value the parameter can take. If None (default) it
        is implied that the parameter does not have a lower bound.

    max_val: float or int, optional (default=None)
        The maximum valid value the parameter can take. If None (default) it
        is implied that the parameter does not have an upper bound.

    Raises
    -------
    TypeError
        If the parameter's type does not match the desired type.

    ValueError
        If the parameter's value violates the given bounds.
    """

    if not isinstance(x, target_type):
        raise TypeError('`{}` must be an instance of {}, not {}.'
                        .format(name, target_type, type(x)))

    if min_val is not None and x < min_val:
        raise ValueError('`{}`= {}, must be >= {}.'.format(name, x, min_val))

    if max_val is not None and x > max_val:
        raise ValueError('`{}`= {}, must be <= {}.'.format(name, x, max_val))


def _make_masks(y):
    """Create one-hot encoding of vector ``y``.

    Parameters
    ----------
    y : array, shape (n_samples,)
        Data samples labels.

    Returns
    -------
    masks: array, shape (n_samples, n_classes)
        One-hot encoding of ``y``.
    """

    n = y.shape[0]
    masks = np.zeros((n, y.max() + 1))
    masks[np.arange(n), y] = [1]
    return masks.astype(bool)
