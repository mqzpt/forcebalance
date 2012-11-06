"""@package objective

ForceBalance objective function."""

from simtab import SimTab
from numpy import argsort, array, diag, dot, eye, linalg, ones, reshape, sum, zeros, exp, log
from collections import defaultdict
from collections import OrderedDict
from finite_difference import in_fd
from nifty import printcool_dictionary
from baseclass import ForceBalanceBaseClass

## This is the canonical lettering that corresponds to : objective function, gradient, Hessian.
Letters = ['X','G','H']

class Objective(ForceBalanceBaseClass):
    """ Objective function.
    
    The objective function is a combination of contributions from the different
    fitting simulations.  Basically, it loops through the fitting simulations,
    gets their contributions to the objective function and then sums all of them
    (although more elaborate schemes are conceivable).  The return value is the
    same data type as calling the fitting simulation itself: a dictionary containing
    the objective function, the gradient and the Hessian.

    The penalty function is also computed here; it keeps the parameters from straying
    too far from their initial values.

    @param[in] mvals The mathematical parameters that enter into computing the objective function
    @param[in] Order The requested order of differentiation
    @param[in] usepvals Switch that determines whether to use physical parameter values
    """
    def __init__(self, options, sim_opts, forcefield):

        super(Objective, self).__init__(options)
        self.set_option(options, 'penalty_type')
        self.set_option(options, 'penalty_additive')
        self.set_option(options, 'penalty_multiplicative')
        self.set_option(options, 'penalty_hyperbolic_b')
        self.set_option(options, 'normalize_weights')

        ## The list of fitting simulations
        #self.Simulations = [SimTab[opts['simtype']](options,opts,forcefield) for opts in sim_opts]
        self.Simulations = []
        for opts in sim_opts:
            Sim = SimTab[opts['simtype']](options,opts,forcefield)
            self.Simulations.append(Sim)
            printcool_dictionary(Sim.PrintOptionDict,"Setup for fitting simulation %s :" % Sim.name)
        # # Print the options for each simulation to the terminal.
        # for Sim in self.Simulations:
        #     printcool_dictionary(Sim.PrintOptionDict,"Setup for fitting simulation %s :" % Sim.name)
        ## The force field (it seems to be everywhere)
        self.FF = forcefield
        ## Initialize the penalty function.
        self.Penalty = Penalty(options['penalty_type'],forcefield,options['penalty_additive'],
                               options['penalty_multiplicative'],options['penalty_hyperbolic_b'],
                               options['penalty_alpha'])
        ## Obtain the denominator.
        if self.normalize_weights:
            self.WTot = sum([i.weight for i in self.Simulations])
        else:
            self.WTot = 1.0
        self.ObjDict = OrderedDict()
        self.ObjDict_Last = OrderedDict()

        printcool_dictionary(self.PrintOptionDict, "Setup for objective function :")

        
    def Simulation_Terms(self, mvals, Order=0, usepvals=False, verbose=False):
        ## This is the objective function; it's a dictionary containing the value, first and second derivatives
        Objective = {'X':0.0, 'G':zeros(self.FF.np), 'H':zeros((self.FF.np,self.FF.np))}
        # Loop through the simulations.
        for Sim in self.Simulations:
            # The first call is always done at the midpoint.
            Sim.bSave = True
            # List of functions that I can call.
            Funcs   = [Sim.get_X, Sim.get_G, Sim.get_H]
            # Call the appropriate function
            Ans = Funcs[Order](mvals)
            # Print out the qualitative indicators
            if verbose:
                Sim.indicate()
            # Note that no matter which order of function we call, we still increment the objective / gradient / Hessian the same way.
            if not in_fd():
                self.ObjDict[Sim.name] = {'w' : Sim.weight/self.WTot , 'x' : Ans['X']}
            for i in range(3):
                Objective[Letters[i]] += Ans[Letters[i]]*Sim.weight/self.WTot
        return Objective

    def Indicate(self):
        """ Print objective function contributions. """
        PrintDict = OrderedDict()
        Total = 0.0
        Change = False
        for key, val in self.ObjDict.items():
            color = "\x1b[97m"
            if key in self.ObjDict_Last:
                if self.ObjDict[key] <= self.ObjDict_Last[key]:
                    Change = True
                    color = "\x1b[92m"
                elif self.ObjDict[key] > self.ObjDict_Last[key]:
                    Change = True
                    color = "\x1b[91m"
            PrintDict[key] = "% 12.5f % 10.3f %s% 16.5e%s" % (val['x'],val['w'],color,val['x']*val['w'],"\x1b[0m")
            if Change:
                xnew = self.ObjDict[key]['x'] * self.ObjDict[key]['w']
                xold = self.ObjDict_Last[key]['x'] * self.ObjDict_Last[key]['w']
                PrintDict[key] += " ( %+10.3e )" % (xnew - xold)
            Total += val['x']*val['w']
        if Change:
            Title = "Objective Function Breakdown, Total = % .5e\n %-20s %55s" % (Total, "Simulation Name", "Residual  x  Weight  =  Contribution (Current-Last)")
        else:
            Title = "Objective Function Breakdown, Total = % .5e\n %-20s %40s" % (Total, "Simulation Name", "Residual  x  Weight  =  Contribution")
        printcool_dictionary(PrintDict,color=6,title=Title)
        for key, val in self.ObjDict.items():
            self.ObjDict_Last[key] = val
        return

    def Full(self, mvals, Order=0, usepvals=False, verbose=False):
        Objective = self.Simulation_Terms(mvals, Order, usepvals, verbose)
        ## Compute the penalty function.
        Extra = self.Penalty.compute(mvals,Objective)
        Objective['X0'] = Objective['X']
        Objective['G0'] = Objective['G'].copy()
        Objective['H0'] = Objective['H'].copy()
        if not in_fd():
            self.ObjDict['Regularization'] = {'w' : 1.0, 'x' : Extra[0]}
            if verbose:
                self.Indicate()
        for i in range(3):
            Objective[Letters[i]] += Extra[i]
        return Objective

class Penalty:
    """ Penalty functions for regularizing the force field optimizer.

    The purpose for this module is to improve the behavior of our optimizer;
    essentially, our problem is fraught with 'linear dependencies', a.k.a.
    directions in the parameter space that the objective function does not
    respond to.  This would happen if a parameter is just plain useless, or
    if there are two or more parameters that describe the same thing.

    To accomplish these objectives, a penalty function is added to the
    objective function.  Generally, the more the parameters change (i.e.
    the greater the norm of the parameter vector), the greater the
    penalty.  Note that this is added on after all of the other
    contributions have been computed.  This only matters if the penalty
    'multiplies' the objective function: Obj + Obj*Penalty, but we also
    have the option of an additive penalty: Obj + Penalty.

    Statistically, this is called regularization.  If the penalty function
    is the norm squared of the parameter vector, it is called ridge regression.
    There is also the option of using simply the norm, and this is called lasso,
    but I think it presents problems for the optimizer that I need to work out.

    Note that the penalty functions can be considered as part of a 'maximum
    likelihood' framework in which we assume a PRIOR PROBABILITY of the
    force field parameters around their initial values.  The penalty function
    is related to the prior by an exponential.  Ridge regression corresponds
    to a Gaussian prior and lasso corresponds to an exponential prior.  There
    is also 'elastic net regression' which interpolates between Gaussian
    and exponential using a tuning parameter.

    Our priors are adjustable too - there is one parameter, which is the width
    of the distribution.  We can even use a noninformative prior for the
    distribution widths (hyperprior!).  These are all important things to
    consider later.

    Importantly, note that here there is no code that treats the distribution
    width.  That is because the distribution width is wrapped up in the
    rescaling factors, which is essentially a coordinate transformation
    on the parameter space.  More documentation on this will follow, perhaps
    in the 'rsmake' method.

    """
    def __init__(self, User_Option, ForceField, Factor_Add=0.0, Factor_Mult=0.0, Factor_B=0.1, Alpha=1.0):
        self.fadd = Factor_Add
        self.fmul = Factor_Mult
        self.a    = Alpha
        self.b    = Factor_B
        self.FF   = ForceField
        self.Pen_Names = {'HYP' : 1, 'HYPER' : 1, 'HYPERBOLIC' : 1, 'L1' : 1, 'HYPERBOLA' : 1,
                          'PARA' : 2, 'PARABOLA' : 2, 'PARABOLIC' : 2, 'L2': 2, 'QUADRATIC' : 2,
                          'FUSE' : 3, 'FUSION' : 3, 'FUSE_L0' : 4, 'FUSION_L0' : 4}
        self.ptyp = self.Pen_Names[User_Option.upper()]
        self.Pen_Tab = {1 : self.HYP, 2: self.L2_norm, 3: self.FUSE, 4:self.FUSE_L0}
        if User_Option.upper() == 'L1':
            print "L1 norm uses the hyperbolic penalty, make sure penalty_hyperbolic_b is set sufficiently small"
        elif self.ptyp == 1:
            print "Using hyperbolic regularization (Laplacian prior) with strength %.1e (+), %.1e (x) and tightness %.1e" % (Factor_Add, Factor_Mult, Factor_B)
        elif self.ptyp == 2:
            print "Using parabolic regularization (Gaussian prior) with strength %.1e (+), %.1e (x)" % (Factor_Add, Factor_Mult)
        elif self.ptyp == 3:
            print "Using FUSION PENALTY (only relevant for basis set optimizations at the moment) with strength %.1e" % Factor_Add
        elif self.ptyp == 4:
            print "Using L0-L1 FUSION PENALTY (only relevant for basis set optimizations at the moment) with strength %.1e and switching distance %.1e" % (Factor_Add, Alpha)

    def compute(self, mvals, Objective):
        X = Objective['X']
        G = Objective['G']
        H = Objective['H']
        np = len(mvals)
        K0, K1, K2 = self.Pen_Tab[self.ptyp](mvals)
        XAdd = 0.0
        GAdd = zeros(np, dtype=float)
        HAdd = zeros((np, np), dtype=float)
        if self.fadd > 0.0:
            XAdd += K0 * self.fadd
            GAdd += K1 * self.fadd
            HAdd += K2 * self.fadd
        if self.fmul > 0.0:
            XAdd += ( X*K0 ) * self.fmul
            GAdd += array( G*K0 + X*K1 ) * self.fmul
            GK1 = reshape(G, (1, -1))*reshape(K1, (-1, 1))
            K1G = reshape(K1, (1, -1))*reshape(G, (-1, 1))
            HAdd += array( H*K0+GK1+K1G+X*K2 ) * self.fmul
        return XAdd, GAdd, HAdd

    def L2_norm(self, mvals):
        """
        Harmonic L2-norm constraints.  These are the ones that I use
        the most often to regularize my optimization.

        @param[in] mvals The parameter vector
        @return DC0 The norm squared of the vector
        @return DC1 The gradient of DC0
        @return DC2 The Hessian (just a constant)

        """
        mvals = array(mvals)
        DC0 = dot(mvals, mvals)
        DC1 = 2*array(mvals)
        DC2 = 2*eye(len(mvals))
        return DC0, DC1, DC2

    def HYP(self, mvals):
        """
        Hyperbolic constraints.  Depending on the 'b' parameter, the smaller it is,
        the closer we are to an L1-norm constraint.  If we use these, we expect
        a properly-behaving optimizer to make several of the parameters very nearly zero
        (which would be cool).

        @param[in] mvals The parameter vector
        @return DC0 The hyperbolic penalty
        @return DC1 The gradient
        @return DC2 The Hessian

        """
        mvals = array(mvals)
        np = len(mvals)
        DC0   = sum((mvals**2 + self.b**2)**0.5 - self.b)
        DC1   = mvals*(mvals**2 + self.b**2)**-0.5
        DC2   = diag(self.b**2*(mvals**2 + self.b**2)**-1.5)
        return DC0, DC1, DC2

    def FUSE(self, mvals):
        Groups = defaultdict(list)
        for p, pid in enumerate(self.FF.plist):
            if 'Exponent' not in pid or len(pid.split()) != 1:
                warn_press_key("Fusion penalty currently implemented only for basis set optimizations, where parameters are like this: Exponent:Elem=H,AMom=D,Bas=0,Con=0")
            Data = dict([(i.split('=')[0],i.split('=')[1]) for i in pid.split(':')[1].split(',')])
            if 'Con' not in Data or Data['Con'] != '0':
                warn_press_key("More than one contraction coefficient found!  You should expect the unexpected")
            key = Data['Elem']+'_'+Data['AMom']
            Groups[key].append(p)
        pvals = self.FF.create_pvals(mvals)
        DC0 = 0.0
        DC1 = zeros(self.FF.np, dtype=float)
        DC2 = zeros(self.FF.np, dtype=float)
        for gnm, pidx in Groups.items():
            # The group of parameters for a particular element / angular momentum.
            pvals_grp = pvals[pidx]
            # The order that the parameters come in.
            Order = argsort(pvals_grp)
            # The number of nearest neighbor pairs.
            #print Order
            for p in range(len(Order) - 1):
                # The pointers to the parameter indices.
                pi = pidx[Order[p]]
                pj = pidx[Order[p+1]]
                # pvals[pi] is the SMALLER parameter.
                # pvals[pj] is the LARGER parameter.
                dp = log(pvals[pj]) - log(pvals[pi])
                DC0     += (dp**2 + self.b**2)**0.5 - self.b
                DC1[pi] -= dp*(dp**2 + self.b**2)**-0.5
                DC1[pj] += dp*(dp**2 + self.b**2)**-0.5
                # The second derivatives have off-diagonal terms,
                # but we're not using them right now anyway
                DC2[pi] -= self.b**2*(dp**2 + self.b**2)**-1.5
                DC2[pj] += self.b**2*(dp**2 + self.b**2)**-1.5
                #print "pvals[%i] = %.4f, pvals[%i] = %.4f dp = %.4f" % (pi, pvals[pi], pj, pvals[pj], dp), 
                #print "First Derivative = % .4f, Second Derivative = % .4f" % (dp*(dp**2 + self.b**2)**-0.5, self.b**2*(dp**2 + self.b**2)**-1.5)
        return DC0, DC1, diag(DC2)

    def FUSE_L0(self, mvals):
        Groups = defaultdict(list)
        for p, pid in enumerate(self.FF.plist):
            if 'Exponent' not in pid or len(pid.split()) != 1:
                warn_press_key("Fusion penalty currently implemented only for basis set optimizations, where parameters are like this: Exponent:Elem=H,AMom=D,Bas=0,Con=0")
            Data = dict([(i.split('=')[0],i.split('=')[1]) for i in pid.split(':')[1].split(',')])
            if 'Con' not in Data or Data['Con'] != '0':
                warn_press_key("More than one contraction coefficient found!  You should expect the unexpected")
            key = Data['Elem']+'_'+Data['AMom']
            Groups[key].append(p)
        pvals = self.FF.create_pvals(mvals)
        #print "pvals: ", pvals
        DC0 = 0.0
        DC1 = zeros(self.FF.np, dtype=float)
        DC2 = zeros((self.FF.np,self.FF.np), dtype=float)
        for gnm, pidx in Groups.items():
            # The group of parameters for a particular element / angular momentum.
            pvals_grp = pvals[pidx]
            # The order that the parameters come in.
            Order = argsort(pvals_grp)
            # The number of nearest neighbor pairs.
            #print Order
            Contribs = []
            dps = []
            for p in range(len(Order) - 1):
                # The pointers to the parameter indices.
                pi = pidx[Order[p]]
                pj = pidx[Order[p+1]]
                # pvals[pi] is the SMALLER parameter.
                # pvals[pj] is the LARGER parameter.
                dp = log(pvals[pj]) - log(pvals[pi])
                dp2b2 = dp**2 + self.b**2
                h   = self.a*((dp2b2)**0.5 - self.b)
                hp  = self.a*(dp*(dp2b2)**-0.5)
                hpp = self.a*(self.b**2*(dp2b2)**-1.5)
                emh = exp(-1*h)
                dps.append(dp)
                Contribs.append((1.0 - emh))
                DC0     += (1.0 - emh)
                DC1[pi] -= hp*emh
                DC1[pj] += hp*emh
        # for i in self.FF.redirect:
        #     p = mvals[i]
        #     DC0 += 1e-6*p*p
        #     DC1[i] = 2e-6*p
            
                # The second derivatives have off-diagonal terms,
                # but we're not using them right now anyway
                #DC2[pi,pi] += (hpp - hp**2)*emh
                #DC2[pi,pj] -= (hpp - hp**2)*emh
                #DC2[pj,pi] -= (hpp - hp**2)*emh
                #DC2[pj,pj] += (hpp - hp**2)*emh
                #print "pvals[%i] = %.4f, pvals[%i] = %.4f dp = %.4f" % (pi, pvals[pi], pj, pvals[pj], dp), 
                #print "First Derivative = % .4f, Second Derivative = % .4f" % (dp*(dp**2 + self.b**2)**-0.5, self.b**2*(dp**2 + self.b**2)**-1.5)
            
            #print "grp:", gnm, "dp:", ' '.join(["% .1e" % i for i in dps]), "Contributions:", ' '.join(["% .1e" % i for i in Contribs])

        #print DC0, DC1, DC2
        #print pvals
        #raw_input()

        return DC0, DC1, DC2

    #return self.HYP(mvals)
