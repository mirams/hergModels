#
#
from __future__ import absolute_import, division
from __future__ import print_function, unicode_literals
import sys
sys.path.append('../lib/')
import pints
import numpy as np


class MCMCSampling(object):
    """
    Samples from a :class:`pints.LogPDF` using a Markov Chain Monte Carlo
    (MCMC) method.

    Arguments:

    ``log_pdf``
        A :class:`LogPDF` function that evaluates points in the parameter
        space.
    ``chains``
        The number of MCMC chains to generate.
    ``x0``
        A sequence of starting points. Can be a list of lists, a 2-dimensional
        array, or any other structure such that ``x0[i]`` is the starting point
        for chain ``i``.
    ``sigma0=None``
        An optional initial covariance matrix, i.e., a guess of the covariance
        in ``logpdf`` around the points in ``x0`` (the same ``sigma0`` is used
        for each point in ``x0``).
        Can be specified as a ``(d, d)`` matrix (where ``d`` is the dimension
        of the parameterspace) or as a ``(d, )`` vector, in which case
        ``diag(sigma0)`` will be used.
    ``method``
        The class of :class:`MCMCSampler` to use. If no method is specified,
        :class:`AdaptiveCovarianceMCMC` is used.

    """

    def __init__(self, log_pdf, chains, x0, sigma0=None, method=None):

        # Store function
        if not isinstance(log_pdf, pints.LogPDF):
            raise ValueError('Given function must extend pints.LogPDF')
        self._log_pdf = log_pdf

        # Get dimension
        self._dimension = self._log_pdf.n_parameters()

        # Check number of chains
        self._chains = int(chains)
        if self._chains < 1:
            raise ValueError('Number of chains must be at least 1.')

        # Check initial position(s): Most checking is done by samplers!
        if len(x0) != chains:
            raise ValueError(
                'Number of initial positions must be equal to number of'
                ' chains.')
        if not all([len(x) == self._dimension for x in x0]):
            raise ValueError(
                'All initial positions must have the same dimension as the'
                ' given LogPDF.')

        # Don't check initial standard deviation: done by samplers!

        # Set default method
        if method is None:
            method = pints.AdaptiveCovarianceMCMC
        else:
            try:
                ok = issubclass(method, pints.MCMCSampler)
            except TypeError:   # Not a class
                ok = False
            if not ok:
                raise ValueError('Given method must extend pints.MCMCSampler.')

        # Using single chain samplers?
        self._single_chain = issubclass(method, pints.SingleChainMCMC)

        # Create sampler(s)
        if self._single_chain:
            # Using n individual samplers (Note that it is possible to have
            # _single_chain=True and _n_samplers=1)
            self._n_samplers = self._chains
            self._samplers = [method(x, sigma0) for x in x0]
        else:
            # Using a single sampler that samples multiple chains
            self._n_samplers = 1
            self._samplers = [method(self._chains, x0, sigma0)]

        # Logging
        self._log_to_screen = True
        self._log_filename = None
        self._log_csv = False
        self.set_log_rate()

        # Parallelisation
        self._parallel = False
        self._n_workers = 1
        self.set_parallel()

        # Initial phase (needed for e.g. adaptive covariance)
        self._initial_phase_iterations = 0
        self._needs_initial_phase = self._samplers[0].needs_initial_phase()
        if self._needs_initial_phase:
            self.set_initial_phase_iterations()

        #
        # Stopping criteria
        #

        # Maximum iterations
        self._max_iterations = None
        self.set_max_iterations()

        # TODO: Add more stopping criteria

    def initial_phase_iterations(self):
        """
        For methods that require an initial phase (e.g. an adaptation-free
        phase for the adaptive covariance MCMC method), this returns the number
        of iterations that the initial phase will take.

        For methods that do not require an initial phase, a
        ``NotImplementedError`` is raised.
        """
        return self._initial_phase_iterations

    def max_iterations(self):
        """
        Returns the maximum iterations if this stopping criterion is set, or
        ``None`` if it is not. See :meth:`set_max_iterations()`.
        """
        return self._max_iterations

    def method_needs_initial_phase(self):
        """
        Returns true if this sampler has been created with a method that has
        an initial phase (see :meth:`MCMCSampler.needs_initial_phase()`.)
        """
        return self._samplers[0].needs_initial_phase()

    def parallel(self):
        """
        Returns the number of parallel worker processes this routine will be
        run on, or ``False`` if parallelisation is disabled.
        """
        return self._n_workers if self._parallel else False

    def run(self, returnLL=False):
        """
        Runs the MCMC sampler(s) and returns a number of markov chains, each
        representing the distribution of the given log-pdf.
        """
        # Check stopping criteria
        has_stopping_criterion = False
        has_stopping_criterion |= (self._max_iterations is not None)
        if not has_stopping_criterion:
            raise ValueError('At least one stopping criterion must be set.')

        # Iteration and evaluation counting
        iteration = 0
        evaluations = 0

        # Create evaluator object
        if self._parallel:
            # Use at most n_workers workers
            n_workers = min(self._n_workers, self._chains)
            evaluator = pints.ParallelEvaluator(
                self._log_pdf, n_workers=n_workers)
        else:
            evaluator = pints.SequentialEvaluator(self._log_pdf)

        # Initial phase
        if self._needs_initial_phase:
            for sampler in self._samplers:
                sampler.set_initial_phase(True)

        # Set up progress reporting
        next_message = 0

        # Start logging
        logging = self._log_to_screen or self._log_filename
        if logging:
            if self._log_to_screen:
                print('Using ' + str(self._samplers[0].name()))
                print('Generating ' + str(self._chains) + ' chains.')
                if self._parallel:
                    print('Running in parallel with ' + str(n_workers) +
                          ' worker processess.')
                else:
                    print('Running in sequential mode.')

            # Set up logger
            logger = pints.Logger()
            if not self._log_to_screen:
                logger.set_stream(None)
            if self._log_filename:
                logger.set_filename(self._log_filename, csv=self._log_csv)

            # Add fields to log
            max_iter_guess = max(self._max_iterations or 0, 10000)
            max_eval_guess = max_iter_guess * self._chains
            logger.add_counter('Iter.', max_value=max_iter_guess)
            logger.add_counter('Eval.', max_value=max_eval_guess)
            for sampler in self._samplers:
                sampler._log_init(logger)
            logger.add_time('Time m:s')

        # Create chains
        # TODO Pre-allocate?
        # TODO Thinning
        # TODO Advanced logging
        LLs = []
        chains = []

        # Start sampling
        timer = pints.Timer()
        running = True
        while running:
            # Initial phase
            if (self._needs_initial_phase and
                    iteration == self._initial_phase_iterations):
                for sampler in self._samplers:
                    sampler.set_initial_phase(False)
                if self._log_to_screen:
                    print('Initial phase completed.')

            # Get points
            if self._single_chain:
                xs = [sampler.ask() for sampler in self._samplers]
            else:
                xs = self._samplers[0].ask()

            # Calculate scores
            fxs = evaluator.evaluate(xs)

            # Perform iteration(s)
            if self._single_chain:
                samples = np.array([
                    s.tell(fxs[i]) for i, s in enumerate(self._samplers)])
            else:
                samples = self._samplers[0].tell(fxs)
            if returnLL:
                LLs.append(evaluator.evaluate(samples))
            chains.append(samples)

            # Update evaluation count
            evaluations += len(fxs)

            # Show progress
            if logging and iteration >= next_message:
                # Log state
                logger.log(iteration, evaluations)
                for sampler in self._samplers:
                    sampler._log_write(logger)
                logger.log(timer.time())

                # Choose next logging point
                if iteration < self._message_warm_up:
                    next_message = iteration + 1
                else:
                    next_message = self._message_rate * (
                        1 + iteration // self._message_rate)

            # Update iteration count
            iteration += 1

            #
            # Check stopping criteria
            #

            # Maximum number of iterations
            if (self._max_iterations is not None and
                    iteration >= self._max_iterations):
                running = False
                halt_message = ('Halting: Maximum number of iterations ('
                                + str(iteration) + ') reached.')

            # TODO Add more stopping criteria

        # Log final state and show halt message
        if logging:
            logger.log(iteration, evaluations)
            for sampler in self._samplers:
                sampler._log_write(logger)
            logger.log(timer.time())
            if self._log_to_screen:
                print(halt_message)

        # Swap axes in chains, to get indices
        #  [chain, iteration, parameter]
        chains = np.array(chains)
        chains = chains.swapaxes(0, 1)
        if returnLL:
            LLs = np.array(LLs)
            #LLs = LLs.swapaxes(0, 1)
            return chains, LLs

        # Return generated chains
        return chains

    def sampler(self):
        """
        For multi-chain methods, this returns the single underlying sampler.
        For single-chain methods, this raises an RuntimeError.
        """
        if self._single_chain:
            raise RuntimeError(
                'The `sampler` method is not supported for single-sampler'
                ' methods.')
        return self._samplers[0]

    def samplers(self):
        """
        Returns the underlying array of samplers. The length of the array will
        either be the number of chains, or one for samplers that sample
        multiple chains
        """
        return self._samplers

    def set_initial_phase_iterations(self, iterations=200):
        """
        For methods that require an initial phase (e.g. an adaptation-free
        phase for the adaptive covariance MCMC method), this sets the number of
        iterations that the initial phase will take.

        For methods that do not require an initial phase, a
        ``NotImplementedError`` is raised.
        """
        if not self._needs_initial_phase:
            raise NotImplementedError

        # Check input
        iterations = int(iterations)
        if iterations < 0:
            raise ValueError(
                'Number of initial-phase iterations cannot be negative.')
        self._initial_phase_iterations = iterations

    def set_log_rate(self, rate=20, warm_up=3):
        """
        Changes the frequency with which messages are logged.

        Arguments:

        ``rate``
            A log message will be shown every ``rate`` iterations.
        ``warm_up``
            A log message will be shown every iteration, for the first
            ``warm_up`` iterations.

        """
        rate = int(rate)
        if rate < 1:
            raise ValueError('Rate must be greater than zero.')
        warm_up = max(0, int(warm_up))

        self._message_rate = rate
        self._message_warm_up = warm_up

    def set_log_to_file(self, filename=None, csv=False):
        """
        Enables logging to file when a filename is passed in, disables it if
        ``filename`` is ``False`` or ``None``.

        The argument ``csv`` can be set to ``True`` to write the file in comma
        separated value (CSV) format. By default, the file contents will be
        similar to the output on screen.
        """
        if filename:
            self._log_filename = str(filename)
            self._log_csv = True if csv else False
        else:
            self._log_filename = None
            self._log_csv = False

    def set_log_to_screen(self, enabled):
        """
        Enables or disables logging to screen.
        """
        self._log_to_screen = True if enabled else False

    def set_max_iterations(self, iterations=10000):
        """
        Adds a stopping criterion, allowing the routine to halt after the
        given number of `iterations`.

        This criterion is enabled by default. To disable it, use
        `set_max_iterations(None)`.
        """
        if iterations is not None:
            iterations = int(iterations)
            if iterations < 0:
                raise ValueError(
                    'Maximum number of iterations cannot be negative.')
        self._max_iterations = iterations

    def set_parallel(self, parallel=False):
        """
        Enables/disables parallel evaluation.

        If ``parallel=True``, the method will run using a number of worker
        processes equal to the detected cpu core count. The number of workers
        can be set explicitly by setting ``parallel`` to an integer greater
        than 0.
        Parallelisation can be disabled by setting ``parallel`` to ``0`` or
        ``False``.
        """
        if parallel is True:
            self._parallel = True
            self._n_workers = pints.ParallelEvaluator.cpu_count()
        elif parallel >= 1:
            self._parallel = True
            self._n_workers = int(parallel)
        else:
            self._parallel = False
            self._n_workers = 1


def mcmc_sample(log_pdf, chains, x0, sigma0=None, method=None):
    """
    Sample from a :class:`pints.LogPDF` using a Markov Chain Monte Carlo
    (MCMC) method.

    Arguments:

    ``log_pdf``
        A :class:`LogPDF` function that evaluates points in the parameter
        space.
    ``chains``
        The number of MCMC chains to generate.
    ``x0``
        A sequence of starting points. Can be a list of lists, a 2-dimensional
        array, or any other structure such that ``x0[i]`` is the starting point
        for chain ``i``.
    ``sigma0=None``
        An optional initial covariance matrix, i.e., a guess of the covariance
        in ``logpdf`` around the points in ``x0`` (the same ``sigma0`` is used
        for each point in ``x0``).
        Can be specified as a ``(d, d)`` matrix (where ``d`` is the dimension
        of the parameterspace) or as a ``(d, )`` vector, in which case
        ``diag(sigma0)`` will be used.
    ``method``
        The class of :class:`MCMCSampler` to use. If no method is specified,
        :class:`AdaptiveCovarianceMCMC` is used.
    """
    return MCMCSampling(    # pragma: no cover
        log_pdf, chains, x0, sigma0, method).run()
