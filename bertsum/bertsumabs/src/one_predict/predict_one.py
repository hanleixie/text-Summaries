#!/usr/bin/env python
""" Translator Class and builder """
from __future__ import print_function
import codecs
import os
import math

import torch

from tensorboardX import SummaryWriter

from others.utils import rouge_results_to_str, test_rouge, tile
from translate.beam import GNMTGlobalScorer


def build_predictor(args, tokenizer, symbols, model, logger=None):
    scorer = GNMTGlobalScorer(args.alpha, length_penalty='wu')

    translator = Translator(args, model, tokenizer, symbols, global_scorer=scorer, logger=logger)
    return translator


class Translator(object):
    """
    Uses a model to translate a batch of sentences.


    Args:
       model (:obj:`onmt.modules.NMTModel`):
          NMT model to use for translation
       fields (dict of Fields): data fields
       beam_size (int): size of beam to use
       n_best (int): number of translations produced
       max_length (int): maximum length output to produce
       global_scores (:obj:`GlobalScorer`):
         object to rescore final translations
       copy_attn (bool): use copy attention during translation
       cuda (bool): use cuda
       beam_trace (bool): trace beam search for debugging
       logger(logging.Logger): logger.
    """

    def __init__(self,
                 args,
                 model,
                 vocab,
                 symbols,
                 global_scorer=None,
                 logger=None,
                 dump_beam=""):
        self.logger = logger
        self.cuda = args.visible_gpus != '-1'

        self.args = args
        self.model = model
        self.generator = self.model.generator
        self.vocab = vocab
        self.symbols = symbols
        self.start_token = symbols['BOS']
        self.end_token = symbols['EOS']

        self.global_scorer = global_scorer
        self.beam_size = args.beam_size
        self.min_length = args.min_length#15
        self.max_length = args.max_length#150

        self.dump_beam = dump_beam

        # for debugging
        self.beam_trace = self.dump_beam != ""
        self.beam_accum = None

        tensorboard_log_dir = args.model_path

        self.tensorboard_writer = SummaryWriter(tensorboard_log_dir, comment="Unmt")

        if self.beam_trace:
            self.beam_accum = {
                "predicted_ids": [],
                "beam_parent_ids": [],
                "scores": [],
                "log_probs": []}

    def _build_target_tokens(self, pred):
        # vocab = self.fields["tgt"].vocab
        tokens = []
        for tok in pred:
            tok = int(tok)
            tokens.append(tok)
            if tokens[-1] == self.end_token:
                tokens = tokens[:-1]
                break
        tokens = [t for t in tokens if t < len(self.vocab)]
        tokens = self.vocab.DecodeIds(tokens).split(' ')
        return tokens

    def from_batch(self, translation_batch):
        batch = translation_batch["batch"]
        assert (len(translation_batch["gold_score"]) ==
                len(translation_batch["predictions"]))
        batch_size = batch.src.size(0)

        preds, pred_score, gold_score, src = translation_batch["predictions"], translation_batch["scores"], translation_batch["gold_score"], batch.src

        translations = []
        for b in range(batch_size):
            pred_sents = self.vocab.convert_ids_to_tokens([int(n) for n in preds[b][0]])
            pred_sents = ' '.join(pred_sents).replace(' ##','')

            raw_src = [self.vocab.ids_to_tokens[int(t)] for t in src[b]][:500]
            raw_src = ' '.join(raw_src)
            translation = (pred_sents, raw_src)
            translations.append(translation)

        return translations

    def translate(self,
                  data_iter, step,
                  ):
        self.model.eval()
        can_path = self.args.result_path + '.%d.candidate' % step
        self.can_out_file = codecs.open(can_path, 'w', 'utf-8')


        raw_src_path = self.args.result_path + '.%d.raw_src' % step
        self.src_out_file = codecs.open(raw_src_path, 'w', 'utf-8')

        ct = 0
        ct_ = 0
        with torch.no_grad():
            if data_iter:
                batch = data_iter

                batch_data = self.translate_batch(batch)
                translations = self.from_batch(batch_data)

                for trans in translations:
                    # pred, gold, src = trans#一个batch一个返回
                    pred, src = trans#一个batch一个返回

                    pred_str = pred.replace('[unused0]', '').replace('[unused3]', '').replace('[PAD]', '').replace('[unused1]', '').replace(r' +', ' ').replace(' [unused2] ', '<q>').replace('[unused2]', '').replace('[SEP]', '').replace('[unused4]', '').strip()

                    print('bertsumabs_predict:', pred_str.replace(' ', ''))
                    self.can_out_file.write(pred_str + '\n')
                    self.src_out_file.write(src.strip() + '\n')
                    ct += 1
                ct_ += 1

                self.can_out_file.flush()
                self.src_out_file.flush()

        self.can_out_file.close()
        self.src_out_file.close()

        return pred_str.replace(' ', '')

    def _report_rouge(self, gold_path, can_path):
        self.logger.info("Calculating Rouge")
        results_dict = test_rouge(self.args.temp_dir, can_path, gold_path)
        return results_dict

    def translate_batch(self, batch, fast=False):
        """
        Translate a batch of sentences.

        Mostly a wrapper around :obj:`Beam`.

        Args:
           batch (:obj:`Batch`): a batch from a dataset object
           data (:obj:`Dataset`): the dataset object
           fast (bool): enables fast beam search (may not support all features)

        Todo:
           Shouldn't need the original dataset.
        """
        with torch.no_grad():
            return self._fast_translate_batch(
                batch,
                self.max_length,#150
                min_length=self.min_length)#15

    def _fast_translate_batch(self,
                              batch,
                              max_length,
                              min_length=0):
        # TODO: faster code path for beam_size == 1.

        # TODO: support these blacklisted features.
        assert not self.dump_beam

        beam_size = self.beam_size#5
        batch_size = batch.src.size(0)#nums of sents
        src = batch.src#
        segs = batch.segs
        mask_src = batch.mask_src#其中True（1）代表有用信息，FALSE（0）代表填充信息
        #当src中为零时选择用FALSE，否则true
        src_features = self.model.bert(src, segs, mask_src)#[6, 119, 768]-->[batch.size, seq.lens, word_embeddings]，整个句子的语义信息而不是cls的信息
        #输入的是句子、位置和编码BERT模型将字向量、文本向量和位置向量的加和作为模型输入，两种输出模式，本模型选用三个维度的那种
        dec_states = self.model.decoder.init_decoder_state(src, src_features, with_cache=True)
        #输入序列X经过word embedding（src_feature）和positional encoding(src)做直接加和后，作为输入
        device = src_features.device

        # Tile states and memory beam_size times.(.src[30, 119],cache=6
        dec_states.map_batch_fn(
            lambda state, dim: tile(state, beam_size, dim=dim))#beam_size=5
        src_features = tile(src_features, beam_size, dim=0)#[30, 119,768]#复制src_features五份
        batch_offset = torch.arange(
            batch_size, dtype=torch.long, device=device)
        beam_offset = torch.arange(
            0,
            batch_size * beam_size,
            step=beam_size,
            dtype=torch.long,
            device=device)
        alive_seq = torch.full(#[30,1]all_ones
            [batch_size * beam_size, 1],
            self.start_token,
            dtype=torch.long,
            device=device)

        # Give full probability to the first beam on the first step.
        topk_log_probs = (
            torch.tensor([0.0] + [float("-inf")] * (beam_size - 1),
                         device=device).repeat(batch_size))

        # Structure that holds finished hypotheses.
        hypotheses = [[] for _ in range(batch_size)]  # noqa: F812

        results = {}
        results["predictions"] = [[] for _ in range(batch_size)]  # noqa: F812
        results["scores"] = [[] for _ in range(batch_size)]  # noqa: F812
        results["gold_score"] = [0] * batch_size
        results["batch"] = batch

        for step in range(max_length):
            decoder_input = alive_seq[:, -1].view(1, -1)#[1, 30]

            # Decoder forward.
            decoder_input = decoder_input.transpose(0, 1)#经过bert模型后的向量进入decoder模型，其中encoder在bert模型中[30,1]
            #预测tgt，但为啥是30呢，batchsize是6呀，经过beam后才成的30？
            dec_out, dec_states = self.model.decoder(decoder_input, src_features, dec_states,
                                                     step=step)#dec_out[30,1,768]

            # Generator forward.
            log_probs = self.generator.forward(dec_out.transpose(0,1).squeeze(0))#[30, 768]-->[30, 21128]
            vocab_size = log_probs.size(-1)

            if step < min_length:
                log_probs[:, self.end_token] = -1e20

            # Multiply probs by the beam probability.
            log_probs += topk_log_probs.view(-1).unsqueeze(1)#[30,21128]

            alpha = self.global_scorer.alpha#0.6
            length_penalty = ((5.0 + (step + 1)) / 6.0) ** alpha#1.0

            # Flatten probs into a list of possibilities.
            curr_scores = log_probs / length_penalty

            if(self.args.block_trigram):
                cur_len = alive_seq.size(1)
                if(cur_len>3):
                    for i in range(alive_seq.size(0)):
                        fail = False
                        words = [int(w) for w in alive_seq[i]]
                        words = [self.vocab.ids_to_tokens[w] for w in words]
                        words = ' '.join(words).replace(' ##','').split()
                        if(len(words)<=3):
                            continue
                        trigrams = [(words[i-1], words[i], words[i+1]) for i in range(1, len(words)-1)]
                        trigram = tuple(trigrams[-1])
                        if trigram in trigrams[:-1]:
                            fail = True
                        if fail:
                            curr_scores[i] = -10e20

            curr_scores = curr_scores.reshape(-1, beam_size * vocab_size)#[6,105640]
            topk_scores, topk_ids = curr_scores.topk(beam_size, dim=-1)#topk_id[6, 5], [6,5]

            # Recover log probs.
            topk_log_probs = topk_scores * length_penalty#[6,5]

            # Resolve beam origin and true word ids.
            topk_beam_index = topk_ids.div(vocab_size)#[6,5]这是个除法
            topk_ids = topk_ids.fmod(vocab_size)#[6,5]求余

            # Map beam_index to batch_index in the flat representation.
            batch_index = (
                    topk_beam_index
                    + beam_offset[:topk_beam_index.size(0)].unsqueeze(1))
            select_indices = batch_index.view(-1)#[]*30

            # Append last prediction.
            alive_seq = torch.cat(
                [alive_seq.index_select(0, select_indices),
                 topk_ids.view(-1, 1)], -1)

            is_finished = topk_ids.eq(self.end_token)#[6,5]
            if step + 1 == max_length:
                is_finished.fill_(1)
            # End condition is top beam is finished.
            end_condition = is_finished[:, 0].eq(1)#[6,]
            # Save finished hypotheses.
            if is_finished.any():
                predictions = alive_seq.view(-1, beam_size, alive_seq.size(-1))
                for i in range(is_finished.size(0)):
                    b = batch_offset[i]
                    if end_condition[i]:
                        is_finished[i].fill_(1)
                    finished_hyp = is_finished[i].nonzero().view(-1)
                    # Store finished hypotheses for this batch.
                    for j in finished_hyp:
                        hypotheses[b].append((
                            topk_scores[i, j],
                            predictions[i, j, 1:]))
                    # If the batch reached the end, save the n_best hypotheses.
                    if end_condition[i]:
                        best_hyp = sorted(
                            hypotheses[b], key=lambda x: x[0], reverse=True)
                        score, pred = best_hyp[0]

                        results["scores"][b].append(score)
                        results["predictions"][b].append(pred)
                non_finished = end_condition.eq(0).nonzero().view(-1)
                # If all sentences are translated, no need to go further.
                if len(non_finished) == 0:
                    break
                # Remove finished batches for the next step.
                topk_log_probs = topk_log_probs.index_select(0, non_finished)
                batch_index = batch_index.index_select(0, non_finished)
                batch_offset = batch_offset.index_select(0, non_finished)
                alive_seq = predictions.index_select(0, non_finished) \
                    .view(-1, alive_seq.size(-1))
            # Reorder states.
            select_indices = batch_index.view(-1)
            src_features = src_features.index_select(0, select_indices)
            dec_states.map_batch_fn(
                lambda state, dim: state.index_select(dim, select_indices))

        return results


class Translation(object):
    """
    Container for a translated sentence.

    Attributes:
        src (`LongTensor`): src word ids
        src_raw ([str]): raw src words

        pred_sents ([[str]]): words from the n-best translations
        pred_scores ([[float]]): log-probs of n-best translations
        attns ([`FloatTensor`]) : attention dist for each translation
        gold_sent ([str]): words from gold translation
        gold_score ([float]): log-prob of gold translation

    """

    def __init__(self, fname, src, src_raw, pred_sents,
                 attn, pred_scores, tgt_sent, gold_score):
        self.fname = fname
        self.src = src
        self.src_raw = src_raw
        self.pred_sents = pred_sents
        self.attns = attn
        self.pred_scores = pred_scores
        self.gold_sent = tgt_sent
        self.gold_score = gold_score

    def log(self, sent_number):
        """
        Log translation.
        """

        output = '\nSENT {}: {}\n'.format(sent_number, self.src_raw)

        best_pred = self.pred_sents[0]
        best_score = self.pred_scores[0]
        pred_sent = ' '.join(best_pred)
        output += 'PRED {}: {}\n'.format(sent_number, pred_sent)
        output += "PRED SCORE: {:.4f}\n".format(best_score)

        if self.gold_sent is not None:
            tgt_sent = ' '.join(self.gold_sent)
            output += 'GOLD {}: {}\n'.format(sent_number, tgt_sent)
            output += ("GOLD SCORE: {:.4f}\n".format(self.gold_score))
        if len(self.pred_sents) > 1:
            output += '\nBEST HYP:\n'
            for score, sent in zip(self.pred_scores, self.pred_sents):
                output += "[{:.4f}] {}\n".format(score, sent)

        return output


#python train.py -task abs -mode test -bert_data_path BERT_DATA_PATH  -batch_size 128 -test_batch_size 64 -log_file ../logs/testdata_run.log  -test_from ../models/model_step_210000.pt -sep_optim true -use_interval true -visible_gpus 0 -max_pos 512 -max_length 200 -alpha 0.95 -min_length 50 -result_path ../results -report_rouge True