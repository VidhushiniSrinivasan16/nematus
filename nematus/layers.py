'''
Layer definitions
'''

import json
import cPickle as pkl
import numpy
from collections import OrderedDict

import theano
import theano.tensor as tensor
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

from initializers import *
from util import *
from theano_util import *
from alignment_util import *

from training_monitor import *

#from theano import printing

# layers: 'name': ('parameter initializer', 'feedforward')
layers = {'ff': ('param_init_fflayer', 'fflayer'),
          'creluff': ('param_init_crelufflayer', 'crelufflayer'),
          'preluff': ('param_init_prelufflayer', 'prelufflayer'),
          'gru': ('param_init_gru', 'gru_layer'),
          'gru_cond': ('param_init_gru_cond', 'gru_cond_layer'),
          'crelurhn_cond': ('param_init_crelurhn_cond', 'crelurhn_cond_layer'),
          'embedding': ('param_init_embedding_layer', 'embedding_layer'),
          'tanh_attention' : ('param_init_tanh_attention', 'tanh_attention'),
          'crelu_attention' : ('param_init_crelu_attention', 'crelu_attention'),
          }


def dropout_constr(options, use_noise, trng, sampling):
    """This constructor takes care of the fact that we want different
    behaviour in training and sampling, and keeps backward compatibility:
    on older versions, activations need to be rescaled at test time;
    on newer veresions, they are rescaled at training time.
    """

    # if dropout is off, or we don't need it because we're sampling, multiply by 1
    # this is also why we make all arguments optional
    def get_layer(shape=None, dropout_probability=0, num=1):
        if num > 1:
            return theano.shared(numpy.array([1.]*num, dtype=floatX))
        else:
            return theano.shared(numpy_floatX(1.))

    if options['use_dropout']:
        # models trained with old dropout need to be rescaled at test time
        if sampling and options['model_version'] < 0.1:
            def get_layer(shape=None, dropout_probability=0, num=1):
                if num > 1:
                    return theano.shared(numpy.array([1-dropout_probability]*num, dtype=floatX))
                else:
                    return theano.shared(numpy_floatX(1-dropout_probability))
        elif not sampling:
            if options['model_version'] < 0.1:
                scaled = False
            else:
                scaled = True
            def get_layer(shape, dropout_probability=0, num=1):
                if num > 1:
                    return shared_dropout_layer((num,) + shape, use_noise, trng, 1-dropout_probability, scaled)
                else:
                    return shared_dropout_layer(shape, use_noise, trng, 1-dropout_probability, scaled)

    return get_layer


def get_layer_param(name):
    param_fn, constr_fn = layers[name]
    return eval(param_fn)

def get_layer_constr(name):
    param_fn, constr_fn = layers[name]
    return eval(constr_fn)

# dropout that will be re-used at different time steps
def shared_dropout_layer(shape, use_noise, trng, value, scaled=True):
    #re-scale dropout at training time, so we don't need to at test time
    if scaled:
        proj = tensor.switch(
            use_noise,
            trng.binomial(shape, p=value, n=1,
                                        dtype=floatX)/value,
            theano.shared(numpy_floatX(1.)))
    else:
        proj = tensor.switch(
            use_noise,
            trng.binomial(shape, p=value, n=1,
                                        dtype=floatX),
            theano.shared(numpy_floatX(value)))
    return proj

# layer normalization
# code from https://github.com/ryankiros/layer-norm
def layer_norm(x, b, s, monitor_mean_and_std=False, training_monitor=the_training_monitor):
    _eps = numpy_floatX(1e-5)
    mean_val, std_val = None, None
    if x.ndim == 3:
        mean_val = x.mean(2)[:,:,None]
        std_val = tensor.sqrt((x.var(2)[:,:,None] + _eps))
        output = (x - mean_val) / std_val
        if s != None:
            output = s[None, None, :] * output
        if b != None:
          output += b[None, None,:]
    else:
        mean_val = x.mean(1)[:,None]
        std_val = tensor.sqrt((x.var(1)[:,None] + _eps))
        output = (x - mean_val) / std_val
        if s != None:
            output = s[None, :] * output
        if b != None:
          output += b[None,:]

    if monitor_mean_and_std:
        name = s.name if s is not None else ""
        if not (name.startswith("encoder") or name.startswith("decoder")):
            avg_mean_val, avg_std_val = mean_val.mean(), std_val.mean()
            avg_mean_val.name = name+"_mean"
            avg_std_val.name = name+"_std"
            training_monitor.add_monitor(avg_mean_val)
            training_monitor.add_monitor(avg_std_val)

    return output

def weight_norm(W, s):
    """
    Normalize the columns of a matrix
    """
    _eps = numpy_floatX(1e-5)
    W_norms = tensor.sqrt((W * W).sum(axis=0, keepdims=True) + _eps)
    W_norms_s = W_norms * s # do this first to ensure proper broadcasting
    return W / W_norms_s

# feedforward layer: affine transformation + point-wise nonlinearity
def param_init_fflayer(options, params, prefix='ff', nin=None, nout=None,
                       ortho=True, weight_matrix=True, bias=True, followed_by_softmax=False):
    if nin is None:
        nin = options['dim_proj']
    if nout is None:
        nout = options['dim_proj']
    if weight_matrix:
        params[pp(prefix, 'W')] = norm_weight(nin, nout, scale=0.01, ortho=ortho)
    if bias:
       params[pp(prefix, 'b')] = numpy.zeros((nout,)).astype(floatX)

    if options['layer_normalisation'] and not followed_by_softmax:
        scale_add = 0.0
        scale_mul = 1.0
        params[pp(prefix,'ln_b')] = scale_add * numpy.ones((1*nout)).astype(floatX)
        params[pp(prefix,'ln_s')] = scale_mul * numpy.ones((1*nout)).astype(floatX)

    if options['weight_normalisation'] and not followed_by_softmax:
        scale_mul = 1.0
        params[pp(prefix,'W_wns')] = scale_mul * numpy.ones((1*nout)).astype(floatX)

    return params


def fflayer(tparams, state_below, options, dropout, prefix='rconv',
            activ='lambda x: tensor.tanh(x)', W=None, b=None, dropout_probability=0, followed_by_softmax=False, training_monitor=the_training_monitor, **kwargs):
    if W == None:
        W = tparams[pp(prefix, 'W')]
    if b == None:
        b = tparams[pp(prefix, 'b')]

    # for three-dimensional tensors, we assume that first dimension is number of timesteps
    # we want to apply same mask to all timesteps
    if state_below.ndim == 3:
        dropout_shape = (state_below.shape[1], state_below.shape[2])
    else:
        dropout_shape = state_below.shape
    dropout_mask = dropout(dropout_shape, dropout_probability)

    if options['weight_normalisation'] and not followed_by_softmax:
         W = weight_norm(W, tparams[pp(prefix, 'W_wns')])
    preact = tensor.dot(state_below*dropout_mask, W) + b

    if options['monitor_ff_layers']:
        preact_mean = preact.mean()
        preact_mean.name = prefix+'_mean'
        training_monitor.add_monitor(preact_mean)
        preact_std = preact.std(axis=-1).mean()
        preact_std.name = prefix+'_std'
        training_monitor.add_monitor(preact_std)

    if options['layer_normalisation'] and not followed_by_softmax:
        preact = layer_norm(preact, tparams[pp(prefix,'ln_b')], tparams[pp(prefix,'ln_s')])

    return eval(activ)(preact)

# prelu feedforward layer (activation-only)
def param_init_prelufflayer(options, params, prefix='preluff', nin=None,
                            followed_by_softmax=False, forced_layernorm=False, layernorm_has_scaling=True, layernorm_has_bias=True):
    if nin is None:
        nin = options['dim_proj']
    params[pp(prefix,'prelu_p')] = -1.0 * numpy.ones((1*nin)).astype(floatX)    # Looks linear initialization (Balduzzi et al. 2017)
    has_layernorm = (options['layer_normalisation'] and not followed_by_softmax) or forced_layernorm
    if has_layernorm:
        if layernorm_has_bias:
            params[pp(prefix,'ln_b')] = numpy.zeros((1*nin)).astype(floatX)
        if layernorm_has_scaling:
            params[pp(prefix,'ln_s')] = numpy.ones((1*nin)).astype(floatX)
    return params

def prelufflayer(tparams, state_below, options, dropout=None, prefix='preluff', p=None, followed_by_softmax=False, forced_layernorm=False, **kwargs):
    if p == None:
        p = tparams[pp(prefix, 'prelu_p')]
    prelu_pos = tensor.nnet.relu(state_below)
    prelu_neg = tensor.nnet.relu(-state_below)
    rv = prelu_pos + p * prelu_neg
    has_layernorm = (options['layer_normalisation'] and not followed_by_softmax) or forced_layernorm
    if has_layernorm:
        ln_b = tparams.get(pp(prefix,'ln_b'), None)
        ln_s = tparams.get(pp(prefix,'ln_s'), None)
        rv = layer_norm(rv, ln_b, ln_s)
    return rv

    
# crelu feedforward layer
def param_init_crelufflayer(options, params, prefix='creluff', nin=None, nout=None,
                       ortho=True, weight_matrix=True, bias=True, followed_by_softmax=False):
    if nin is None:
        nin = options['dim_proj']
    if nout is None:
        nout = options['dim_proj']
    if weight_matrix:
        params[pp(prefix, 'W_pos')] = norm_weight(nin, nout, scale='glorot_inout', ortho=ortho)
        params[pp(prefix, 'W_neg')] = -params[pp(prefix, 'W_pos')]  # Looks linear initialization (Balduzzi et al. 2017)
    if bias:
       params[pp(prefix, 'b')] = numpy.zeros((nout,)).astype(floatX)

    if options['layer_normalisation'] and not followed_by_softmax:
        scale_add = 0.0
        scale_mul = 1.0
        params[pp(prefix,'ln_b')] = scale_add * numpy.ones((1*nout)).astype(floatX)
        params[pp(prefix,'ln_s')] = scale_mul * numpy.ones((1*nout)).astype(floatX)

    if options['weight_normalisation'] and not followed_by_softmax:
        scale_mul = 1.0
        params[pp(prefix,'W_pos_wns')] = scale_mul * numpy.ones((1*nout)).astype(floatX)
        params[pp(prefix,'W_neg_wns')] = scale_mul * numpy.ones((1*nout)).astype(floatX)

    return params


def crelufflayer(tparams, state_below, options, dropout, prefix='creluff', W_pos=None, W_neg=None, b=None, dropout_probability=0, followed_by_softmax=False, training_monitor=the_training_monitor, **kwargs):
    if W_pos == None:
        W_pos = tparams[pp(prefix, 'W_pos')]
    if W_neg == None:
        W_neg = tparams[pp(prefix, 'W_neg')]
    if b == None:
        b = tparams[pp(prefix, 'b')]

    # for three-dimensional tensors, we assume that first dimension is number of timesteps
    # we want to apply same mask to all timesteps
    if state_below.ndim == 3:
        dropout_shape = (state_below.shape[1], state_below.shape[2])
    else:
        dropout_shape = state_below.shape
    dropout_mask = dropout(dropout_shape, dropout_probability)

    if options['weight_normalisation'] and not followed_by_softmax:
         W_pos = weight_norm(W_pos, tparams[pp(prefix, 'W_pos_wns')])
         W_neg = weight_norm(W_neg, tparams[pp(prefix, 'W_neg_wns')])

    state_below_dr = state_below*dropout_mask
    state_below_dr.name = pp(prefix, 'state_below_dr')
    crelu_pos = tensor.nnet.relu(state_below_dr)
    crelu_neg = tensor.nnet.relu(-state_below_dr)
    preact = tensor.dot(crelu_pos, W_pos) + tensor.dot(crelu_neg, W_neg) + b

    if options['monitor_ff_layers']:
        preact_mean = preact.mean()
        preact_mean.name = prefix+'_mean'
        training_monitor.add_monitor(preact_mean)
        preact_std = preact.std(axis=-1).mean()
        preact_std.name = prefix+'_std'
        training_monitor.add_monitor(preact_std)

    if options['layer_normalisation'] and not followed_by_softmax:
        preact = layer_norm(preact, tparams[pp(prefix,'ln_b')], tparams[pp(prefix,'ln_s')])

    preact.name = pp(prefix, 'preact')
    return preact

# embedding layer
def param_init_embedding_layer(options, params, n_words, dims, factors=None, prefix='', suffix=''):
    if factors == None:
        factors = 1
        dims = [dims]
    for factor in xrange(factors):
        params[prefix+embedding_name(factor)+suffix] = norm_weight(n_words, dims[factor])
    return params

def embedding_layer(tparams, ids, factors=None, prefix='', suffix=''):
    do_reshape = False
    if factors == None:
        if ids.ndim > 1:
            do_reshape = True
            n_timesteps = ids.shape[0]
            n_samples = ids.shape[1]
        emb = tparams[prefix+embedding_name(0)+suffix][ids.flatten()]
    else:
        if ids.ndim > 2:
          do_reshape = True
          n_timesteps = ids.shape[1]
          n_samples = ids.shape[2]
        emb_list = [tparams[prefix+embedding_name(factor)+suffix][ids[factor].flatten()] for factor in xrange(factors)]
        emb = concatenate(emb_list, axis=1)
    if do_reshape:
        emb = emb.reshape((n_timesteps, n_samples, -1))

    return emb

# GRU layer
def param_init_gru(options, params, prefix='gru', nin=None, dim=None,
                   recurrence_transition_depth=1,
                   reset_gate=True,
                   main_activation='tanh',
                   main_recurrent_identity_init=False,
                   post_activation_input=False,
                   zero_init_main_input=False,
                   zero_init_main_state=False,
                   gate_negativity=0.0,
                   **kwargs):
    if nin is None:
        nin = options['dim_proj']
    if dim is None:
        dim = options['dim_proj']

    scale_add = 0.0
    scale_mul = 1.0
    scale_mul_main_in = 0.0 if zero_init_main_input else scale_mul
    scale_add_gates = numpy.log(gate_negativity) if zero_init_main_state and (gate_negativity != 0.0) else scale_add
    scale_mul_gates = 0.0 if zero_init_main_state else scale_mul

    n_aux_gates = 2 if reset_gate else 1

    for i in xrange(recurrence_transition_depth):
        suffix = '' if i == 0 else ('_drt_%s' % i)
        # recurrent transformation weights for gates
        params[pp(prefix, 'b'+suffix)] = numpy.zeros((n_aux_gates * dim,)).astype(floatX)
        U = numpy.concatenate([ortho_weight(dim), ortho_weight(dim)], axis=1) if reset_gate else ortho_weight(dim)
        params[pp(prefix, 'U'+suffix)] = U
        # recurrent transformation weights for hidden state proposal
        params[pp(prefix, 'bx'+suffix)] = numpy.zeros((dim,)).astype(floatX)
        Ux = ortho_weight(dim)
        params[pp(prefix, 'Ux'+suffix)] = Ux

        if main_activation == 'tanh':
            pass
        elif main_activation == 'crelu':
            params[pp(prefix, 'Ux_post_pos'+suffix)] = Ux.T
            params[pp(prefix, 'Ux_post_neg'+suffix)] = -Ux.T
        else:
            assert False, 'Unknown main activation %s' % main_activation

        if options['layer_normalisation']:
            params[pp(prefix,'U%s_lnb' % suffix)] = scale_add_gates * numpy.ones((n_aux_gates*dim)).astype(floatX)
            params[pp(prefix,'U%s_lns' % suffix)] = scale_mul_gates * numpy.ones((n_aux_gates*dim)).astype(floatX)
            params[pp(prefix,'Ux%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Ux%s_lns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            if main_activation == 'crelu':
                params[pp(prefix,'Ux_post%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Ux_post%s_lns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)

        if options['weight_normalisation']:
            params[pp(prefix,'U%s_wns' % suffix)] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
            params[pp(prefix,'Ux%s_wns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            if main_activation == 'crelu':
                params[pp(prefix,'Ux_post_pos%s_wns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Ux_post_neg%s_wns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)

        if i == 0:
            # embedding to gates transformation weights, biases
            W = numpy.concatenate([norm_weight(nin, dim), norm_weight(nin, dim)], axis=1) if reset_gate else norm_weight(nin, dim)
            params[pp(prefix, 'W'+suffix)] = W
            # embedding to hidden state proposal weights, biases

            if zero_init_main_input and not options['layer_normalisation']:
                Wx = numpy.zeros((nin, dim)).astype(floatX)
            else:
                Wx = norm_weight(nin, dim)
            params[pp(prefix, 'Wx'+suffix)] = Wx

            if post_activation_input:
                Wx_post = -Wx # Looks-linear initialization
                params[pp(prefix, 'Wx_post')] = Wx_post
                params[pp(prefix, 'bx_post')] = numpy.zeros((dim,)).astype(floatX)

            if options['layer_normalisation']:
                params[pp(prefix,'W%s_lnb' % suffix)] = scale_add * numpy.ones((n_aux_gates*dim)).astype(floatX)
                params[pp(prefix,'W%s_lns' % suffix)] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
                params[pp(prefix,'Wx%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wx%s_lns' % suffix)] = scale_mul_main_in * numpy.ones((1*dim)).astype(floatX)
                if post_activation_input:
                   params[pp(prefix,'Wx_post%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
                   params[pp(prefix,'Wx_post%s_lns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            if options['weight_normalisation']:
                params[pp(prefix,'W%s_wns' % suffix)] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
                params[pp(prefix,'Wx%s_wns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                if post_activation_input:
                    params[pp(prefix,'Wx_post%s_wns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)

    return params


def gru_layer(tparams, state_below, options, dropout, prefix='gru',
              mask=None, one_step=False,
              init_state=None,
              dropout_probability_below=0,
              dropout_probability_rec=0,
              recurrence_transition_depth=1,
              truncate_gradient=-1,
              reset_gate=True,
              main_activation='tanh',
              gate_negativity=0.0,
              profile=False,
              **kwargs):

    if one_step:
        assert init_state, 'previous state must be provided'

    nsteps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
        dim_below = state_below.shape[2]
    else:
        n_samples = 1
        dim_below = state_below.shape[1]

    dim = tparams[pp(prefix, 'Ux')].shape[1]

    post_activation_input = pp(prefix, 'Wx_post') in tparams

    # utility function to look up parameters and apply weight normalization if enabled
    def wn(param_name):
        param = tparams[param_name]
        if options['weight_normalisation']: 
            return weight_norm(param, tparams[param_name+'_wns'])
        else:
            return param

    # initial/previous state
    if init_state is None:
        init_state = tensor.zeros((n_samples, dim))

    if mask is None:
        mask = tensor.ones((state_below.shape[0], 1))

    below_dropout = dropout((n_samples, dim_below), dropout_probability_below, num=2)
    rec_dropout = dropout((n_samples, dim), dropout_probability_rec, num=2*(recurrence_transition_depth))

    # utility function to slice a tensor
    def _slice(_x, n, dim):
        if _x.ndim == 3:
            return _x[:, :, n*dim:(n+1)*dim]
        return _x[:, n*dim:(n+1)*dim]

    state_below_list, state_belowx_list, state_belowx_post_list = [], [], []

    # state_below is the input word embeddings
    # input to the gates, concatenated
    state_below_ = tensor.dot(state_below*below_dropout[0], wn(pp(prefix, 'W'))) + tparams[pp(prefix, 'b')]
    # input to compute the hidden state proposal
    state_belowx = tensor.dot(state_below*below_dropout[1], wn(pp(prefix, 'Wx'))) + tparams[pp(prefix, 'bx')]
    if options['layer_normalisation']:
        state_below_ = layer_norm(state_below_, tparams[pp(prefix, 'W_lnb')], tparams[pp(prefix, 'W_lns')])
        state_belowx = layer_norm(state_belowx, tparams[pp(prefix, 'Wx_lnb')], tparams[pp(prefix, 'Wx_lns')])
    state_below_list.append(state_below_)
    state_belowx_list.append(state_belowx)

    if post_activation_input:
        state_belowx_post = tensor.dot(state_below*below_dropout[1], wn(pp(prefix, 'Wx_post'))) + tparams[pp(prefix, 'bx_post')]
        if options['layer_normalisation']:
            state_belowx_post = layer_norm(state_belowx_post, tparams[pp(prefix, 'Wx_post_lnb')], tparams[pp(prefix, 'Wx_post_lns')])
        state_belowx_post_list.append(state_belowx_post)


    # step function to be used by scan
    # arguments    | sequences |outputs-info| non-seqs
    def _step_slice(*args):
        n_ins = 1
        m_ = args[0]
        x_list = args[1:1+n_ins]
        xx_list = args[1+n_ins:1+2*n_ins]
        if post_activation_input:
            xx_post_list = args[1+2*n_ins:1+3*n_ins]
        h_, rec_dropout = args[-2], args[-1]

        h_prev = h_
        for i in xrange(recurrence_transition_depth):
            suffix = '' if i == 0 else ('_drt_%s' % i)
            if i == 0:
                x_cur = x_list[i]
                xx_cur = xx_list[i]
                if post_activation_input:
                    xx_post_cur = xx_post_list[i]
            else:
                x_cur = tparams[pp(prefix, 'b'+suffix)]
                xx_cur = tparams[pp(prefix, 'bx'+suffix)]
                xx_post_cur = 0

            preact = tensor.dot(h_prev*rec_dropout[0+2*i], wn(pp(prefix, 'U'+suffix)))
            if options['layer_normalisation']:
                preact = layer_norm(preact, tparams[pp(prefix, 'U%s_lnb' % suffix)], tparams[pp(prefix, 'U%s_lns' % suffix)])
            preact += x_cur

            # reset and update gates
            gates_act = tensor.nnet.sigmoid(preact)
            if gate_negativity != 0.0: 	# Does theano already optimize this?
                gates_act = (1.0 + gate_negativity) * gates_act - gate_negativity
            if reset_gate:
                r = _slice(gates_act, 0, dim)
                u = _slice(gates_act, 1, dim)
            else:
                r = 1
                u = gates_act

            # compute the hidden state proposal
            preactx = tensor.dot(h_prev*rec_dropout[1+2*i], wn(pp(prefix, 'Ux'+suffix)))
            if options['layer_normalisation']:
                preactx = layer_norm(preactx, tparams[pp(prefix, 'Ux%s_lnb' % suffix)], tparams[pp(prefix, 'Ux%s_lns' % suffix)])
            preactx = preactx * r
            preactx = preactx + xx_cur

            if main_activation == 'tanh':
                h = tensor.tanh(preactx)
            elif main_activation == 'crelu':
                h_pos = tensor.nnet.relu(preactx)
                h_neg = tensor.nnet.relu(-preactx)
        
            if post_activation_input:
                if main_activation == 'crelu':
                    h_pos += xx_post_cur / 2.0
                    h_neg -= xx_post_cur / 2.0
                else:
                    h += xx_post_cur
        
            if main_activation == 'crelu': # Notice that this goes after the post activation input
                h = tensor.dot(h_pos, wn(pp(prefix, 'Ux_post_pos%s' % suffix))) + tensor.dot(h_neg, wn(pp(prefix, 'Ux_post_neg%s' % suffix)))
                if options['layer_normalisation']:
                    h = layer_norm(h, tparams[pp(prefix, 'Ux_post%s_lnb' % suffix)], tparams[pp(prefix, 'Ux_post%s_lns' % suffix)])


            # leaky integrate and obtain next hidden state
            h = u * h_prev + (1. - u) * h
            h = m_[:, None] * h + (1. - m_)[:, None] * h_prev
            h_prev = h

        return h

    # prepare scan arguments
    seqs = [mask] + state_below_list + state_belowx_list + state_belowx_post_list
    _step = _step_slice
    shared_vars = [rec_dropout]

    if one_step:
        rval = _step(*(seqs + [init_state] + shared_vars))
    else:
        rval, updates = theano.scan(_step,
                                sequences=seqs,
                                outputs_info=init_state,
                                non_sequences=shared_vars,
                                name=pp(prefix, '_layers'),
                                n_steps=nsteps,
                                truncate_gradient=truncate_gradient,
                                profile=profile,
                                strict=False)
    rval = [rval]
    return rval

# Attention

# Tanh attention
def param_init_tanh_attention(options, params, prefix='tanh_attention',
                              dim=None, dimctx=None, attention_hidden_dim=None):
    # attention: combined -> hidden
    W_comb_att = norm_weight(dim, attention_hidden_dim)
    params[pp(prefix, 'W_comb_att')] = W_comb_att

    # attention: context -> hidden
    Wc_att = norm_weight(dimctx, attention_hidden_dim)
    params[pp(prefix, 'Wc_att')] = Wc_att

    # attention: hidden bias
    b_att = numpy.zeros((attention_hidden_dim,)).astype(floatX)
    params[pp(prefix, 'b_att')] = b_att

    # attention:
    U_att = norm_weight(attention_hidden_dim, 1)
    params[pp(prefix, 'U_att')] = U_att
    c_att = numpy.zeros((1,)).astype(floatX)
    params[pp(prefix, 'c_tt')] = c_att

    scale_add = 0.0
    scale_mul = 1.0    
    if options['layer_normalisation']:
        params[pp(prefix,'W_comb_att_lnb')] = scale_add * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'W_comb_att_lns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'Wc_att_lnb')] = scale_add * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'Wc_att_lns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
    if options['weight_normalisation']:
        params[pp(prefix,'W_comb_att_wns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'Wc_att_wns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'U_att_wns')] = scale_mul * numpy.ones((1*1)).astype(floatX)
    return params

def tanh_attention(tparams, options, dropout, prefix='',
                   n_samples=None,
                   dropout_probability_ctx=0,
                   dropout_probability_rec=0):

    def wn(param_name):
        param = tparams[param_name]
        if options['weight_normalisation']:
            return weight_norm(param, tparams[param_name+'_wns'])
        else:
            return param
    
    dim, attention_hidden_dim = tparams[pp(prefix, 'W_comb_att')].shape
    dimctx = tparams[pp(prefix, 'Wc_att')].shape[0]
    
    rec_dropout = dropout((n_samples, dim), dropout_probability_rec, num=1)
    ctx_dropout = dropout((n_samples, dimctx), dropout_probability_ctx, num=1)
    attention_hidden_dropout = dropout((n_samples, attention_hidden_dim), dropout_probability_ctx, num=1)
    
    def project_context(context, pctx_):
        assert context.ndim == 3, 'Context must be 3-d: #annotation x #sample x dim'
        if pctx_ is None:
            pctx_ = tensor.dot(context*ctx_dropout, wn(pp(prefix, 'Wc_att'))) +\
                tparams[pp(prefix, 'b_att')]

        if options['layer_normalisation']:
            pctx_ = layer_norm(pctx_, tparams[pp(prefix,'Wc_att_lnb')], tparams[pp(prefix,'Wc_att_lns')])
        
        return pctx_
        
    def compute_attention(h1, pctx_, context_mask):
        pstate_ = tensor.dot(h1*rec_dropout, wn(pp(prefix, 'W_comb_att')))
        if options['layer_normalisation']:
            pstate_ = layer_norm(pstate_, tparams[pp(prefix, 'W_comb_att_lnb')], tparams[pp(prefix, 'W_comb_att_lns')])
        pctx__ = pctx_ + pstate_[None, :, :]
        pctx__ = tensor.tanh(pctx__)
        alpha = tensor.dot(pctx__*attention_hidden_dropout, wn(pp(prefix, 'U_att')))+tparams[pp(prefix, 'c_tt')]
        alpha = alpha.reshape([alpha.shape[0], alpha.shape[1]])
        alpha = tensor.exp(alpha - alpha.max(0, keepdims=True))
        if context_mask:
            alpha = alpha * context_mask
        alpha = alpha / alpha.sum(0, keepdims=True)
        
        return alpha
        
    # DIY object-oriented programming (ToDo: refactor everything to use classes)
    return (project_context, compute_attention)

# CReLU attention
def param_init_crelu_attention(options, params, prefix='crelu_attention',
                              dim=None, dimctx=None, attention_hidden_dim=None):
    # attention: combined -> hidden
    W_comb_att = norm_weight(dim, attention_hidden_dim, scale='glorot_inout')
    params[pp(prefix, 'W_comb_att')] = W_comb_att

    # attention: context -> hidden
    Wc_att = norm_weight(dimctx, attention_hidden_dim, scale='glorot_inout')
    params[pp(prefix, 'Wc_att')] = Wc_att

    # attention: hidden bias
    b_att = numpy.zeros((attention_hidden_dim,)).astype(floatX)
    params[pp(prefix, 'b_att')] = b_att

    # attention:
    U_att_pos = norm_weight(attention_hidden_dim, 1, scale='glorot_in')
    params[pp(prefix, 'U_att_pos')] = U_att_pos
    U_att_neg = -U_att_pos     # Looks linear initialization (Balduzzi et al. 2017)
    params[pp(prefix, 'U_att_neg')] = U_att_neg
#    c_att = numpy.zeros((1,)).astype(floatX)
#    params[pp(prefix, 'c_tt')] = c_att

    scale_add = 0.0
    scale_mul = 1.0    
    if options['layer_normalisation']:
#        params[pp(prefix,'W_comb_att_lnb')] = scale_add * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'W_comb_att_lns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'Wc_att_lnb')] = scale_add * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'Wc_att_lns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
    if options['weight_normalisation']:
        params[pp(prefix,'W_comb_att_wns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'Wc_att_wns')] = scale_mul * numpy.ones((1*dimctx)).astype(floatX)
        params[pp(prefix,'U_att_pos_wns')] = scale_mul * numpy.ones((1*1)).astype(floatX)
        params[pp(prefix,'U_att_neg_wns')] = scale_mul * numpy.ones((1*1)).astype(floatX)
    return params

def crelu_attention(tparams, options, dropout, prefix='',
                   n_samples=None,
                   dropout_probability_ctx=0,
                   dropout_probability_rec=0):

    def wn(param_name):
        param = tparams[param_name]
        if options['weight_normalisation']:
            return weight_norm(param, tparams[param_name+'_wns'])
        else:
            return param
    
    dim, attention_hidden_dim = tparams[pp(prefix, 'W_comb_att')].shape
    dimctx = tparams[pp(prefix, 'Wc_att')].shape[0]
    
    rec_dropout = dropout((n_samples, dim), dropout_probability_rec, num=1)
    ctx_dropout = dropout((n_samples, dimctx), dropout_probability_ctx, num=1)
    attention_hidden_dropout = dropout((n_samples, attention_hidden_dim), dropout_probability_ctx, num=1)
    
    def project_context(context, pctx_):
        assert context.ndim == 3, 'Context must be 3-d: #annotation x #sample x dim'
        if pctx_ is None:
            pctx_ = tensor.dot(context*ctx_dropout, wn(pp(prefix, 'Wc_att'))) +\
                tparams[pp(prefix, 'b_att')]

        if options['layer_normalisation']:
            pctx_ = layer_norm(pctx_, tparams[pp(prefix,'Wc_att_lnb')], tparams[pp(prefix,'Wc_att_lns')])
        
        return pctx_
        
    def compute_attention(h1, pctx_, context_mask):
        pstate_ = tensor.dot(h1*rec_dropout, wn(pp(prefix, 'W_comb_att')))
        if options['layer_normalisation']:
            #pstate_ = layer_norm(pstate_, tparams[pp(prefix, 'W_comb_att_lnb')], tparams[pp(prefix, 'W_comb_att_lns')])
            pstate_ = layer_norm(pstate_, None, tparams[pp(prefix, 'W_comb_att_lns')])
        pctx__ = pctx_ + pstate_[None, :, :]
        pctx__dropped = pctx__*attention_hidden_dropout     # Note: ReLUs are transparent to dropout
        pctx__pos = tensor.nnet.relu(pctx__dropped)
        pctx__neg = tensor.nnet.relu(-pctx__dropped)
        
        alpha = tensor.dot(pctx__pos, wn(pp(prefix, 'U_att_pos'))) + \
                tensor.dot(pctx__neg, wn(pp(prefix, 'U_att_neg'))) \
                # + tparams[pp(prefix, 'c_tt')]		# Do we need this? Probably not
                                 
        alpha = alpha.reshape([alpha.shape[0], alpha.shape[1]])
        alpha = tensor.exp(alpha - alpha.max(0, keepdims=True))
        if context_mask:
            alpha = alpha * context_mask
        alpha = alpha / alpha.sum(0, keepdims=True)
        
        return alpha
        
    # DIY object-oriented programming (ToDo: refactor everything to use classes)
    return (project_context, compute_attention)


    
# Conditional GRU layer with Attention
def param_init_gru_cond(options, params, prefix='gru_cond',
                        nin=None, dim=None, dimctx=None,
                        nin_nonlin=None, dim_nonlin=None,
                        recurrence_transition_depth=2,
                        attention_hidden_activation='tanh',
                        attention_hidden_dim=None,
                        reset_gate=True,
                        main_activation='tanh',
                        main_recurrent_identity_init=False,
                        post_activation_input=False,
                        zero_init_main_input=False):
    if nin is None:
        nin = options['dim']
    if dim is None:
        dim = options['dim']
    if dimctx is None:
        dimctx = options['dim']
    if nin_nonlin is None:
        nin_nonlin = nin
    if dim_nonlin is None:
        dim_nonlin = dim
    if attention_hidden_dim is None:
        attention_hidden_dim = 2 * dim

    scale_add = 0.0
    scale_mul = 1.0
    scale_mul_main = numpy.sqrt(0.5) if (main_activation == 'prelu') and (not post_activation_input) else 1.0
    scale_mul_main_in = 0.0 if zero_init_main_input else scale_mul_main

    n_aux_gates = 2 if reset_gate else 1
    W = numpy.concatenate([norm_weight(nin, dim), norm_weight(nin, dim)], axis=1) if reset_gate else norm_weight(nin, dim)

    params[pp(prefix, 'W')] = W
    params[pp(prefix, 'b')] = numpy.zeros((n_aux_gates * dim,)).astype(floatX)
    U = numpy.concatenate([ortho_weight(dim_nonlin), ortho_weight(dim_nonlin)], axis=1) if reset_gate else ortho_weight(dim_nonlin)
    params[pp(prefix, 'U')] = U

    if zero_init_main_input and not options['layer_normalisation']:
        Wx = umpy.zeros((nin_nonlin, dim_nonlin)).astype(floatX)
    else:
        Wx = norm_weight(nin_nonlin, dim_nonlin)
    params[pp(prefix, 'Wx')] = Wx
    Ux = ortho_weight(dim_nonlin) if not main_recurrent_identity_init else numpy.eye(dim_nonlin).astype(floatX)
    params[pp(prefix, 'Ux')] = Ux
    params[pp(prefix, 'bx')] = numpy.zeros((dim_nonlin,)).astype(floatX)
    if post_activation_input:
        Wx_post = -Wx # Looks-linear initialization
        params[pp(prefix, 'Wx_post')] = Wx_post
        params[pp(prefix, 'bx_post')] = numpy.zeros((dim_nonlin,)).astype(floatX)

    if main_activation == 'tanh':
        pass
    elif main_activation == 'prelu':
        params[pp(prefix, 'prelupx')] = -numpy.ones((dim_nonlin,)).astype(floatX)
    elif main_activation == 'crelu':
        params[pp(prefix, 'Ux_post_pos')] = Ux.T
        params[pp(prefix, 'Ux_post_neg')] = -Ux.T
    else:
        assert False, 'Unknown main activation %s' % main_activation

    for i in xrange(recurrence_transition_depth - 1):
        suffix = '' if i == 0 else ('_drt_%s' % i)
        U_nl = numpy.concatenate([ortho_weight(dim_nonlin), ortho_weight(dim_nonlin)], axis=1) if reset_gate else ortho_weight(dim_nonlin)
        params[pp(prefix, 'U_nl'+suffix)] = U_nl
        params[pp(prefix, 'b_nl'+suffix)] = numpy.zeros((n_aux_gates * dim_nonlin,)).astype(floatX)
        Ux_nl = ortho_weight(dim_nonlin) if not main_recurrent_identity_init else numpy.eye(dim_nonlin).astype(floatX)
        params[pp(prefix, 'Ux_nl'+suffix)] = Ux_nl
        params[pp(prefix, 'bx_nl'+suffix)] = numpy.zeros((dim_nonlin,)).astype(floatX)

        if main_activation == 'prelu':
            params[pp(prefix, 'prelupx_nl'+suffix)] = -numpy.ones((dim_nonlin,)).astype(floatX)
        elif main_activation == 'crelu':
            params[pp(prefix, 'Ux_nl_post_pos'+suffix)] = Ux_nl.T
            params[pp(prefix, 'Ux_nl_post_neg'+suffix)] = -Ux_nl.T
        
        if options['layer_normalisation']:
            params[pp(prefix,'U_nl%s_lnb' % suffix)] = scale_add * numpy.ones((n_aux_gates*dim)).astype(floatX)
            params[pp(prefix,'U_nl%s_lns' % suffix)] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
            params[pp(prefix,'Ux_nl%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Ux_nl%s_lns' % suffix)] = scale_mul_main * numpy.ones((1*dim)).astype(floatX)
            if main_activation == 'crelu':
                 params[pp(prefix,'Ux_nl_post%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
                 params[pp(prefix,'Ux_nl_post%s_lns' % suffix)] = scale_mul_main * numpy.ones((1*dim)).astype(floatX)
        if options['weight_normalisation']:
            params[pp(prefix,'U_nl%s_wns') % suffix] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
            params[pp(prefix,'Ux_nl%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            if main_activation == 'crelu':
                params[pp(prefix,'Ux_nl_post_pos%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Ux_nl_post_neg%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)

        # context to LSTM
        if i == 0:
            Wc = norm_weight(dimctx, dim*n_aux_gates)
            params[pp(prefix, 'Wc'+suffix)] = Wc
            if zero_init_main_input and not options['layer_normalisation']:
                Wcx = numpy.zeros((dimctx, dim)).astype(floatX)
            else:
                Wcx = norm_weight(dimctx, dim)
            params[pp(prefix, 'Wcx'+suffix)] = Wcx
            if post_activation_input:
                Wcx_post = -Wcx # Looks-linear initialization
                params[pp(prefix, 'Wcx_post'+suffix)] = Wcx_post
                
            if options['layer_normalisation']:
                params[pp(prefix,'Wc%s_lnb') % suffix] = scale_add * numpy.ones((n_aux_gates*dim)).astype(floatX)
                params[pp(prefix,'Wc%s_lns') % suffix] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
                params[pp(prefix,'Wcx%s_lnb') % suffix] = scale_add * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wcx%s_lns') % suffix] = scale_mul_main_in * numpy.ones((1*dim)).astype(floatX)
                if post_activation_input:
                    params[pp(prefix,'Wcx_post%s_lnb') % suffix] = scale_add * numpy.ones((1*dim)).astype(floatX)
                    params[pp(prefix,'Wcx_post%s_lns') % suffix] = scale_mul_main * numpy.ones((1*dim)).astype(floatX)
            if options['weight_normalisation']:
                params[pp(prefix,'Wc%s_wns') % suffix] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
                params[pp(prefix,'Wcx%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                if post_activation_input:
                    params[pp(prefix,'Wcx_post%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                

    # attention
    params = get_layer_param(attention_hidden_activation+'_attention')(options, params, prefix=prefix, dim=dim, dimctx=dimctx, attention_hidden_dim=attention_hidden_dim)

    if options['layer_normalisation']:
        # layer-normalization parameters
        params[pp(prefix,'W_lnb')] = scale_add * numpy.ones((n_aux_gates*dim)).astype(floatX)
        params[pp(prefix,'W_lns')] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
        params[pp(prefix,'U_lnb')] = scale_add * numpy.ones((n_aux_gates*dim)).astype(floatX)
        params[pp(prefix,'U_lns')] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
        params[pp(prefix,'Wx_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Wx_lns')] = scale_mul_main_in * numpy.ones((1*dim)).astype(floatX)
        if post_activation_input:
            params[pp(prefix,'Wx_post_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Wx_post_lns')] = scale_mul_main * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Ux_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Ux_lns')] = scale_mul_main * numpy.ones((1*dim)).astype(floatX)
        if main_activation == 'crelu':
            params[pp(prefix,'Ux_post_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Ux_post_lns')] = scale_mul_main * numpy.ones((1*dim)).astype(floatX)
    if options['weight_normalisation']:
        params[pp(prefix,'W_wns')] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
        params[pp(prefix,'U_wns')] = scale_mul * numpy.ones((n_aux_gates*dim)).astype(floatX)
        params[pp(prefix,'Wx_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        if post_activation_input:
            params[pp(prefix,'Wx_post_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Ux_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        if main_activation == 'crelu':
            params[pp(prefix,'Ux_post_pos_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Ux_post_neg_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)

    return params    

def gru_cond_layer(tparams, state_below, options, dropout, prefix='gru',
                   mask=None, context=None, one_step=False,
                   init_memory=None, init_state=None,
                   context_mask=None,
                   dropout_probability_below=0,
                   dropout_probability_ctx=0,
                   dropout_probability_rec=0,
                   pctx_=None,
                   recurrence_transition_depth=2,
                   truncate_gradient=-1,
                   attention_hidden_activation='tanh',
                   reset_gate=True,
                   main_activation='tanh',
                   profile=False,
                   **kwargs):

    assert context, 'Context must be provided'

    if one_step:
        assert init_state, 'previous state must be provided'

    nsteps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
        dim_below = state_below.shape[2]
    else:
        n_samples = 1
        dim_below = state_below.shape[1]

    # mask
    if mask is None:
        mask = tensor.ones((state_below.shape[0], 1))

    dim = tparams[pp(prefix, 'Wcx')].shape[1]
    
    post_activation_input = pp(prefix, 'Wcx_post') in tparams

    rec_dropout = dropout((n_samples, dim), dropout_probability_rec, num= 2 * recurrence_transition_depth)
    
    # utility function to look up parameters and apply weight normalization if enabled
    def wn(param_name):
        param = tparams[param_name]
        if options['weight_normalisation']:
            return weight_norm(param, tparams[param_name+'_wns'])
        else:
            return param

    below_dropout = dropout((n_samples, dim_below),  dropout_probability_below, num=2)
    ctx_dropout = dropout((n_samples, 2*options['dim']), dropout_probability_ctx, num=2)

    # initial/previous state
    if init_state is None:
        init_state = tensor.zeros((n_samples, dim))

    project_context, compute_attention = get_layer_constr(attention_hidden_activation+'_attention')(tparams, options, dropout, prefix,
                                                                                                    n_samples,
                                                                                                    dropout_probability_ctx,
                                                                                                    dropout_probability_rec)
    
    # projected context
    pctx_ = project_context(context, pctx_)
    
    def _slice(_x, n, dim):
        if _x.ndim == 3:
            return _x[:, :, n*dim:(n+1)*dim]
        return _x[:, n*dim:(n+1)*dim]

    # state_below is the previous output word embedding
    state_belowx = tensor.dot(state_below*below_dropout[0], wn(pp(prefix, 'Wx'))) + tparams[pp(prefix, 'bx')]
    state_below_ = tensor.dot(state_below*below_dropout[1], wn(pp(prefix, 'W'))) + tparams[pp(prefix, 'b')]
    if post_activation_input:
        state_belowx_post = tensor.dot(state_below*below_dropout[0], wn(pp(prefix, 'Wx_post'))) + tparams[pp(prefix, 'bx_post')]
        state_belowx = tensor.concatenate([state_belowx, state_belowx_post], axis=-1) # ToDo: make more efficient

    def _step_slice(m_, x_, xx_, h_, ctx_, alpha_, pctx_, cc_, rec_dropout, ctx_dropout):
        if post_activation_input:
            xx_tmp  = _slice(xx_, 0, dim)
            xx_post = _slice(xx_, 1, dim)
            xx_ = xx_tmp
        
        if options['layer_normalisation']:
            x_ = layer_norm(x_, tparams[pp(prefix, 'W_lnb')], tparams[pp(prefix, 'W_lns')])
            xx_ = layer_norm(xx_, tparams[pp(prefix, 'Wx_lnb')], tparams[pp(prefix, 'Wx_lns')])
            if post_activation_input:
               xx_post = layer_norm(xx_post, tparams[pp(prefix, 'Wx_post_lnb')], tparams[pp(prefix, 'Wx_post_lns')])

        preact1 = tensor.dot(h_*rec_dropout[0], wn(pp(prefix, 'U')))
        if options['layer_normalisation']:
            preact1 = layer_norm(preact1, tparams[pp(prefix, 'U_lnb')], tparams[pp(prefix, 'U_lns')])
        preact1 += x_
        preact1 = tensor.nnet.sigmoid(preact1)

        if reset_gate:
            r1 = _slice(preact1, 0, dim)
            u1 = _slice(preact1, 1, dim)
        else:
            r1 = 1
            u1 = preact1

        preactx1 = tensor.dot(h_*rec_dropout[1], wn(pp(prefix, 'Ux')))
        if options['layer_normalisation']:
            preactx1 = layer_norm(preactx1, tparams[pp(prefix, 'Ux_lnb')], tparams[pp(prefix, 'Ux_lns')])
        preactx1 *= r1
        preactx1 += xx_

        if main_activation == 'tanh':
            h1 = tensor.tanh(preactx1)
        elif main_activation == 'prelu':
            h1_pos = tensor.nnet.relu(preactx1)
            h1_neg = tensor.nnet.relu(-preactx1)
            h1 = h1_pos + tparams[pp(prefix, 'prelupx')] * h1_neg
        elif main_activation == 'crelu':
            h1_pos = tensor.nnet.relu(preactx1)
            h1_neg = tensor.nnet.relu(-preactx1)
        
        if post_activation_input:
            if main_activation == 'crelu':
                h1_pos += xx_post / 2.0
                h1_neg -= xx_post / 2.0
            else:
                h1 += xx_post
        
        if main_activation == 'crelu': # Notice that this goes after the post activation input
            h1 = tensor.dot(h1_pos, wn(pp(prefix, 'Ux_post_pos'))) + tensor.dot(h1_neg, wn(pp(prefix, 'Ux_post_neg')))
            if options['layer_normalisation']:
                h1 = layer_norm(h1, tparams[pp(prefix, 'Ux_post_lnb')], tparams[pp(prefix, 'Ux_post_lns')])

        h1 = u1 * h_ + (1. - u1) * h1
        h1 = m_[:, None] * h1 + (1. - m_)[:, None] * h_

        # attention
        alpha = compute_attention(h1, pctx_, context_mask)
        ctx_ = (cc_ * alpha[:, :, None]).sum(0)  # current context

        h2_prev = h1
        for i in xrange(recurrence_transition_depth - 1):
            suffix = '' if i == 0 else ('_drt_%s' % i)

            preact2 = tensor.dot(h2_prev*rec_dropout[2+2*i], wn(pp(prefix, 'U_nl'+suffix)))+tparams[pp(prefix, 'b_nl'+suffix)]
            if options['layer_normalisation']:
                preact2 = layer_norm(preact2, tparams[pp(prefix, 'U_nl%s_lnb' % suffix)], tparams[pp(prefix, 'U_nl%s_lns' % suffix)])
            if i == 0:
                ctx1_ = tensor.dot(ctx_*ctx_dropout[0], wn(pp(prefix, 'Wc'+suffix))) # dropout mask is shared over mini-steps
                if options['layer_normalisation']:
                    ctx1_ = layer_norm(ctx1_, tparams[pp(prefix, 'Wc%s_lnb' % suffix)], tparams[pp(prefix, 'Wc%s_lns' % suffix)])
                preact2 += ctx1_
            preact2 = tensor.nnet.sigmoid(preact2)

            if reset_gate:
                r2 = _slice(preact2, 0, dim)
                u2 = _slice(preact2, 1, dim)
            else:
                r2 = 1
                u2 = preact2

            preactx2 = tensor.dot(h2_prev*rec_dropout[3+2*i], wn(pp(prefix, 'Ux_nl'+suffix)))+tparams[pp(prefix, 'bx_nl'+suffix)]
            if options['layer_normalisation']:
               preactx2 = layer_norm(preactx2, tparams[pp(prefix, 'Ux_nl%s_lnb' % suffix)], tparams[pp(prefix, 'Ux_nl%s_lns' % suffix)])
            preactx2 *= r2
            if i == 0:
                ctx2_ = tensor.dot(ctx_*ctx_dropout[1], wn(pp(prefix, 'Wcx'+suffix))) # dropout mask is shared over mini-steps
                if options['layer_normalisation']:
                    ctx2_ = layer_norm(ctx2_, tparams[pp(prefix, 'Wcx%s_lnb' % suffix)], tparams[pp(prefix, 'Wcx%s_lns' % suffix)])
                preactx2 += ctx2_
            if main_activation == 'tanh':
                h2 = tensor.tanh(preactx2)
            elif main_activation == 'prelu':
                h2_pos = tensor.nnet.relu(preactx2)
                h2_neg = tensor.nnet.relu(-preactx2)
                h2 = h2_pos + tparams[pp(prefix, 'prelupx_nl')] * h2_neg
            elif main_activation == 'crelu':
                h2_pos = tensor.nnet.relu(preactx2)
                h2_neg = tensor.nnet.relu(-preactx2)
                
            if post_activation_input:
                ctx2_post = tensor.dot(ctx_*ctx_dropout[1], wn(pp(prefix, 'Wcx_post'+suffix))) # dropout mask is shared over mini-steps
                if options['layer_normalisation']:
                    ctx2_post = layer_norm(ctx2_post, tparams[pp(prefix, 'Wcx_post%s_lnb' % suffix)], tparams[pp(prefix, 'Wcx_post%s_lns' % suffix)])
                if main_activation == 'crelu':
                    h2_pos += ctx2_post
                    h2_neg += ctx2_post
                else:
                    h2 += ctx2_post
            
            if main_activation == 'crelu': # Notice that this goes after the post activation input
                h2 = tensor.dot(h2_pos, wn(pp(prefix, 'Ux_nl_post_pos'+suffix))) + tensor.dot(h2_neg, wn(pp(prefix, 'Ux_nl_post_neg'+suffix)))
                if options['layer_normalisation']:
                    h2 = layer_norm(h2, tparams[pp(prefix, 'Ux_nl_post_lnb'+suffix)], tparams[pp(prefix, 'Ux_nl_post_lns'+suffix)])

            h2 = u2 * h2_prev + (1. - u2) * h2
            h2 = m_[:, None] * h2 + (1. - m_)[:, None] * h2_prev
            h2_prev = h2

        return h2, ctx_, alpha.T  # pstate_, preact, preactx, r, u

    seqs = [mask, state_below_, state_belowx]
    #seqs = [mask, state_below_, state_belowx, state_belowc]
    _step = _step_slice

    shared_vars = []

    if one_step:
        rval = _step(*(seqs + [init_state, None, None, pctx_, context, rec_dropout, ctx_dropout] +
                       shared_vars))
    else:
        rval, updates = theano.scan(_step,
                                    sequences=seqs,
                                    outputs_info=[init_state,
                                                  tensor.zeros((n_samples,
                                                               context.shape[2])),
                                                  tensor.zeros((n_samples,
                                                               context.shape[0]))],
                                    non_sequences=[pctx_, context, rec_dropout, ctx_dropout]+shared_vars,
                                    name=pp(prefix, '_layers'),
                                    n_steps=nsteps,
                                    truncate_gradient=truncate_gradient,
                                    profile=profile,
                                    strict=False)
    return rval

# Conditional GRU layer with Attention
def param_init_crelurhn_cond(options, params, prefix='crelurhn_cond',
                        nin=None, dim=None, dimctx=None,
                        nin_nonlin=None, dim_nonlin=None,
                        recurrence_transition_depth=2,
                        attention_hidden_activation='tanh',
                        attention_hidden_dim=None,
                        **kwargs):
    if nin is None:
        nin = options['dim']
    if dim is None:
        dim = options['dim']
    if dimctx is None:
        dimctx = options['dim']
    if nin_nonlin is None:
        nin_nonlin = nin
    if dim_nonlin is None:
        dim_nonlin = dim
    if attention_hidden_dim is None:
        attention_hidden_dim = 2 * dim

    scale_add = 0.0
    scale_mul = 1.0

    W = norm_weight(nin, dim)
    params[pp(prefix, 'W')] = W
    params[pp(prefix, 'b')] = numpy.zeros((1 * dim,)).astype(floatX)
    U = ortho_weight(dim_nonlin)
    params[pp(prefix, 'U')] = U

    Wx_pos = norm_weight(nin_nonlin, dim_nonlin)
    Wx_neg = -Wx_pos
    params[pp(prefix, 'Wx_pos')] = Wx_pos
    params[pp(prefix, 'Wx_neg')] = Wx_neg
    Ux = ortho_weight(dim_nonlin)
    Ux_post_pos = Ux.T
    Ux_post_neg = -Ux_post_pos
    params[pp(prefix, 'Ux')] = Ux
    params[pp(prefix, 'Ux_post_pos')] = Ux_post_pos
    params[pp(prefix, 'Ux_post_neg')] = Ux_post_neg
    params[pp(prefix, 'bx_pos')] = numpy.zeros((dim_nonlin,)).astype(floatX)
    params[pp(prefix, 'bx_neg')] = numpy.zeros((dim_nonlin,)).astype(floatX)

    for i in xrange(recurrence_transition_depth - 1):
        suffix = '' if i == 0 else ('_drt_%s' % i)
        U_nl = ortho_weight(dim_nonlin)
        params[pp(prefix, 'U_nl'+suffix)] = U_nl
        params[pp(prefix, 'b_nl'+suffix)] = numpy.zeros((1 * dim_nonlin,)).astype(floatX)
        Ux_nl = ortho_weight(dim_nonlin)
        Ux_nl_post_pos = Ux_nl.T
        Ux_nl_post_neg = -Ux_nl_post_pos
        params[pp(prefix, 'Ux_nl'+suffix)] = Ux_nl
        params[pp(prefix, 'Ux_nl_post_pos'+suffix)] = Ux_nl_post_pos
        params[pp(prefix, 'Ux_nl_post_neg'+suffix)] = Ux_nl_post_neg
        params[pp(prefix, 'bx_nl_pos'+suffix)] = numpy.zeros((dim_nonlin,)).astype(floatX)
        params[pp(prefix, 'bx_nl_neg'+suffix)] = numpy.zeros((dim_nonlin,)).astype(floatX)
        
        if options['layer_normalisation']:
            params[pp(prefix,'U_nl%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'U_nl%s_lns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            #params[pp(prefix,'Ux_nl%s_lnb' % suffix)] = scale_add * numpy.ones((1*dim)).astype(floatX)
            #params[pp(prefix,'Ux_nl%s_lns' % suffix)] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        if options['weight_normalisation']:
            params[pp(prefix,'U_nl%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Ux_nl%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Ux_nl_post_pos%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            params[pp(prefix,'Ux_nl_post_neg%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)

        # context to LSTM
        if i == 0:
            Wc = norm_weight(dimctx, dim*1)
            params[pp(prefix, 'Wc'+suffix)] = Wc
            Wcx_pos = norm_weight(dimctx, dim)
            Wcx_neg = -Wcx_pos
            params[pp(prefix, 'Wcx_pos'+suffix)] = Wcx_pos
            params[pp(prefix, 'Wcx_neg'+suffix)] = Wcx_neg
            if options['layer_normalisation']:
                params[pp(prefix,'Wc%s_lnb') % suffix] = scale_add * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wc%s_lns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wcx_pos%s_lnb') % suffix] = scale_add * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wcx_pos%s_lns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wcx_neg%s_lnb') % suffix] = scale_add * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wcx_neg%s_lns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
            if options['weight_normalisation']:
                params[pp(prefix,'Wc%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wcx_pos%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)
                params[pp(prefix,'Wcx_neg%s_wns') % suffix] = scale_mul * numpy.ones((1*dim)).astype(floatX)          

    # attention
    params = get_layer_param(attention_hidden_activation+'_attention')(options, params, prefix=prefix, dim=dim, dimctx=dimctx, attention_hidden_dim=attention_hidden_dim)

    if options['layer_normalisation']:
        # layer-normalization parameters
        params[pp(prefix,'W_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'W_lns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'U_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'U_lns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Wx_pos_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Wx_pos_lns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Wx_neg_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Wx_neg_lns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        #params[pp(prefix,'Ux_lnb')] = scale_add * numpy.ones((1*dim)).astype(floatX)
        #params[pp(prefix,'Ux_lns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
    if options['weight_normalisation']:
        params[pp(prefix,'W_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'U_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Wx_pos_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Wx_neg_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Ux_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Ux_post_pos_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)
        params[pp(prefix,'Ux_post_neg_wns')] = scale_mul * numpy.ones((1*dim)).astype(floatX)

    return params    

def crelurhn_cond_layer(tparams, state_below, options, dropout, prefix='crelurhn_cond',
                   mask=None, context=None, one_step=False,
                   init_memory=None, init_state=None,
                   context_mask=None,
                   dropout_probability_below=0,
                   dropout_probability_ctx=0,
                   dropout_probability_rec=0,
                   pctx_=None,
                   recurrence_transition_depth=2,
                   truncate_gradient=-1,
                   attention_hidden_activation='tanh',
                   crelurhn_layer_norm_on_state=True,
                   profile=False,
                   **kwargs):

    assert context, 'Context must be provided'

    if one_step:
        assert init_state, 'previous state must be provided'

    nsteps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
        dim_below = state_below.shape[2]
    else:
        n_samples = 1
        dim_below = state_below.shape[1]

    # mask
    if mask is None:
        mask = tensor.ones((state_below.shape[0], 1))

    dim = tparams[pp(prefix, 'Wcx_pos')].shape[1]

    rec_dropout = dropout((n_samples, dim), dropout_probability_rec, num= 2 * recurrence_transition_depth)
    
    # utility function to look up parameters and apply weight normalization if enabled
    def wn(param_name):
        param = tparams[param_name]
        if options['weight_normalisation']:
            return weight_norm(param, tparams[param_name+'_wns'])
        else:
            return param

    below_dropout = dropout((n_samples, dim_below),  dropout_probability_below, num=2)
    ctx_dropout = dropout((n_samples, 2*options['dim']), dropout_probability_ctx, num=2)

    # initial/previous state
    #if init_state is None:
    #    init_state = tensor.zeros((n_samples, dim))

    project_context, compute_attention = get_layer_constr(attention_hidden_activation+'_attention')(tparams, options, dropout, prefix,
                                                                                                    n_samples,
                                                                                                    dropout_probability_ctx,
                                                                                                    dropout_probability_rec)
    
    # projected context
    pctx_ = project_context(context, pctx_)
    
    def _slice(_x, n, dim):
        if _x.ndim == 3:
            return _x[:, :, n*dim:(n+1)*dim]
        return _x[:, n*dim:(n+1)*dim]

    # state_below is the previous output word embedding
    state_belowx_pos = tensor.dot(state_below*below_dropout[0], wn(pp(prefix, 'Wx_pos'))) + tparams[pp(prefix, 'bx_pos')]
    state_belowx_neg = tensor.dot(state_below*below_dropout[0], wn(pp(prefix, 'Wx_neg'))) + tparams[pp(prefix, 'bx_neg')]
    state_below_ = tensor.dot(state_below*below_dropout[1], wn(pp(prefix, 'W'))) +tparams[pp(prefix, 'b')]

    def _step_slice(m_, x_, xx_pos, xx_neg, h_, ctx_, alpha_, pctx_, cc_, rec_dropout, ctx_dropout):
        if options['layer_normalisation']:
            x_ = layer_norm(x_, tparams[pp(prefix, 'W_lnb')], tparams[pp(prefix, 'W_lns')])
            xx_pos = layer_norm(xx_pos, tparams[pp(prefix, 'Wx_pos_lnb')], tparams[pp(prefix, 'Wx_pos_lns')])
            xx_neg = layer_norm(xx_neg, tparams[pp(prefix, 'Wx_neg_lnb')], tparams[pp(prefix, 'Wx_neg_lns')])

        preact1 = tensor.dot(h_*rec_dropout[0], wn(pp(prefix, 'U')))
        if options['layer_normalisation']:
            preact1 = layer_norm(preact1, tparams[pp(prefix, 'U_lnb')], tparams[pp(prefix, 'U_lns')])
        preact1 += x_
        preact1 = tensor.nnet.sigmoid(preact1)

        #r1 = _slice(preact1, 0, dim)
        #u1 = _slice(preact1, 1, dim)
        u1 = preact1

        preactx1 = tensor.dot((h_ + xx_pos)*rec_dropout[1], wn(pp(prefix, 'Ux')))
        #if options['layer_normalisation']:
        #    preactx1 = layer_norm(preactx1, tparams[pp(prefix, 'Ux_lnb')], tparams[pp(prefix, 'Ux_lns')])
        #preactx1 *= r1
        #preactx1 += xx_

        actx1_pos = tensor.nnet.relu(preactx1)
        actx1_neg = tensor.nnet.relu(-preactx1)
        actx1_post = tensor.dot(actx1_pos, wn(pp(prefix, 'Ux_post_pos'))) + tensor.dot(actx1_neg, wn(pp(prefix, 'Ux_post_neg')))
        h1 = actx1_post + xx_neg
        if crelurhn_layer_norm_on_state:
            h1 = layer_norm(h1, None, None)
        

        h1 = u1 * h_ + (1. - u1) * h1
        h1 = m_[:, None] * h1 + (1. - m_)[:, None] * h_

        # attention
        alpha = compute_attention(h1, pctx_, context_mask)
        ctx_ = (cc_ * alpha[:, :, None]).sum(0)  # current context

        h2_prev = h1
        for i in xrange(recurrence_transition_depth - 1):
            suffix = '' if i == 0 else ('_drt_%s' % i)

            preact2 = tensor.dot(h2_prev*rec_dropout[2+2*i], wn(pp(prefix, 'U_nl'+suffix)))+tparams[pp(prefix, 'b_nl'+suffix)]
            if options['layer_normalisation']:
                preact2 = layer_norm(preact2, tparams[pp(prefix, 'U_nl%s_lnb' % suffix)], tparams[pp(prefix, 'U_nl%s_lns' % suffix)])
            if i == 0:
                ctx1_ = tensor.dot(ctx_*ctx_dropout[0], wn(pp(prefix, 'Wc'+suffix))) # dropout mask is shared over mini-steps
                if options['layer_normalisation']:
                    ctx1_ = layer_norm(ctx1_, tparams[pp(prefix, 'Wc%s_lnb' % suffix)], tparams[pp(prefix, 'Wc%s_lns' % suffix)])
                preact2 += ctx1_
            preact2 = tensor.nnet.sigmoid(preact2)

            #r2 = _slice(preact2, 0, dim)
            #u2 = _slice(preact2, 1, dim)
            u2 = preact2

            h2_prev_in = h2_prev
            if i == 0:
                ctx2_pos = tensor.dot(ctx_*ctx_dropout[1], wn(pp(prefix, 'Wcx_pos'+suffix))) # dropout mask is shared over mini-steps
                ctx2_neg = tensor.dot(ctx_*ctx_dropout[1], wn(pp(prefix, 'Wcx_neg'+suffix))) # dropout mask is shared over mini-steps
                if options['layer_normalisation']:
                    ctx2_pos = layer_norm(ctx2_pos, tparams[pp(prefix, 'Wcx_pos%s_lnb' % suffix)], tparams[pp(prefix, 'Wcx_pos%s_lns' % suffix)])
                    ctx2_neg = layer_norm(ctx2_neg, tparams[pp(prefix, 'Wcx_neg%s_lnb' % suffix)], tparams[pp(prefix, 'Wcx_neg%s_lns' % suffix)])
                h2_prev_in = h2_prev + ctx2_pos
            
            preactx2 = tensor.dot(h2_prev_in*rec_dropout[3+2*i], wn(pp(prefix, 'Ux_nl'+suffix)))+tparams[pp(prefix, 'bx_nl_pos'+suffix)]
            #if options['layer_normalisation']:
            #   preactx2 = layer_norm(preactx2, tparams[pp(prefix, 'Ux_nl%s_lnb' % suffix)], tparams[pp(prefix, 'Ux_nl%s_lns' % suffix)])
            #preactx2 *= r2
            
            actx2_pos = tensor.nnet.relu(preactx2)
            actx2_neg = tensor.nnet.relu(-preactx2)
            actx2_post = tensor.dot(actx2_pos, wn(pp(prefix, 'Ux_nl_post_pos'+suffix))) + tensor.dot(actx2_neg, wn(pp(prefix, 'Ux_nl_post_neg'+suffix)))
            h2 = actx2_post + tparams[pp(prefix, 'bx_nl_neg'+suffix)]
            if i == 0:
                h2 += ctx2_neg 
            if crelurhn_layer_norm_on_state:
                h2 = layer_norm(h2, None, None)

            h2 = u2 * h2_prev + (1. - u2) * h2
            h2 = m_[:, None] * h2 + (1. - m_)[:, None] * h2_prev
            h2_prev = h2

        return h2, ctx_, alpha.T  # pstate_, preact, preactx, r, u

    seqs = [mask, state_below_, state_belowx_pos, state_belowx_neg]
    #seqs = [mask, state_below_, state_belowx, state_belowc]
    _step = _step_slice

    shared_vars = []

    if one_step:
        rval = _step(*(seqs + [init_state, None, None, pctx_, context, rec_dropout, ctx_dropout] +
                       shared_vars))
    else:
        rval, updates = theano.scan(_step,
                                    sequences=seqs,
                                    outputs_info=[init_state,
                                                  tensor.zeros((n_samples,
                                                               context.shape[2])),
                                                  tensor.zeros((n_samples,
                                                               context.shape[0]))],
                                    non_sequences=[pctx_, context, rec_dropout, ctx_dropout]+shared_vars,
                                    name=pp(prefix, '_layers'),
                                    n_steps=nsteps,
                                    truncate_gradient=truncate_gradient,
                                    profile=profile,
                                    strict=False)
    return rval
