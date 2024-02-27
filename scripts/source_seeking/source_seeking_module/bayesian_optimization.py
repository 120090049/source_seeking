import os
import csv
import copy
import json
import imageio.v2 as imageio
import datetime
import warnings
import itertools
import __main__
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

from tqdm import tqdm
from matplotlib import cm
from scipy.stats import norm
from scipy.optimize import minimize
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from sklearn.utils import check_random_state
from sklearn.preprocessing import StandardScaler
from sklearn.gaussian_process import kernels, GaussianProcessRegressor
import torch
import gpytorch
import pandas as pd

warnings.filterwarnings("ignore")

class bayesian_optimization:
    def __init__(self, domain, n_workers = 1,
                 kernel = kernels.RBF(), alpha=10**(-10),
                 acquisition_function = 'ei', policy = 'greedy',
                 epsilon = 0.01, regularization = None, regularization_strength = None,
                 pending_regularization = None, pending_regularization_strength = None,
                 grid_density = 100):

        # Optimization setup
        self.n_workers = n_workers

        self._policy = policy
        if policy not in ['greedy', 'boltzmann']:
            print("Supported policies: 'greedy', 'boltzmann' ")
            return

        # Regularization function
        self._regularization = None
        if regularization is not None:
            if regularization == 'ridge':
                self._regularization = self._ridge
            else:
                print('Supported regularization functions: ridge')
                return
        self._pending_regularization = None
        if pending_regularization is not None:
            if pending_regularization == 'ridge':
                self._pending_regularization = self._ridge
            else:
                print('Supported pending_regularization functions: ridge')
                return

        # Domain
        self.domain = domain    #shape = [n_params, 2]
        self._dim = domain.shape[0]
        self._grid_density = grid_density
        grid_elemets = []
        for [i,j] in self.domain:
            grid_elemets.append(np.linspace(i, j, self._grid_density))
        self._grid = np.array(list(itertools.product(*grid_elemets)))

        # Model Setup
        self.alpha = alpha
        self.kernel = kernel
        self._regularization_strength = regularization_strength
        self._pending_regularization_strength = pending_regularization_strength
        self.model = [GaussianProcessRegressor(  kernel=self.kernel,
                                                    alpha=self.alpha,
                                                    n_restarts_optimizer=10)
                                                    for i in range(self.n_workers) ]
        self.scaler = [StandardScaler() for i in range(n_workers)]

        # Data holders
        self.bc_data = None
        self.X_train = self.Y_train = None
        self.X = self.Y = None
        self._acquisition_evaluations = [[] for i in range(n_workers)]

        # file storage
        self._DT_ = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self._ROOT_DIR_ = "E:\\workspace\\dbo"
        # self._ROOT_DIR_ = os.path.dirname(os.path.dirname(__main__.__file__))

        alg_name = acquisition_function.upper()

       
        self.alg_name = acquisition_function


        self._TEMP_DIR_ = os.path.join(os.path.join(self._ROOT_DIR_, "result"))
        self._ID_DIR_ = os.path.join(self._TEMP_DIR_, alg_name + self._DT_)
        self._DATA_DIR_ = os.path.join(self._ID_DIR_, "data")
        self._FIG_DIR_ = os.path.join(self._ID_DIR_, "fig")
        self._PNG_DIR_ = os.path.join(self._FIG_DIR_, "png")
        self._PDF_DIR_ = os.path.join(self._FIG_DIR_, "pdf")
        self._GIF_DIR_ = os.path.join(self._FIG_DIR_, "gif")
        for path in [self._TEMP_DIR_, self._DATA_DIR_, self._FIG_DIR_, self._PNG_DIR_, self._PDF_DIR_, self._GIF_DIR_]:
            try:
                os.makedirs(path)
            except FileExistsError:
                pass

        self.beta = None


    def _ridge(self, x, center = 0):
        return np.linalg.norm(x - center)

class ExactGPModel(gpytorch.models.ExactGP):
    def __init__(self, train_x, train_y, likelihood):
        super(ExactGPModel, self).__init__(train_x, train_y, likelihood)
        self.mean_module = gpytorch.means.ConstantMean()
        self.covar_module = gpytorch.kernels.ScaleKernel(gpytorch.kernels.MaternKernel())

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)

class TorchGPModel():
    def __init__(self, X, Y):
        self.likelihood = gpytorch.likelihoods.GaussianLikelihood()
        self.model = ExactGPModel(X, Y, self.likelihood)
        self.train()
    
    def train(self):
        self.model.train()
        self.likelihood.train()

    def fit(self, X, Y):
        if isinstance(X, list):
            X = np.array(X)
        if isinstance(X, np.ndarray):
            X = torch.tensor(X).float()
        if isinstance(Y, np.ndarray):
            Y = torch.tensor(Y).float()
        if len(X.shape) == 2:
            X = X
        if len(Y.shape) == 2:
            Y = torch.reshape(Y, [-1, ])
        # try:
        self.model.set_train_data(X, Y, strict=False)
        # except:
        #     self.__init__(X, Y, likelihood)

    def predict(self, X, return_std= False, return_cov = False, return_tensor=False):
        self.model.eval()
        self.likelihood.eval()
        if isinstance(X, np.ndarray):
            X = torch.tensor(X).float()
        if len(X.shape) == 1:
            X = torch.reshape(X, [1, -1])
        with gpytorch.settings.fast_pred_var():
            f_pred = self.model(X)
            if return_tensor:
                if return_std:
                    return f_pred.mean, f_pred.variance
                elif return_cov:
                    return f_pred.mean, f_pred.covariance_matrix
                else:
                    return f_pred.mean
            else:
                if return_std:
                    return f_pred.mean.detach().numpy(), f_pred.variance.detach().numpy()
                elif return_cov:
                    return f_pred.mean.detach().numpy(), f_pred.covariance_matrix.detach().numpy()
                else:
                    return f_pred.mean.detach().numpy()

    def sample_y(self, X, n_samples, random_state = None):
        rng = check_random_state(random_state)

        y_mean, y_cov = self.predict(X, return_cov=True)
        if y_mean.ndim == 1:
            y_samples = rng.multivariate_normal(y_mean, y_cov, n_samples).T
        else:
            y_samples = [
                rng.multivariate_normal(
                    y_mean[:, target], y_cov[..., target], n_samples
                ).T[:, np.newaxis]
                for target in range(y_mean.shape[1])
            ]
            y_samples = np.hstack(y_samples)
        return y_samples

    @property
    def y_train_(self):
        return self.model.train_targets.detach().numpy()

    @property
    def X_train_(self):
      return self.model.train_inputs[0].detach().numpy()

class BayesianOptimizationCentralized(bayesian_optimization):
    def __init__(self, domain, n_workers = 1,
                 kernel = kernels.RBF(), alpha=10**(-10),
                 acquisition_function = 'es', policy = 'greedy', 
                 epsilon = 0.01, regularization = None, regularization_strength = None,
                 pending_regularization = None, pending_regularization_strength = None,
                 grid_density = 100):

        super(BayesianOptimizationCentralized, self).__init__(domain=domain, n_workers=n_workers,
                 kernel=kernel, alpha=alpha,
                 acquisition_function=acquisition_function, policy = policy, 
                 epsilon = epsilon, regularization = regularization, regularization_strength = regularization_strength,
                 pending_regularization = pending_regularization, pending_regularization_strength = pending_regularization_strength,
                 grid_density = grid_density)
        self.diversity_penalty = False
        self.radius = 0.2
        
        self.X_train = []
        self.Y_train =[]
        self.X = []
        self.Y = []
        
        if acquisition_function == 'es':
            self._acquisition_function = self._entropy_search_grad
      
        else:
            print('Supported acquisition functions: es')
            return

    def _entropy_search_grad(self, a, x, n, radius=0.1):
        """
                Entropy search acquisition function.
                Args:
                    a: # agents
                    x: array-like, shape = [n_samples, n_hyperparams]
                    n: agent nums
                    projection: if project to a close circle
                    radius: circle of the projected circle
                """

        x = x.reshape(-1, self._dim)
        self.beta = 3 - 0.019 * n

        domain = self.domain

        mu, sigma = self.model.predict(x, return_std=True, return_tensor=True) # 11000
        ucb = mu + self.beta * sigma
        amaxucb = x[np.argmax(ucb.clone().detach().numpy())][np.newaxis, :]

        if self.n_workers > 1:
            init_x = np.random.normal(amaxucb, 1.0, (self.n_workers, self.domain.shape[0]))
        else:
            init_x = amaxucb

        x = torch.tensor(init_x, requires_grad=True,dtype=torch.float32) # optimize x
        optimizer = torch.optim.Adam([x], lr=0.01)
        training_iter = 200
        for i in range(training_iter):
            optimizer.zero_grad()
            joint_x = torch.vstack((x,torch.tensor(amaxucb).float()))
            cov_x_xucb = self.model.predict(joint_x, return_cov=True, return_tensor=True)[1][-1, :-1].reshape([-1,1])
            cov_x_x = self.model.predict(x, return_cov=True, return_tensor=True)[1]
            if self.diversity_penalty:
                penalty = []
                for i in itertools.combinations(range(self.n_workers - 1), 2):
                    penalty.append(torch.clip(- 1./100. * torch.log(torch.norm(x[i[0]] - x[i[1]]) - self.radius), 0., torch.inf))
                loss = -torch.matmul(torch.matmul(cov_x_xucb.T, torch.linalg.inv(cov_x_x + 0.01 * torch.eye(len(cov_x_x)))), cov_x_xucb) + sum(penalty)
            else:
                loss = -torch.matmul(
                    torch.matmul(cov_x_xucb.T, torch.linalg.inv(cov_x_x + 0.01 * torch.eye(len(cov_x_x)))), cov_x_xucb)
            loss.backward()
            optimizer.step()
            # project back to domain
            x = torch.where(x > torch.tensor(domain[:, 1]), torch.tensor(domain[:, 1]), x)
            x = torch.where(x < torch.tensor(domain[:, 0]), torch.tensor(domain[:, 0]), x)
            x.detach_()
        return x.clone().detach().numpy()

    def find_next_query(self, n, a, random_search=1000, decision_type='distributed'):
        """
        Proposes the next query.
        Arguments:
        ----------
            n: integer
                Iteration number.
            agent: integer
                Agent id to find next query for.
            model: sklearn model
                Surrogate model used for acqusition function
            random_search: integer.
                Number of random samples used to optimize the acquisition function. Default 1000
        """
        # Candidate set
        x = np.random.uniform(self.domain[:, 0], self.domain[:, 1], size=(random_search, self._dim))
        X = x[:]
        # Calculate acquisition function
        x = self._acquisition_function(a, X, n)
        return x

    def initialize(self, x0, y0, n_pre_samples=None):
        # Initial data
        if n_pre_samples is not None:
            for params in np.random.uniform(self.domain[:, 0], self.domain[:, 1], (n_pre_samples, self.domain.shape[0])): #np.random.uniform([-2, -1]), self.domain([2, 3]), (15, 2))
                self.X.append(params)
                self.Y.append(y0)
   
        # Change definition of x0 to be specfic for each agent
        for params in x0:
            self.X.append(params)
            self.Y.append(y0)
            
        self._initial_data_size = len(self.Y)

        # Standardize
        Y = self.scaler[0].fit_transform(np.array(self.Y).reshape(-1, 1)).squeeze()
        self.model = TorchGPModel(torch.tensor(self.X).float(), torch.tensor(Y).float())
        self.model.train()
    
    def train_gp(self, query_x, query_y):
        # parallel/centralized decision
        if query_x is not None:
            self.X = self.X + [q for q in query_x]
            self.Y = self.Y + [q for q in query_y]

            ## Fit GP 
            X = np.array(self.X)
            # Standardize
            Y = self.scaler[0].fit_transform(np.array(self.Y).reshape(-1, 1)).squeeze()
            # Fit surrogate
            self.model.fit(X, Y)
            self.model.train()
        return
            
    


            
