import ctypes as ct
from quagga.matrix import Matrix
from quagga.context import Context
from quagga.connector import Connector
from quagga.matrix import MatrixContainer


class LstmRnn(object):
    def __init__(self, W_init, R_init, x, learning=True, device_id=None):
        """
        TODO
        """
        if W_init.nrows != R_init.nrows:
            raise ValueError('W and R must have the same number of rows!')
        if R_init.nrows != R_init.ncols:
            raise ValueError('R must be a square matrix!')

        input_dim = W_init.nrows
        hidden_dim = R_init.nrows
        self.context = Context(device_id)
        self.max_input_sequence_len = len(x)

        W = [Matrix.from_npa(W_init(), device_id=device_id) for _ in xrange(4)]
        self.W = Matrix.empty(input_dim, 4 * hidden_dim, W[0].dtype, device_id)
        self.W.assign_hstack(self.context, W)
        if learning:
            self.dL_dW = Matrix.empty_like(self.W)

        R = [Matrix.from_npa(R_init(), device_id=device_id) for _ in xrange(4)]
        self.R = Matrix.empty(input_dim, 4 * hidden_dim, R[0].dtype, device_id)
        self.R.assign_vstack(self.context, R)
        if learning:
            self.dL_dR = Matrix.empty_like(self.R)

        self.h = []
        self.lstm_cells = []
        for k in xrange(self.max_input_sequence_len):
            if k == 0:
                prev_c = Matrix.empty_like(x[k], device_id)
                prev_c.fill(0.0)
                prev_h = prev_c
            else:
                prev_c = self.lstm_cells[-1].c
                prev_h = self.lstm_cells[-1].h
            cell = _LstmBlock(self.W, self.R, x[k], prev_c, prev_h, self.context, learning)
            self.lstm_cells.append(cell)
            self.h.append(cell.h)
        self.h = MatrixContainer(self.h)
        self.x = x

    def fprop(self):
        n = len(self.x)
        if n > self.max_input_sequence_len:
            raise ValueError('Sequence has length: {} that is too long. '
                             'The maximum is: {}'.
                             format(n, self.max_input_sequence_len))
        for k in xrange(n):
            self.lstm_cells[k].fprop()

    def bprop(self):
        n = len(self.x)
        for k in reversed(xrange(n)):
            if k == n-1 and k == 0:
                self.lstm_cells[k].bprop(True, True)
            elif k == n-1:
                self.lstm_cells[k].bprop(False, True)
            elif k == 0:
                self.lstm_cells[k].bprop(True, False)
            else:
                self.lstm_cells[k].bprop(False, False)
        self.dL_dW.assign_batch_add(self.context, [e.dL_dW for e in self.lstm_cells])
        self.dL_dR.assign_batch_add(self.context, [e.dL_dR for e in self.lstm_cells])

    @property
    def params(self):
        return [self.W, self.R]

    @property
    def grads(self):
        return [self.dL_dW, self.dL_dR]


class _LstmBlock(object):
    def __init__(self, W, R, x, prev_c, prev_h, context, learning=True):
        """
        TODO

        :param W: matrix that contains horizontally stacked Wz, Wi, Wf, Wo
        :param R: matrix that contains horizontally stacked Rz, Ri, Rf, Ro
        :param prev_c: previous lstm cell state
        :param prev_h: previous lstm hidden state

        TODO
        """

        device_id = context.device_id
        dim = prev_c.nrows
        self.context = context
        self.W = W
        self.R = R
        self.pre_zifo = Matrix.empty_like(prev_c, device_id)
        self.zifo = Matrix.empty_like(prev_c, device_id)
        self.z = self.zifo[:, 0*dim:1*dim]
        self.i = self.zifo[:, 1*dim:2*dim]
        self.f = self.zifo[:, 2*dim:3*dim]
        self.o = self.zifo[:, 3*dim:4*dim]
        self.c = Matrix.empty_like(prev_c, device_id)
        self.tanh_c = Matrix.empty_like(prev_c, device_id)
        self.h = Matrix.empty_like(prev_c, device_id)

        self.learning = learning
        if learning:
            try:
                self.prev_c, self.dL_dprev_c = prev_c.register_usage(self.context, self.context)
                self.prev_h, self.dL_dprev_h = prev_h.register_usage(self.context, self.context)
            except AttributeError:
                self.prev_c = prev_c
                self.prev_h = prev_h

            self._dzifo_dpre_zifo = Matrix.empty_like(self.pre_zifo, device_id)
            self._dz_dpre_z = self._dzifo_dpre_zifo[:, 0*dim:1*dim]
            self._di_dpre_i = self._dzifo_dpre_zifo[:, 1*dim:2*dim]
            self._df_dpre_f = self._dzifo_dpre_zifo[:, 2*dim:3*dim]
            self._do_dpre_o = self._dzifo_dpre_zifo[:, 3*dim:4*dim]

            self.dL_dpre_zifo = Matrix.empty_like(self.pre_zifo, device_id)
            self.dL_dpre_z = self.dL_dpre_zifo[:, 0*dim:1*dim]
            self.dL_dpre_i = self.dL_dpre_zifo[:, 1*dim:2*dim]
            self.dL_dpre_f = self.dL_dpre_zifo[:, 2*dim:3*dim]
            self.dL_dpre_o = self.dL_dpre_zifo[:, 3*dim:4*dim]

            self._dtanh_c_dc = Matrix.empty_like(self.c, device_id)
            self.c = Connector(self.c, self.context, self.context)
            self.h = Connector(self.h, self.context, self.context)
            self.dL_dW = Matrix.empty_like(W, device_id)
            self.dL_dR = Matrix.empty_like(R, device_id)
        else:
            self.prev_c = prev_c.register_usage(self.context)
            self.prev_h = prev_h.register_usage(self.context)
            self.c = Connector(self.c, self.context)
            self.h = Connector(self.h, self.context)

        if learning and x.bpropagable:
            self.x, self.dL_dx = x.register_usage(self.context, self.context)
        else:
            self.x = x.register_usage(self.context)

    @property
    def dzifo_dpre_zifo(self):
        if self.learning:
            return self._dzifo_dpre_zifo

    @property
    def dz_dpre_z(self):
        if self.learning:
            return self._dz_dpre_z

    @property
    def di_dpre_i(self):
        if self.learning:
            return self._di_dpre_i

    @property
    def df_dpre_f(self):
        if self.learning:
            return self._df_dpre_f

    @property
    def do_dpre_o(self):
        if self.learning:
            return self._do_dpre_o

    @property
    def dtanh_c_dc(self):
        if self.learning:
            return self._dtanh_c_dc

    def fprop(self):
        # zifo = tanh_sigm(x[t] * W + h[t-1] * R)
        self.pre_zifo.assign_dot(self.context, self.x, self.W)
        self.pre_zifo.add_dot(self.context, self.prev_h, self.R)
        self.pre_zifo.tanh_sigm(self.context, self.zifo, self.dzifo_dpre_zifo)

        # c[t] = i[t] .* z[t] + f[t] .* c[t-1]
        # h[t] = o[t] .* tanh(c[t])
        self.c.assign_sum_hprod(self.context, self.i, self.z, self.f, self.prev_c)
        self.c.tanh(self.context, self.tanh_c, self.dtanh_c_dc)
        self.h.assign_hprod(self.context, self.o, self.tanh_c)
        self.c.fprop()
        self.h.fprop()

    def bprop(self, is_first, is_last):
        dL_dh = self.h.backward_matrix
        dL_dc = self.c.backward_matrix

        # dL/dc[t] += dL/dh[t] .* o[t] .* dtanh(c[t])/dc[t]
        if is_last:
            dL_dc.assign_hprod(self.context, dL_dh, self.o, self.dtanh_c_dc)
        else:
            dL_dc.add_hprod(self.context, dL_dh, self.o, self.dtanh_c_dc)

        # dL/dpre_o[t] = dL/dh[t] .* tanh(c[t]) .* do[t]/dpre_o[t]
        # dL/dpre_f[t] = dL/dc[t] .* c[t-1] .* df[t]/dpre_f[t]
        # dL/dpre_i[t] = dL/dc[t] .* z[t] .* di[t]/dpre_i[t]
        # dL/dpre_z[t] = dL/dc[t] .* i[t] .* dz[t]/dpre_z[t]
        self.dL_dpre_o.assign_hprod(self.context, dL_dh, self.tanh_c, self.do_dpre_o)
        self.dL_dpre_f.assign_hprod(self.context, dL_dc, self.prev_c, self.df_dpre_f)
        self.dL_dpre_i.assign_hprod(self.context, dL_dc, self.z, self.di_dpre_i)
        self.dL_dpre_z.assign_hprod(self.context, dL_dc, self.i, self.dz_dpre_z)

        # dL_dW[t] = x[t].T * dL/dpre_zifo[t]
        # dL_dR[t] = h[t-1].T * dL/dpre_zifo[t]
        self.dL_dW.assign_dot(self.context, self.x, self.dL_dpre_zifo, 'T')
        if is_first:
            self.dL_dR.scale(self.context, ct.c_float(0.0))
        else:
            self.dL_dR.assign_dot(self.context, self.dL_dpre_zifo, self.prev_h, 'T')

        if hasattr(self, 'dL_dx'):
            # dL/dx[t] = W.T * dL/dpre_zifo[t]
            self.dL_dx.assign_dot(self.context, self.W, self.dL_dpre_zifo, 'T')

        if hasattr(self, 'dL_dprev_h'):
            # dL/dc[t-1] = f[t] .* dL/dc[t]
            self.dL_dprev_c.assign_hprod(self.context, self.f, dL_dc)
            # dL/dh[t-1] = R.T * dL/dpre_zifo[t]
            self.dL_dprev_h.assign_dot(self.context, self.R, self.dL_dpre_zifo, 'T')