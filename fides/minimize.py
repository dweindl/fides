"""
Minimization
------------
This module performs the optimization given a step proposal.
"""

import time

import numpy as np
import logging
from numpy.linalg import norm
from scipy.sparse import csc_matrix
from .trust_region import trust_region_reflective, Step
from .hessian_approximation import HessianApproximation
from .constants import Options, StepBackStrategy, DEFAULT_OPTIONS
from .logging import logger

from typing import Callable, Dict, Optional, Tuple


class Optimizer:
    """
    Performs optimization

    :ivar fun: objective function
    :ivar funargs: keyword arguments that are passed to the function
    :ivar lb: lower optimization boundaries
    :ivar ub: upper optimization boundaries
    :ivar options: options that configure convergence checks
    :ivar delta_iter: trust region radius that was used for the current step
    :ivar delta: updated trust region radius
    :ivar x: current optimization variables
    :ivar fval: objective function value at x
    :ivar grad: objective function gradient at x
    :ivar hess: objective function Hessian (approximation) at x
    :ivar hessian_update: object that performs hessian updates
    :ivar starttime: time at which optimization was started
    :ivar iteration: current iteration
    :ivar converged: flag indicating whether optimization has converged
    """
    def __init__(self, fun: Callable,
                 ub: np.ndarray,
                 lb: np.ndarray,
                 verbose: Optional[int] = logging.DEBUG,
                 options: Optional[Dict] = None,
                 funargs: Optional[Dict] = None,
                 hessian_update: Optional[HessianApproximation] = None):
        """
        Create an optimizer object

        :param fun:
            This is the objective function, if no `hessian_update` is
            provided, this function must return a tuple (fval, grad),
            otherwise this function must return a tuple (fval, grad, Hessian)
        :param ub:
            Upper optimization boundaries. Individual entries can be set to
            np.inf for respective variable to have no upper bound
        :param lb:
            Lower optimization boundaries. Individual entries can be set to
            -np.inf for respective variable to have no lower bound
        :param verbose:
            Verbosity level, pick from logging.[DEBUG,INFO,WARNING,ERROR]
        :param options:
            Options that control termination of optimization.
            See `minimize` for details.
        :param funargs:
            Additional keyword arguments that are to be passed to fun for
            evaluation
        :param hessian_update:
            Subclass of :py:class:`fides.hessian_update.HessianApproximation`
            that performs the hessian updates in every iteration.
        """
        self.fun: Callable = fun
        if funargs is None:
            funargs = {}
        self.funargs: Dict = funargs

        self.lb: np.ndarray = np.array(lb)
        self.ub: np.ndarray = np.array(ub)

        if options is None:
            options = {}

        for option in options:
            try:
                Options(option)
            except ValueError:
                raise ValueError(f'{option} is not a valid options field.')

        self.options: Dict = options

        self.delta_iter: float = 1
        self.delta: float = 1
        self.tr_ratio: float = 1

        self.x: np.ndarray = np.empty(ub.shape)
        self.fval: float = np.nan
        self.grad: np.ndarray = np.empty(ub.shape)
        self.hess: np.ndarray = np.empty((ub.shape[0], ub.shape[0]))

        self.hessian_update: HessianApproximation = hessian_update

        self.starttime: float = np.nan
        self.iteration: int = 0
        self.converged: bool = False
        logger.setLevel(verbose)

    def minimize(self, x0: np.ndarray):
        """
        Minimize the objective function the interior trust-region reflective
        algorithm described by [ColemanLi1994] and [ColemanLi1996]
        Convergence with respect to function value is achieved when
        math:`|f_{k+1} - f_k|` < options[`fatol`] - :math:`f_k` options[
        `frtol`]. Similarly,  convergence with respect to optimization
        variables is achieved when :math:`||x_{k+1} - x_k||` < options[
        `xatol`] - :math:`x_k` options[`xrtol`].  Convergence with respect
        to the gradient is achieved when :math:`||g_k||` <
        options[`gatol`] or `||g_k||` < options[`grtol`] * `f_k`.  Other than
        that, optimization can be terminated when iterations exceed
        options[ `maxiter`] or the elapsed time is expected to exceed
        options[`maxtime`] on the next iteration.

        :param x0:
            initial guess

        :returns:
            fval: final function value,
            x: final optimization variable values,
            grad: final gradient,
            hess: final Hessian (approximation)
        """
        self.starttime = time.time()
        self.iteration = 0

        self.x = np.array(x0).copy()
        self.make_non_degenerate()
        self.check_in_bounds()
        if self.hessian_update is None:
            self.fval, self.grad, self.hess = self.fun(self.x, **self.funargs)
        else:
            self.fval, self.grad = self.fun(self.x, **self.funargs)
            self.hess = self.hessian_update.get_mat()
        self.log_header()
        self.log_step_initial()

        self.check_finite()

        self.converged = False

        while self.check_continue():
            self.iteration += 1
            self.delta_iter = self.delta

            v, dv = self.get_affine_scaling()

            scaling = csc_matrix(np.diag(np.sqrt(np.abs(v))))
            theta = max(.95, 1 - norm(v * self.grad, np.inf))

            step = \
                trust_region_reflective(
                    self.x, self.grad, self.hess, scaling,
                    self.delta, dv, theta, self.lb, self.ub,
                    self.get_option(Options.SUBSPACE_DIM),
                    self.get_option(Options.STEPBACK_STRAT)
                )

            x_new = self.x + step.s + step.s0

            if self.hessian_update is None:
                fval_new, grad_new, hess_new = self.fun(x_new, **self.funargs)
            else:
                fval_new, grad_new = self.fun(x_new, **self.funargs)

            accepted = self.update_tr_radius(fval_new, grad_new, step, dv)

            if (self.iteration) % 10 == 0:
                self.log_header()
            self.log_step(accepted, step, theta, fval_new)
            self.check_convergence(fval_new, x_new, grad_new)

            if accepted:
                if self.hessian_update is not None:
                    self.hessian_update.update(step.s + step.s0,
                                               grad_new - self.grad)
                    self.hess = self.hessian_update.get_mat()
                else:
                    self.hess = hess_new
                self.check_in_bounds(x_new)
                self.fval = fval_new
                self.x = x_new
                self.grad = grad_new
                self.check_finite()
                self.make_non_degenerate()

        return self.fval, self.x, self.grad, self.hess

    def update_tr_radius(self,
                         fval: float,
                         grad: np.ndarray,
                         step: Step,
                         dv: np.ndarray) -> bool:
        """
        Update the trust region radius

        :param fval:
            new function value if step defined by step_sx is taken
        :param grad:
            new gradient value if step defined by step_sx is taken
        :param step:
            step
        :param dv:
            derivative of scaling vector v wrt x

        :return:
            flag indicating whether the proposed step should be accepted
        """
        stepsx = step.ss + step.ss0
        nsx = norm(stepsx)
        if not np.isfinite(fval):
            self.delta = np.min([self.delta / 4, nsx / 4])
            return False
        else:
            qpval = 0.5 * stepsx.dot(dv * np.abs(grad) * stepsx)
            self.tr_ratio = (fval + qpval - self.fval) / step.qpval

            # values as proposed in algorithm 4.1 in Nocedal & Wright
            if self.tr_ratio >= 0.75 and nsx > self.delta * 0.9:
                self.delta = 2 * self.delta
            elif self.tr_ratio <= .25 or nsx < self.delta * 0.9 \
                    or fval > self.fval * 1.1:
                self.delta = np.min([self.delta / 4, nsx / 4])
            return self.tr_ratio >= .25 and fval < self.fval * 1.1

    def check_convergence(self, fval, x, grad) -> None:
        """
        Check whether optimization has converged.

        :param fval:
            updated objective function value
        :param x:
            updated optimization variables
        :param grad:
            updated objective function gradient
        """
        converged = False

        fatol = self.get_option(Options.FATOL)
        frtol = self.get_option(Options.FRTOL)
        xatol = self.get_option(Options.XATOL)
        xrtol = self.get_option(Options.XRTOL)
        gatol = self.get_option(Options.GATOL)
        grtol = self.get_option(Options.GRTOL)
        gnorm = norm(grad)

        if np.isclose(fval, self.fval, atol=fatol, rtol=frtol):
            logger.info(
                'Stopping as function difference '
                f'{np.abs(self.fval - fval)} was smaller than specified '
                f'tolerances (atol={fatol:.2E}, rtol={frtol:.2E})'
            )
            converged = True

        elif np.isclose(x, self.x, atol=xatol, rtol=xrtol).all():
            logger.info(
                'Stopping as step was smaller than specified tolerances ('
                f'atol={xatol:.2E}, rtol={xrtol:.2E})'
            )
            converged = True

        elif gnorm <= gatol:
            logger.info(
                'Stopping as gradient norm satisfies absolute convergence '
                f'criteria: {gnorm:.2E} < {gatol:.2E}'
            )
            converged = True

        elif gnorm <= grtol * self.fval:
            logger.info(
                'Stopping as gradient norm satisfies relative convergence '
                f'criteria: {gnorm:.2E} < {grtol:.2E} * {self.fval:.2E}'
            )
            converged = True

        self.converged = converged

    def check_continue(self) -> bool:
        """
        Checks whether minimization should continue based on convergence,
        iteration count and remaining computational budget

        :return:
            flag indicating whether minimization should continue
        """

        if self.converged:
            return False

        maxiter = self.get_option(Options.MAXITER)
        if self.iteration > maxiter:
            logger.error(
                f'Stopping as maximum number of iterations {maxiter} was '
                f'exceeded.'
            )
            return False

        time_elapsed = time.time() - self.starttime
        maxtime = self.get_option(Options.MAXTIME)
        time_remaining = maxtime - time_elapsed
        avg_iter_time = time_elapsed/(self.iteration + (self.iteration == 0))
        if time_remaining < avg_iter_time:
            logger.error(
                f'Stopping as maximum runtime {maxtime} is expected to be '
                f'exceeded in the next iteration.'
            )
            return False

        if self.delta < np.spacing(1):
            logger.error(
                'Stopping as trust region radius is smaller that machine '
                'precision.'
            )
            return False

        return True

    def make_non_degenerate(self, eps=1e2 * np.spacing(1)) -> None:
        """
        Ensures that x is non-degenerate, this should only be necessary for
        initial points.

        :param eps: degeneracy threshold
        """
        if np.min(np.abs(self.ub - self.x)) < eps or \
                np.min(np.abs(self.x - self.lb)) < eps:
            upperi = (self.ub - self.x) < eps
            loweri = (self.x - self.lb) < eps
            self.x[upperi] = self.x[upperi] - eps
            self.x[loweri] = self.x[loweri] + eps

    def get_affine_scaling(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Computes the vector v and dv, the diagonal of it's Jacobian. For the
        definition of v, see Definition 2 in [Coleman-Li1994]

        :return:
            v scaling vector
            dv diagonal of the Jacobian of v wrt x
        """
        # this implements no scaling for variables that are not constrained by
        # bounds ((iii) and (iv) in Definition 2)
        v = np.sign(self.grad) + (self.grad == 0)
        dv = np.zeros(self.x.shape)

        # this implements scaling for variables that are constrained by
        # bounds ( i and ii in Definition 2) bounds is equal to lb if grad <
        # 0 ub if grad >= 0
        bounds = ((1 + v)*self.lb + (1 - v)*self.ub)/2
        bounded = np.isfinite(bounds)
        v[bounded] = self.x[bounded] - bounds[bounded]
        dv[bounded] = 1
        return v, dv

    def log_step(self, accepted: bool, step: Step, theta: float, fval: float):
        """
        Prints diagnostic information about the current step to the log

        :param accepted:
            flag indicating whether the current step was accepted
        :param step:
            proposal step
        :param theta:
            value of the theta parameter
        :param fval:
            new fval if step is accepted
        """
        normdx = norm(step.s + step.s0)

        iterspaces = max(len(str(self.get_option(Options.MAXITER))), 5) - \
            len(str(self.iteration))
        steptypespaces = 4 - len(step.type)
        if self.get_option(Options.STEPBACK_STRAT) == StepBackStrategy.REFLECT:
            count = step.reflection_count
        else:
            count = step.truncation_count
        stepbackspaces = 4 - len(str(count))
        logger.info(f'{" " * iterspaces}{self.iteration}'
                    f' | {fval if accepted else self.fval:.3E}'
                    f' | {(fval - self.fval)*accepted:+.2E}'
                    f' | {step.qpval:+.2E}'
                    f' | {self.tr_ratio:+.2E}'
                    f' | {self.delta_iter:.2E}'
                    f' | {norm(self.grad):.2E}'
                    f' | {normdx:.2E}'
                    f' | {theta:.2E}'
                    f' | {step.alpha:.2E}'
                    f' | {step.type}{" " * steptypespaces}'
                    f' | {" " * stepbackspaces}{count}'
                    f' | {int(accepted)}')

    def log_step_initial(self):
        """
        Prints diagnostic information about the initial step to the log
        """

        iterspaces = max(len(str(self.get_option(Options.MAXITER))), 5) - \
                     len(str(self.iteration))
        logger.info(f'{" " * iterspaces}{self.iteration}'
                    f' | {self.fval:.3E}'
                    f' |    NaN   '
                    f' |    NaN   '
                    f' |    NaN   '
                    f' | {self.delta:.2E}'
                    f' | {norm(self.grad):.2E}'
                    f' |   NaN   '
                    f' |   NaN   '
                    f' |   NaN   '
                    f' | NaN '
                    f' | NaN '
                    f' | {int(np.isfinite(self.fval))}')

    def log_header(self):
        """
        Prints the header for diagnostic information, should complement
        :py:func:`Optimizer.log_step`.
        """
        iterspaces = len(str(self.get_option(Options.MAXITER))) - 5

        if self.get_option(Options.STEPBACK_STRAT) == StepBackStrategy.REFLECT:
            countheader = 'refl'
        else:
            countheader = 'trun'

        logger.info(f'{" " * iterspaces} iter '
                    f'|   fval    | fval diff | pred diff | tr ratio  '
                    f'|  delta   |  ||g||   | ||step|| |  theta   |  alpha   '
                    f'| step | {countheader} | accept')

    def check_finite(self):
        """
        Checks whether objective function value, gradient and Hessian (
        approximation) have finite values and optimization can continue.

        :raises:
            RuntimeError if any of the variables have non-finite entries
        """

        if self.iteration == 0:
            pointstr = 'at initial point.'
        else:
            pointstr = f'at iteration {self.iteration}.'

        if not np.isfinite(self.fval):
            raise RuntimeError(f'Encountered non-finite function {self.fval} '
                               f'value {pointstr}')

        if not np.isfinite(self.grad).all():
            ix = np.where(np.logical_not(np.isfinite(self.grad)))
            raise RuntimeError('Encountered non-finite gradient entries'
                               f' {self.grad[ix]} for indices {ix} '
                               f'{pointstr}')

        if not np.isfinite(self.hess).all():
            ix = np.where(np.logical_not(np.isfinite(self.hess)))
            raise RuntimeError('Encountered non-finite gradient hessian'
                               f' {self.hess[ix]} for indices {ix} '
                               f'{pointstr}')

    def check_in_bounds(self, x: Optional[np.ndarray] = None):
        """
        Checks whether the current optimization variables are all within the
        specified boundaries

        :raises:
            RuntimeError if any of the variables are not within boundaries
        """
        if x is None:
            x = self.x

        if self.iteration == 0:
            pointstr = 'at initial point.'
        else:
            pointstr = f'at iteration {self.iteration}.'

        for ref, sign, name in zip([self.ub, self.lb],
                                   [-1.0, 1.0],
                                   ['upper bounds', 'lower bounds']):
            diff = sign * (ref - x)
            if not np.all(diff <= 0):
                ix = np.where(diff > 0)[0]
                raise RuntimeError(f'Exceeded {name} for indices {ix} by '
                                   f'{diff[ix]} {pointstr}')

    def get_option(self, option):
        if option not in Options:
            raise ValueError(f'{option} is not a valid option name.')

        if option not in DEFAULT_OPTIONS:
            raise ValueError(f'{option} is missing its default option.')

        return self.options.get(option, DEFAULT_OPTIONS.get(option))
