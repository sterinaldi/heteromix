import numpy as np
from figaro.mixture import DPGMM
from figaro.decorators import probit
from figaro.transform import probit_logJ
from scipy.special import logsumexp

def uniform(x, v):
    return np.ones(np.shape(x))*v

class par_model:
    """
    Class to store a parametric model.
    
    Arguments:
        :callable model: model pdf
        :iterable pars:  parameters of the model
    
    Returns:
        :par_model: instance of model class
    """
    def __init__(self, model, pars, bounds):
        self.model  = model
        self.pars   = pars
        self.bounds = bounds
    
    def __call__(self, x):
        return self.pdf(x)

    def pdf(self, x):
        return self.model(x, *self.pars)
    
    def pdf_pars(self, x, pars):
        return self.model(x, *pars)

class het_mixture:
    """
    Class to store a single draw from HMM.
    
    Arguments:
        :list-of-callables models: models in the mixture
        :iterable pars:            list of model parameters. Must be formatted as [[p1, p2, ...], [q1, q2, ...], ...]. Add empty list for no parameters.
        :np.ndarray:               weights
        
    Returns:
        :het_mixture: instance of het_mixture class
    """
    def __init__(self, models, weights, bounds):
        self.models  = models
        self.weights = weights
        self.bounds  = bounds
    
    def __call__(self, x):
        return self.pdf(x)
    
    def pdf(self, x):
        return np.array([wi*mi.pdf(x) for wi, mi in zip(self.weights, self.models)]).sum(axis = 0)

#-----------------#
# Inference class #
#-----------------#

class HMM:
    """
    Class to infer a distribution given a set of samples.
    
    Arguments:
        :list-of-callbles:    models
        :iterable bounds:     boundaries of the rectangle over which the distribution is defined. It should be in the format [[xmin, xmax],[ymin, ymax],...]
        :iterable prior_pars: NIW prior parameters (k, L, nu, mu)
        :iterable par_bounds: boundaries of the allowed values for the parameters. It should be in the format [[[xmin, xmax],[ymin, ymax]],[[xmin, xmax]],...]
        :double n_draws:      number of draws for MC integral over parameters
        :double alpha0:       initial guess for concentration parameter
        :np.array gamma0:     Dirichlet Distribution prior
    
    Returns:
        :HMM: instance of HMM class
    """
    def __init__(self, models,
                       bounds,
                       pars = None,
                       par_bounds = None,
                       prior_pars = None,
                       n_draws = 1e3,
                       alpha0 = 1.,
                       gamma0 = None,
                       ):
                       
        if pars is None:
            pars = [[] for _ in models]
            self.n_draws = 0.
        if par_bounds is not None:
            self.par_bounds = np.atleast_3d(par_bounds).reshape(len(models), -1, 2)
            self.n_draws = int(n_draws)
        else:
            self.par_bounds = None
                
        self.par_models = [par_model(mod, p, bounds) for mod, p in zip(models, pars)]
        if self.par_bounds is not None:
            self.par_draws   = np.atleast_2d([np.random.uniform(low = b[:,0], high = b[:,1], size = (self.n_draws, len(b))) for b in self.par_bounds])
            self.log_total_p = [np.zeros(self.n_draws) for _ in range(len(self.par_models))]
        self.DPGMM  = DPGMM(bounds = bounds, prior_pars = prior_pars, alpha0 = alpha0)
        self.bounds       = np.atleast_2d(bounds)
        self.volume       = np.prod(np.diff(self.DPGMM.bounds, axis = 1))
        self.components   = [self.DPGMM] + self.par_models
        self.n_components = len(models) + 1
        
        self.n_pts = np.zeros(self.n_components)
         
        if gamma0 is None:
            # Symmetric prior
            self.gamma0  = np.ones(self.n_components)
        else:
            gamma0 = np.array([gamma0])
            if len(gamma0) == self.n_components:
                self.gamma0 = gamma0
            else:
                raise Exception("gamma0 must be an array with {n} components.".format(self.n_components))
        self.weights = self.gamma0/np.sum(self.gamma0)
    
    def __call__(self, x):
        return self.pdf(x)
    
    def initialise(self):
        self.n_pts     = np.zeros(self.n_components)
        self.weights   = self.gamma0/np.sum(self.gamma0)
        if self.par_bounds is not None:
            self.par_draws   = np.array([np.random.uniform(low = b[:,0], high = b[:,1], size = (self.n_draws, len(b))) for b in self.par_bounds])
            self.log_total_p = [np.zeros(self.n_draws) for _ in range(len(self.par_models))]
    
    def _assign_to_component(self, x):
        scores = np.zeros(self.n_components)
        vals = np.zeros(shape = (self.n_components, self.n_draws))
        for i in range(self.n_components):
            score, vals[i] = self._log_predictive_likelihood(x, i)
            scores[i]  = score
            scores[i] += np.log(self.gamma0[i] + self.n_pts[i])
        scores = np.exp(scores - logsumexp(scores))
        id = np.random.choice(self.n_components, p = scores)
        self.n_pts[id] += 1
        self.weights = (self.n_pts + self.gamma0)/np.sum(self.n_pts + self.gamma0)
        # If DPGMM, updates mixture
        if id == 0:
            self.DPGMM.add_new_point(x)
        # Parameter estimation
        else:
            self.log_total_p[id-1] += vals[id]
        
    def _log_predictive_likelihood(self, x, i):
        if i == 0:
            return self._log_predictive_mixture(x), np.zeros(self.n_draws)
        else:
            if self.par_bounds is None:
                p = self.components[i].pdf(x)
                if p == 0.:
                    return -np.inf, np.zeros(self.n_draws)
                return np.log(p), np.zeros(self.n_draws)
            else:
                p = np.array([self.components[i].pdf_pars(x, pars) for pars in self.par_draws[i-1]]).flatten()
                log_p = np.ones(len(p))*-np.inf
                log_p[p>0] = np.log(p[p>0])
                v = logsumexp(log_p + self.log_total_p[i-1]) - logsumexp(self.log_total_p[i-1])
                return v, log_p

    
    @probit
    def _log_predictive_mixture(self, x):
        scores = {}
        for i in list(np.arange(self.DPGMM.n_cl)) + ["new"]:
            if i == "new":
                ss = "new"
            else:
                ss = self.DPGMM.mixture[i]
            scores[i] = self.DPGMM._log_predictive_likelihood(x, ss)
            if ss == "new":
                scores[i] += np.log(self.DPGMM.alpha) - np.log(self.DPGMM.n_pts + self.DPGMM.alpha)
            else:
                scores[i] += np.log(ss.N) - np.log(self.DPGMM.n_pts + self.DPGMM.alpha)
        scores = [score for score in scores.values()]
        return logsumexp(scores) - probit_logJ(x, self.bounds)
    
    def add_new_point(self, x):
        self._assign_to_component(np.atleast_2d(x))
    
    def density_from_samples(self, samples):
        np.random.shuffle(samples)
        for s in samples:
            self.add_new_point(s)
        d = self.build_mixture()
        self.DPGMM.initialise()
        self.initialise()
        return d
    
    def pdf(self, x):
        return np.array([wi*mi.pdf(x) for wi, mi in zip(self.weights, self.models)]).sum(axis = 0)
    
    def build_mixture(self):
        if self.par_draws is not None:
            for i in range(len(self.par_models)):
                pars     = self.par_draws[i].T
                vals     = np.exp(self.log_total_p[i] - logsumexp(self.log_total_p[i]))
                par_vals = np.array([np.sum(p*vals) for p in pars])
                self.par_models[i].pars = par_vals

        if self.DPGMM.n_pts == 0:
            models = [par_model(uniform, [1./self.volume], self.bounds)] + self.par_models
        else:
            models = [self.DPGMM.build_mixture()] + self.par_models
        return het_mixture(models, self.weights, self.bounds)
        
