import numpy as np
import theano
import theano.tensor as T
from utils import norm_weight, _p, ortho_weight, tanh, linear, rectifier, get_two_rngs


class Layers(object):

    def __init__(self):
        # layers: 'name': ('parameter initializer', 'feedforward')
        self.layers = {
            'ff': ('self.param_init_fflayer', 'self.fflayer'),
            'lstm': ('self.param_init_lstm', 'self.lstm_layer'),
            'lstm_cond': ('self.param_init_lstm_cond', 'self.lstm_cond_layer'),
            }
        self.rng_numpy, self.rng_theano = get_two_rngs()

    def get_layer(self, name):
        """
        Part of the reason the init is very slow is because,
        the layer's constructor is called even when it isn't needed
        """
        fns = self.layers[name]
        return eval(fns[0]), eval(fns[1])

    def dropout_layer(self, state_before, use_noise, trng):

        proj = T.switch(use_noise,
                             state_before *
                             trng.binomial(state_before.shape, p=0.5, n=1, dtype=state_before.dtype),
                             state_before * 0.5)
        return proj

    def fflayer(self, tparams, state_below, activ='lambda x: T.tanh(x)', prefix='ff', **kwargs):

        return eval(activ)(T.dot(state_below, tparams[_p(prefix,'W')])+
                           tparams[_p(prefix,'b')])

    def param_init_fflayer(self, params, nin, nout, prefix=None):
        assert prefix is not None
        params[_p(prefix, 'W')] = norm_weight(nin, nout, scale=0.01)
        params[_p(prefix, 'b')] = np.zeros((nout,)).astype('float32')
        return params


    def param_init_lstm(self, params, nin, dim, prefix='lstm'):
        assert prefix is not None
        # Stack the weight matricies for faster dot prods
        W = np.concatenate([norm_weight(nin,dim),
                            norm_weight(nin,dim),
                            norm_weight(nin,dim),
                            norm_weight(nin,dim)], axis=1)
        params[_p(prefix, 'W')] = W
        U = np.concatenate([ortho_weight(dim),
                            ortho_weight(dim),
                            ortho_weight(dim),
                            ortho_weight(dim)], axis=1)
        params[_p(prefix, 'U')] = U
        params[_p(prefix, 'b')] = np.zeros((4 * dim,)).astype('float32')

        return params

    def lstm_layer(self, tparams, state_below, mask=None, init_state=None, init_memory=None,
                   one_step=False, prefix='lstm', **kwargs):

        # state_below (t, m, dim_word), or (m, dim_word) in sampling

        if one_step:
            assert init_memory, 'previous memory must be provided'
            assert init_state, 'previous state must be provided'

        n_steps = state_below.shape[0]
        dim = tparams[_p(prefix, 'U')].shape[0]

        if state_below.ndim == 3:
            n_samples = state_below.shape[1]
            if init_state is None:
                init_state = T.alloc(0., n_samples, dim)
            if init_memory is None:
                init_memory = T.alloc(0., n_samples, dim)
        else:
            n_samples = 1
            if init_state is None:
                init_state = T.alloc(0., dim)
            if init_memory is None:
                init_memory = T.alloc(0., dim)

        if mask is None:
            mask = T.alloc(1., state_below.shape[0], 1)

        def _slice(_x, n, dim):
            if _x.ndim == 3:
                return _x[:, :, n*dim:(n+1)*dim]
            elif _x.ndim == 2:
                return _x[:, n*dim:(n+1)*dim]
            return _x[n*dim:(n+1)*dim]

        U = tparams[_p(prefix, 'U')]
        b = tparams[_p(prefix, 'b')]

        def _step(m_, x_, h_, c_):
            preact = T.dot(h_, U)
            preact += x_

            i = T.nnet.sigmoid(_slice(preact, 0, dim))
            f = T.nnet.sigmoid(_slice(preact, 1, dim))
            o = T.nnet.sigmoid(_slice(preact, 2, dim))
            c = T.tanh(_slice(preact, 3, dim))

            c = f * c_ + i * c
            h = o * T.tanh(c)
            if m_.ndim == 0:
                # when using this for minibatchsize=1
                h = m_ * h + (1. - m_) * h_
                c = m_ * c + (1. - m_) * c_
            else:
                h = m_[:, None] * h + (1. - m_)[:, None] * h_
                c = m_[:, None] * c + (1. - m_)[:, None] * c_
            return h, c

        state_below = T.dot(
            state_below, tparams[_p(prefix, 'W')]) + b

        if one_step:
            rval = _step(mask, state_below, init_state, init_memory)
        else:
            rval, updates = theano.scan(_step,
                                        sequences=[mask, state_below],
                                        outputs_info=[init_state, init_memory],
                                        name=_p(prefix,'_layers'),
                                        n_steps=n_steps,
                                        profile=False
                                        )
        return rval

    # Conditional LSTM layer with Attention
    def param_init_lstm_cond(self, options, params, nin, dim, dimctx,
                             prefix='lstm_cond'):

        # input to LSTM
        W = np.concatenate([norm_weight(nin,dim),
                               norm_weight(nin,dim),
                               norm_weight(nin,dim),
                               norm_weight(nin,dim)], axis=1)
        params[_p(prefix, 'W')] = W
        # ctx to LSTM
        V = np.concatenate([norm_weight(dimctx,dim),
                               norm_weight(dimctx,dim),
                               norm_weight(dimctx,dim),
                               norm_weight(dimctx,dim)], axis=1)
        params[_p(prefix, 'V')] = V
        # LSTM to LSTM
        U = np.concatenate([ortho_weight(dim),
                               ortho_weight(dim),
                               ortho_weight(dim),
                               ortho_weight(dim)], axis=1)
        params[_p(prefix, 'U')] = U
        params[_p(prefix, 'b')] = np.zeros((4 * dim,)).astype('float32')
        return params

    def lstm_cond_layer(self, options, tparams, state_below,
                        mask=None, context=None, one_step=False,
                        init_state=None, init_memory=None,
                        prefix='lstm', **kwargs):
        # state_below (t, m, dim_word), or (m, dim_word) in sampling
        # mask (t, m)
        # context (m, dim_ctx), or (dim_word) in sampling
        # init_memory, init_state (m, dim)
        assert context, 'Context must be provided'

        if one_step:
            assert init_memory, 'previous memory must be provided'
            assert init_state, 'previous state must be provided'

        n_steps = state_below.shape[0]
        dim = tparams[_p(prefix, 'U')].shape[0]

        if state_below.ndim == 3:
            n_samples = state_below.shape[1]
            if init_state is None:
                init_state = T.alloc(0., n_samples, dim)
            if init_memory is None:
                init_memory = T.alloc(0., n_samples, dim)
        else:
            n_samples = 1
            if init_state is None:
                init_state = T.alloc(0., dim)
            if init_memory is None:
                init_memory = T.alloc(0., dim)

        if mask is None:
            mask = T.alloc(1., state_below.shape[0], 1)

        U = tparams[_p(prefix, 'U')]
        b = tparams[_p(prefix, 'b')]

        def _slice(_x, n, dim):
            if _x.ndim == 3:
                return _x[:, :, n*dim:(n+1)*dim]
            return _x[:, n*dim:(n+1)*dim]

        def _step(m_, x_, h_, c_):
            preact = T.dot(h_, U)
            preact += x_

            i = T.nnet.sigmoid(_slice(preact, 0, dim))
            f = T.nnet.sigmoid(_slice(preact, 1, dim))
            o = T.nnet.sigmoid(_slice(preact, 2, dim))
            c = T.tanh(_slice(preact, 3, dim))

            c = f * c_ + i * c
            h = o * T.tanh(c)
            if m_.ndim == 0:
                # when using this for minibatchsize=1
                h = m_ * h + (1. - m_) * h_
                c = m_ * c + (1. - m_) * c_
            else:
                h = m_[:, None] * h + (1. - m_)[:, None] * h_
                c = m_[:, None] * c + (1. - m_)[:, None] * c_
            return h, c

        # projected inputs
        state_below = T.dot(state_below, tparams[_p(prefix, 'W')]) + \
                      T.dot(context, tparams[_p(prefix,'V')]) + b

        if one_step:
            rval = _step(mask, state_below, init_state, init_memory)
        else:
            rval, updates = theano.scan(_step,
                                        sequences=[mask, state_below],
                                        outputs_info=[init_state, init_memory],
                                        name=_p(prefix,'_layers'),
                                        n_steps=n_steps,
                                        profile=False
                                        )
        return rval