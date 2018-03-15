# encoding: utf-8
from __future__ import unicode_literals, print_function

import json
import os
import io
import itertools
import numpy as np
import random
from time import time
import torch
import pickle
from tqdm import tqdm

import evaluator
import net
import optimizer as optim
from torchtext import data
import utils
from config import get_train_args


def batch_size_func(new, count, sofar):
    return sofar + len(new[0]) + len(new[1])


def save_output(hypotheses, vocab, outf):
    # Save the Hypothesis to output file
    with io.open(outf, 'w') as fp:
        for sent in hypotheses:
            words = [vocab[y] for y in sent]
            fp.write(' '.join(words) + '\n')


def tally_parameters(model):
    n_params = sum([p.nelement() for p in model.parameters()])
    print('* number of parameters: %d' % n_params)
    enc = 0
    dec = 0
    for name, param in model.named_parameters():
        if 'encoder' in name:
            enc += param.nelement()
        elif 'decoder' or 'generator' in name:
            dec += param.nelement()
    print('encoder: ', enc)
    print('decoder: ', dec)


def report_func(epoch, batch, num_batches, start_time, report_stats, report_every, grad_norm):
    """
    This is the user-defined batch-level training progress
    report function.
    Args:
        epoch(int): current epoch count.
        batch(int): current batch count.
        num_batches(int): total number of batches.
        start_time(float): last report time.
        lr(float): current learning rate.
        report_stats(Statistics): old Statistics instance.
    Returns:
        report_stats(Statistics): updated Statistics instance.
    """
    if batch % report_every == -1 % report_every:
        report_stats.output(epoch, batch + 1, num_batches, start_time, grad_norm)
        report_stats = utils.Statistics()

    return report_stats


class CalculateBleu(object):
    def __init__(self, model, test_data, key, batch=50, max_length=50, beam_size=1):
        self.model = model
        self.test_data = test_data
        self.key = key
        self.batch = batch
        self.device = -1
        self.max_length = max_length
        self.beam_size = beam_size

    def __call__(self):
        self.model.eval()
        references = []
        hypotheses = []
        for i in tqdm(range(0, len(self.test_data), self.batch)):
            sources, targets = zip(*self.test_data[i:i + self.batch])
            references.extend(t.tolist() for t in targets)
            if self.beam_size > 1:
                ys = self.model.translate(sources, self.max_length, beam=self.beam_size)
            else:
                ys = [y.tolist() for y in self.model.translate(sources, self.max_length, beam=False)]
            hypotheses.extend(ys)
        bleu = evaluator.BLEUEvaluator().evaluate(references, hypotheses)
        print('BLEU:', bleu.score_str())
        print('')
        return bleu.bleu, hypotheses


def main():
    best_score = 0
    args = get_train_args()
    print(json.dumps(args.__dict__, indent=4))

    # Reading the int indexed text dataset
    train_data = np.load(os.path.join(args.input, args.data + ".train.npy")).tolist()
    dev_data = np.load(os.path.join(args.input, args.data + ".valid.npy")).tolist()
    test_data = np.load(os.path.join(args.input, args.data + ".test.npy")).tolist()

    # Reading the vocab file
    with open(os.path.join(args.input, args.data + '.vocab.pickle'), 'rb') as f:
        id2w = pickle.load(f)

    args.vocab_size = len(id2w)

    # Define Model
    model = net.Transformer(args.layers,
                            len(id2w),
                            args.unit,
                            multi_heads=args.multi_heads,
                            dropout=args.dropout,
                            max_length=args.max_length,
                            label_smoothing=args.label_smoothing,
                            embed_position=args.embed_position,
                            tied=args.tied,
                            pos_attention=args.pos_attention)

    tally_parameters(model)
    if args.gpu >= 0:
        model.cuda(args.gpu)
    print(model)

    optimizer = optim.TransformerAdamTrainer(model, warmup_steps=args.warmup_steps)

    src_words = len(list(itertools.chain.from_iterable(list(zip(*train_data))[0])))
    trg_words = len(list(itertools.chain.from_iterable(list(zip(*train_data))[1])))
    iter_per_epoch = (src_words + trg_words) // args.wbatchsize
    print('Approximate number of iter/epoch =', iter_per_epoch)
    time_s = time()

    total_steps = 0
    for epoch in range(args.epoch):
        random.shuffle(train_data)
        train_iter = data.iterator.pool(train_data, args.wbatchsize,
                                        key=lambda x: data.utils.interleave_keys(len(x[0]), len(x[1])),
                                        batch_size_fn=batch_size_func,
                                        random_shuffler=data.iterator.RandomShuffler())
        report_stats = utils.Statistics()
        train_stats = utils.Statistics()
        valid_stats = utils.Statistics()

        grad_norm = 0
        for num_steps, train_batch in enumerate(train_iter):
            total_steps += 1
            model.train()
            optimizer.zero_grad()

            # ---------- One iteration of the training loop ----------
            src_words = len(list(itertools.chain.from_iterable(list(zip(*train_batch))[0])))
            report_stats.n_src_words += src_words
            train_stats.n_src_words += src_words

            in_arrays = utils.seq2seq_pad_concat_convert(train_batch, -1)
            loss, stat = model(*in_arrays)
            loss.backward()

            norm = utils.grad_norm(model.parameters())
            grad_norm += norm
            optimizer.step()

            report_stats.update(stat)
            train_stats.update(stat)
            report_stats = report_func(epoch, num_steps, iter_per_epoch, time_s, report_stats,
                                       args.report_every, grad_norm / (num_steps + 1))

            if total_steps + 1 % 1000 == 0:
                if not args.no_bleu:
                    score, _ = CalculateBleu(model, dev_data, 'Dev Bleu',
                                             batch=args.batchsize // 4,
                                             beam_size=args.beam_size)()

                    if score >= best_score:
                        best_score = score
                        torch.save(model, args.model_file)

        # Check the validation accuracy of prediction after every epoch
        dev_iter = data.iterator.pool(dev_data, args.batchsize // 4,
                                      key=lambda x: data.utils.interleave_keys(len(x[0]), len(x[1])),
                                      random_shuffler=data.iterator.RandomShuffler())

        for dev_batch in dev_iter:
            model.eval()
            in_arrays = utils.seq2seq_pad_concat_convert(dev_batch, -1)
            loss_test, stat = model(*in_arrays)
            valid_stats.update(stat)

        print('Train perplexity: %g' % train_stats.ppl())
        print('Train accuracy: %g' % train_stats.accuracy())

        print('Validation perplexity: %g' % valid_stats.ppl())
        print('Validation accuracy: %g' % valid_stats.accuracy())

    # BLEU score on Dev and Test Data
    model = torch.load(args.model_file)
    print('Dev Set BLEU Score')
    _, dev_hyp = CalculateBleu(model, dev_data, 'Dev Bleu', batch=args.batchsize // 4, beam_size=args.beam_size)()
    save_output(dev_hyp, id2w, args.dev_hyp)

    print('Test Set BLEU Score')
    _, test_hyp = CalculateBleu(model, test_data, 'Test Bleu', batch=args.batchsize // 4, beam_size=args.beam_size)()
    save_output(test_hyp, id2w, args.test_hyp)


if __name__ == '__main__':
    main()
