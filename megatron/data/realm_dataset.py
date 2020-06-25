import itertools
import random

import numpy as np
from torch.utils.data import Dataset

from megatron import get_tokenizer
from megatron.data.realm_dataset_utils import build_realm_training_sample, get_block_samples_mapping, join_str_list


class REALMDataset(Dataset):
    """Dataset containing simple masked sentences for masked language modeling.

    The dataset should yield sentences just like the regular BertDataset
    However, this dataset also needs to be able to return a set of blocks
    given their start and end indices.

    Presumably

    """
    def __init__(self, name, block_dataset, title_dataset,
                 data_prefix, num_epochs, max_num_samples, masked_lm_prob,
                 max_seq_length, short_seq_prob, seed, ner_dataset=None, cased_block_dataset=None, cased_vocab=None):
        self.name = name
        self.seed = seed
        self.max_seq_length = max_seq_length
        self.masked_lm_prob = masked_lm_prob
        self.block_dataset = block_dataset
        self.title_dataset = title_dataset
        self.short_seq_prob = short_seq_prob
        self.rng = random.Random(self.seed)

        self.ner_dataset = ner_dataset
        self.cased_block_dataset = cased_block_dataset
        self.cased_tokenizer = None
        if self.cased_block_dataset is not None:
            from megatron.tokenizer.tokenizer import BertWordPieceTokenizer
            self.cased_tokenizer = BertWordPieceTokenizer(vocab_file=cased_vocab, lower_case=False)

        self.samples_mapping = get_block_samples_mapping(
            block_dataset, title_dataset, data_prefix, num_epochs,
            max_num_samples, max_seq_length, seed, name)

        self.tokenizer = get_tokenizer()
        self.vocab_id_list = list(self.tokenizer.inv_vocab.keys())
        self.vocab_id_to_token_list = self.tokenizer.inv_vocab
        self.cls_id = self.tokenizer.cls
        self.sep_id = self.tokenizer.sep
        self.mask_id = self.tokenizer.mask
        self.pad_id = self.tokenizer.pad

    def __len__(self):
        return self.samples_mapping.shape[0]

    def __getitem__(self, idx):
        start_idx, end_idx, doc_idx, block_idx = self.samples_mapping[idx]
        block = [list(self.block_dataset[i]) for i in range(start_idx, end_idx)]
        assert len(block) > 1

        block_ner_mask = None
        if self.ner_dataset is not None:
            block_ner_mask = [list(self.ner_dataset[i]) for i in range(start_idx, end_idx)]
            # print([len(list(self.ner_dataset[i])) for i in range(start_idx, end_idx)], flush=True)

        cased_tokens = None
        if self.cased_block_dataset is not None:
            cased_tokens = [list(self.cased_block_dataset[i]) for i in range(start_idx, end_idx)]

        np_rng = np.random.RandomState(seed=(self.seed + idx))

        sample = build_realm_training_sample(block,
                                             self.max_seq_length,
                                             self.vocab_id_list,
                                             self.vocab_id_to_token_list,
                                             self.cls_id,
                                             self.sep_id,
                                             self.mask_id,
                                             self.pad_id,
                                             self.masked_lm_prob,
                                             block_ner_mask,
                                             cased_tokens,
                                             self.cased_tokenizer,
                                             np_rng)
        sample.update({'query_block_indices': np.array([block_idx]).astype(np.int64)})
        return sample


class ICTDataset(Dataset):
    """Dataset containing sentences and their blocks for an inverse cloze task."""
    def __init__(self, name, block_dataset, title_dataset, data_prefix,
                 num_epochs, max_num_samples, max_seq_length,
                 query_in_block_prob, short_seq_prob, seed, use_titles=True):
        self.name = name
        self.seed = seed
        self.max_seq_length = max_seq_length
        self.query_in_block_prob = query_in_block_prob
        self.block_dataset = block_dataset
        self.title_dataset = title_dataset
        self.short_seq_prob = short_seq_prob
        self.rng = random.Random(self.seed)
        self.use_titles = use_titles

        self.samples_mapping = get_block_samples_mapping(
            block_dataset, title_dataset, data_prefix, num_epochs,
            max_num_samples, max_seq_length, seed, name)
        self.tokenizer = get_tokenizer()
        self.vocab_id_list = list(self.tokenizer.inv_vocab.keys())
        self.vocab_id_to_token_list = self.tokenizer.inv_vocab
        self.cls_id = self.tokenizer.cls
        self.sep_id = self.tokenizer.sep
        self.mask_id = self.tokenizer.mask
        self.pad_id = self.tokenizer.pad

    def __len__(self):
        return self.samples_mapping.shape[0]

    def __getitem__(self, idx):
        start_idx, end_idx, doc_idx, block_idx = self.samples_mapping[idx]
        if self.use_titles:
            title = list(self.title_dataset[int(doc_idx)])
            title_pad_offset = 3 + len(title)
        else:
            title = None
            title_pad_offset = 2
        block = [list(self.block_dataset[i]) for i in range(start_idx, end_idx)]
        assert len(block) > 1

        rand_sent_idx = self.rng.randint(0, len(block) - 1)

        # keep the query in the context 10% of the time.
        if self.rng.random() < self.query_in_block_prob:
            query = block[rand_sent_idx].copy()
        else:
            query = block.pop(rand_sent_idx)

        # still need to truncate because blocks are concluded when
        # the sentence lengths have exceeded max_seq_length.
        query = query[:self.max_seq_length - 2]
        block = list(itertools.chain(*block))[:self.max_seq_length - title_pad_offset]

        query_tokens, query_pad_mask = self.concat_and_pad_tokens(query)
        block_tokens, block_pad_mask = self.concat_and_pad_tokens(block, title)

        sample = {
            'query_tokens': np.array(query_tokens),
            'query_pad_mask': np.array(query_pad_mask),
            'block_tokens': np.array(block_tokens),
            'block_pad_mask': np.array(block_pad_mask),
            'block_data': np.array([start_idx, end_idx, doc_idx, block_idx]).astype(np.int64)
        }

        return sample

    def encode_text(self, text):
        return self.tokenizer.tokenize(text)

    def decode_tokens(self, token_ids, hardcore=False):
        tokens = self.tokenizer.tokenizer.convert_ids_to_tokens(token_ids)
        exclude_list = ['[PAD]', '[CLS]']
        if hardcore:
            extra_exclude = ['[SEP]']
            exclude_list.extend(extra_exclude)
        non_pads = [t for t in tokens if t not in exclude_list]
        joined_strs = join_str_list(non_pads)
        if hardcore:
            escape_chars = ['+', '-', '&', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*', '?', ':', '/']
            skip_me = False
            joined_strs = list(joined_strs)
            joined_strs = [s for s in joined_strs if s != '\\']
            for i, c in enumerate(joined_strs):
                if skip_me:
                    skip_me = False
                    continue
                if c in escape_chars:
                    joined_strs.insert(i, '\\')
                    skip_me = True
            joined_strs = ''.join(joined_strs)
            if len(joined_strs) < 3:
                joined_strs += 'text here'
        return joined_strs

    def get_block(self, start_idx, end_idx, doc_idx):
        """Get the IDs for an evidence block plus the title of the corresponding document"""
        block = [list(self.block_dataset[i]) for i in range(start_idx, end_idx)]
        title = list(self.title_dataset[int(doc_idx)])

        block = list(itertools.chain(*block))[:self.max_seq_length - (3 + len(title))]
        block_tokens, block_pad_mask = self.concat_and_pad_tokens(block, title)

        return (block_tokens, block_pad_mask)

    def get_null_block(self):
        block, title = [], []
        block_tokens, block_pad_mask = self.concat_and_pad_tokens(block, title)

        return (block_tokens, block_pad_mask)

    def concat_and_pad_tokens(self, tokens, title=None):
        """concat with special tokens and pad sequence to self.max_seq_length"""
        if title is None:
            tokens = [self.cls_id] + tokens + [self.sep_id]
        else:
            tokens = [self.cls_id] + title + [self.sep_id] + tokens + [self.sep_id]
        assert len(tokens) <= self.max_seq_length, len(tokens)

        num_pad = self.max_seq_length - len(tokens)
        pad_mask = [1] * len(tokens) + [0] * num_pad
        tokens += [self.pad_id] * num_pad
        return tokens, pad_mask
