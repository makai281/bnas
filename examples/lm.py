import random
import pickle

import numpy as np
import theano
from theano import tensor as T

from bnas.model import Model, Linear, LSTM, Dropout
from bnas.optimize import Nesterov, iterate_batches
from bnas.init import Gaussian, Orthogonal, Constant
from bnas.regularize import L2
from bnas.utils import expand_to_batch
from bnas.loss import batch_sequence_crossentropy
from bnas.text import encode_sequences, mask_sequences
from bnas.search import beam, greedy
from bnas.fun import function


class Gate(Model):
    def __init__(self, name, config):
        super().__init__(name)

        self.config = config

        # Define the parameters required for a recurrent transition using
        # LSTM units, taking a character embedding as input and outputting 
        # (through a fully connected tanh layer) a distribution over symbols.
        # The embeddings are shared between the input and output.
        self.param('embeddings',
                   (config['n_symbols'], config['embedding_dims']),
                   init_f=Gaussian(fan_in=config['embedding_dims']))
        self.add(LSTM('transition',
                      config['embedding_dims'], config['state_dims'],
                      layernorm=config['layernorm']))
        self.add(Linear('hidden',
                        config['state_dims'], config['embedding_dims']))
        self.add(Linear('emission',
                        config['embedding_dims'], config['n_symbols'],
                        w=self._embeddings.T))

    def __call__(self, last, h_tm1, c_tm1, last_mask=None, h_mask=None,
                 *non_sequences):
        # Construct the Theano symbol expressions for the new state and the
        # output predictions, given the embedded previous symbol and the
        # previous state.
        h_t, c_t = self.transition(
                last if last_mask is None else last * last_mask,
                h_tm1 if h_mask is None else h_tm1 * h_mask,
                c_tm1)
        return (h_t, c_t,
                T.nnet.softmax(self.emission(T.tanh(self.hidden(h_t)))))


class LanguageModel(Model):
    def __init__(self, name, config):
        super().__init__(name)

        self.config = config

        # Import the parameters of the recurrence into the main model.
        self.add(Gate('gate', config))
        # Add a learnable parameter for the initial state.
        self.param('h_0', (config['state_dims'],),
                   init_f=Gaussian(fan_in=config['state_dims']))
        self.param('c_0', (config['state_dims'],),
                   init_f=Gaussian(fan_in=config['state_dims']))

        if config['dropout']:
            self.add(Dropout('gate_dropout', config['dropout']))

        # Compile a function for a single recurrence step, this is used during
        # decoding (but not during training).
        self.step = self.gate.compile(
                T.matrix('last'), T.matrix('h_tm1'), T.matrix('c_tm1'))

    def __call__(self, outputs, outputs_mask):
        # Construct the Theano symbolic expression for the state and output
        # prediction sequences, which basically amounts to calling
        # theano.scan() using the Gate instance as inner function.
        batch_size = outputs.shape[1]
        embedded_outputs = self.gate._embeddings[outputs] \
                         * outputs_mask.dimshuffle(0,1,'x')
        h_0 = expand_to_batch(self._h_0, batch_size)
        c_0 = expand_to_batch(self._c_0, batch_size)
        (h_seq, c_seq, symbol_seq), _ = theano.scan(
                fn=self.gate,
                sequences=[{'input': embedded_outputs, 'taps': [-1]}],
                outputs_info=[h_0, c_0, None],
                non_sequences=[
                        self.gate_dropout.mask(embedded_outputs.shape[1:]),
                        self.gate_dropout.mask(h_0.shape)] + \
                    self.gate.parameters_list())
        return h_seq, symbol_seq

    def cross_entropy(self, outputs, outputs_mask):
        # Construct a Theano expression for computing the cross-entropy of an
        # example with respect to the current model predictions.
        _, symbol_seq = self(outputs, outputs_mask)
        batch_size = outputs.shape[1]
        return batch_sequence_crossentropy(
                symbol_seq, outputs[1:], outputs_mask[1:])
 
    def loss(self, outputs, outputs_mask):
        # Construct a Theano expression for computing the loss function used
        # during training. This consists of cross-entropy loss for the
        # training batch plus regularization terms.
        return super().loss() + self.cross_entropy(outputs, outputs_mask)

    def search(self, batch_size, start_symbol, stop_symbol,
               max_length, min_length):
        # Perform a beam search.

        # Get the parameter values of the embeddings and initial state.
        embeddings = self.gate._embeddings.get_value(borrow=True)
        h_0 = np.repeat(self._h_0.get_value()[None,:], batch_size, axis=0)
        c_0 = np.repeat(self._c_0.get_value()[None,:], batch_size, axis=0)

        # Define a step function, which takes a list of states and a history
        # of previous outputs, and returns the next states and output
        # predictions.
        def step(i, states, outputs, outputs_mask):
            # In this case we only condition the step on the last output,
            # and there is only one state.
            h_tm1, c_tm1 = states
            h_t, c_t, outputs = self.step(
                    embeddings[outputs[-1]], h_tm1, c_tm1)
            return [h_t, c_t], outputs

        # Call the library beam search function to do the dirty job.
        return beam(step, [h_0, c_0], batch_size, start_symbol,
                    stop_symbol, max_length, min_length=min_length)


if __name__ == '__main__':
    import sys
    import os
    from time import time

    model_filename = sys.argv[1]

    if os.path.exists(model_filename):
        with open(model_filename, 'rb') as f:
            config = pickle.load(f)
            lm = LanguageModel('lm', config)
            lm.load(f)
            symbols = config['symbols']
            index = config['index']
            print('Load model from %s' % model_filename)
    else:
        corpus_filename = sys.argv[2]
        assert os.path.exists(corpus_filename)

        with open(corpus_filename, 'r', encoding='utf-8') as f:
            sents = [line.strip() for line in f if len(line) >= 10]

        # Create a vocabulary table+index and encode the input sentences.
        symbols, index, encoded = encode_sequences(sents)

        # Model hyperparameters
        config = {
                'n_symbols': len(symbols),
                'symbols': symbols,
                'index': index,
                'embedding_dims': 64,
                'state_dims': 1024,
                'layernorm': 'c',
                'dropout': 0.2
                }

        lm = LanguageModel('lm', config)

        # Training-specific parameters
        batch_size = 128
        test_size = 128
        max_length = 128
        batch_nr = 0

        # Create the model.
        sym_outputs = T.lmatrix('outputs')
        sym_outputs_mask = T.bmatrix('outputs_mask')

        # Create an optimizer instance, manually specifying which
        # parameters to optimize, which loss function to use, which inputs
        # (none) and outputs are used for the model. We also specify the
        # gradient clipping threshold.
        optimizer = Nesterov(
                lm.parameters(),
                lm.loss(sym_outputs, sym_outputs_mask),
                [], [sym_outputs, sym_outputs_mask],
                learning_rate=0.02,
                grad_max_norm=5.0)

        # Compile a function to compute cross-entropy of a batch.
        cross_entropy = function(
                [sym_outputs, sym_outputs_mask],
                lm.cross_entropy(sym_outputs, sym_outputs_mask))

        test_set = encoded[:test_size]
        train_set = encoded[test_size:]

        # Get one batch of testing data, encoded as a masked matrix.
        test_outputs, test_outputs_mask = mask_sequences(test_set, max_length)

        for i in range(1):
            for batch in iterate_batches(train_set, batch_size, len):
                outputs, outputs_mask = mask_sequences(batch, max_length)
                if batch_nr % 10 == 0:
                    test_loss = cross_entropy(test_outputs, test_outputs_mask)
                    test_loss_bit = (
                            (test_size/test_outputs_mask[1:].sum())*
                            test_loss/(np.log(2)))
                    print('Test loss: %.3f bits/char' % test_loss_bit)
                t0 = time()
                loss = optimizer.step(outputs, outputs_mask)
                t = time() - t0

                if np.isnan(loss):
                    print('NaN at iteration %d!' % (i+1))
                    break
                print(('Batch %d:%d: train: %.3f bits/char (%.2f s)') % (
                    i+1, batch_nr+1,
                    (batch_size/outputs_mask[1:].sum())*loss/np.log(2),
                    t),
                    flush=True)

                batch_nr += 1

        with open(model_filename, 'wb') as f:
            pickle.dump(config, f)
            lm.save(f)
            print('Saved model to %s' % model_filename)

    pred, pred_mask, scores = lm.search(
            1, index['<S>'], index['</S>'], 72, 72)

    for sent, score in zip(pred, scores):
        print(score, ''.join(symbols[x] for x in sent.flatten()))

