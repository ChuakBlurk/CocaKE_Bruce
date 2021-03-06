import os
import json
import torch
import torch.utils.data.dataset

from typing import Optional, List

from config import args
from triplet import reverse_triplet
from triplet_mask import construct_mask, construct_self_negative_mask
from dict_hub import get_entity_dict, get_link_graph, get_tokenizer
from logger_config import logger
import random
from utils import compute_mutual_exclusiveness
from copy import copy

entity_dict = get_entity_dict()
if args.use_link_graph:
    # make the lazy data loading happen
    get_link_graph()


def _custom_tokenize(text: str,
                     text_pair: Optional[str] = None) -> dict:
    tokenizer = get_tokenizer()
    encoded_inputs = tokenizer(text=text,
                               text_pair=text_pair if text_pair else None,
                               add_special_tokens=True,
                               max_length=args.max_num_tokens,
                               return_token_type_ids=True,
                               truncation=True)
    return encoded_inputs


def _parse_entity_name(entity: str) -> str:
    if args.task.lower() == 'wn18rr':
        # family_alcidae_NN_1
        entity = ' '.join(entity.split('_')[:-2])
        return entity
    # a very small fraction of entities in wiki5m do not have name
    return entity or ''


def _concat_name_desc(entity: str, entity_desc: str) -> str:
    if entity_desc.startswith(entity):
        entity_desc = entity_desc[len(entity):].strip()
    if entity_desc:
        return '{}: {}'.format(entity, entity_desc)
    return entity


def get_neighbor_desc(head_id: str, tail_id: str = None) -> str:
    neighbor_ids = get_link_graph().get_neighbor_ids(head_id)
    # avoid label leakage during training
    if not args.is_test:
        neighbor_ids = [n_id for n_id in neighbor_ids if n_id != tail_id]
    entities = [entity_dict.get_entity_by_id(n_id).entity for n_id in neighbor_ids]
    entities = [_parse_entity_name(entity) for entity in entities]
    return ' '.join(entities)


class Example:

    def __init__(self, head_id, relation, tail_id, **kwargs):
        self.head_id = head_id
        self.tail_id = tail_id
        self.relation = relation

    @property
    def head_desc(self):
        if not self.head_id:
            return ''
        return entity_dict.get_entity_by_id(self.head_id).entity_desc

    @property
    def tail_desc(self):
        return entity_dict.get_entity_by_id(self.tail_id).entity_desc

    @property
    def head(self):
        if not self.head_id:
            return ''
        return entity_dict.get_entity_by_id(self.head_id).entity

    @property
    def tail(self):
        return entity_dict.get_entity_by_id(self.tail_id).entity

    def vectorize(self) -> dict:
        head_desc, tail_desc = self.head_desc, self.tail_desc
        if args.use_link_graph:
            if len(head_desc.split()) < 20:
                head_desc += ' ' + get_neighbor_desc(head_id=self.head_id, tail_id=self.tail_id)
            if len(tail_desc.split()) < 20:
                tail_desc += ' ' + get_neighbor_desc(head_id=self.tail_id, tail_id=self.head_id)

        head_word = _parse_entity_name(self.head)
        head_text = _concat_name_desc(head_word, head_desc)
        hr_encoded_inputs = _custom_tokenize(text=head_text,
                                             text_pair=self.relation)

        head_encoded_inputs = _custom_tokenize(text=head_text)

        tail_word = _parse_entity_name(self.tail)
        tail_encoded_inputs = _custom_tokenize(text=_concat_name_desc(tail_word, tail_desc))

        return {'hr_token_ids': hr_encoded_inputs['input_ids'],
                'hr_token_type_ids': hr_encoded_inputs['token_type_ids'],
                'tail_token_ids': tail_encoded_inputs['input_ids'],
                'tail_token_type_ids': tail_encoded_inputs['token_type_ids'],
                'head_token_ids': head_encoded_inputs['input_ids'],
                'head_token_type_ids': head_encoded_inputs['token_type_ids'],
                'obj': self}


class Dataset(torch.utils.data.dataset.Dataset):

    def __init__(self, path, task, examples=None):
        self.path_list = path.split(',')
        self.task = task
        assert all(os.path.exists(path) for path in self.path_list) or examples
        if examples:
            self.examples = examples
        else:
            self.examples = []
            for path in self.path_list:
                if not self.examples:
                    self.examples = load_data(path)
                else:
                    self.examples.extend(load_data(path))
        self.rels = []
        self.rel2ent_t = {}
        self.rel_ex_cnt = {}
        ##### delete afterwards #####
        self.rel_hent = {}
        self.ex_hid = {}
        self.idx2example = {}
        #############################
        for i, example in enumerate(self.examples):
            self.idx2example[i] = example
            self.ex_hid[i] = example.head_id
            self.rels.append(example.relation)
            if example.relation in self.rel2ent_t:
                self.rel2ent_t[example.relation][example.tail_id] = [i]
            else:
                self.rel2ent_t[example.relation] = {}
                self.rel2ent_t[example.relation][example.tail_id] = [i]
            if example.relation in self.rel_ex_cnt:
                self.rel_ex_cnt[example.relation] += 1
            else:
                self.rel_ex_cnt[example.relation] = 1
            #############################
            if example.relation in self.rel_hent:
                self.rel_hent[example.relation].append(example.head)
            else:
                self.rel_hent[example.relation] = [example.head]
            #############################                
        print([len(set(i)) for i in self.rel_hent.values()])

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, index):
        return self.examples[index].vectorize()
    
    def ds_info(self):
        return {
            "rels": copy(self.rels),
            "rel2ent_t": copy(self.rel2ent_t),
            "rel_ex_cnt": copy(self.rel_ex_cnt),
            "ex_cnt": len(self.examples),
            "ex_hid": self.ex_hid,
            "id2example": self.idx2example
        }


def load_data(path: str,
              add_forward_triplet: bool = True,
              add_backward_triplet: bool = True) -> List[Example]:
    assert path.endswith('.json'), 'Unsupported format: {}'.format(path)
    assert add_forward_triplet or add_backward_triplet
    logger.info('In test mode: {}'.format(args.is_test))

    data = json.load(open(path, 'r', encoding='utf-8'))
    logger.info('Load {} examples from {}'.format(len(data), path))

    cnt = len(data)
    examples = []
    for i in range(cnt):
        obj = data[i]
        if add_forward_triplet:
            examples.append(Example(**obj))
        if add_backward_triplet:
            examples.append(Example(**reverse_triplet(obj)))
        data[i] = None

    return examples


def collate(batch_data: List[dict]) -> dict:
    hr_token_ids, hr_mask = to_indices_and_mask(
        [torch.LongTensor(ex['hr_token_ids']) for ex in batch_data],
        pad_token_id=get_tokenizer().pad_token_id)
    hr_token_type_ids = to_indices_and_mask(
        [torch.LongTensor(ex['hr_token_type_ids']) for ex in batch_data],
        need_mask=False)

    tail_token_ids, tail_mask = to_indices_and_mask(
        [torch.LongTensor(ex['tail_token_ids']) for ex in batch_data],
        pad_token_id=get_tokenizer().pad_token_id)
    tail_token_type_ids = to_indices_and_mask(
        [torch.LongTensor(ex['tail_token_type_ids']) for ex in batch_data],
        need_mask=False)

    head_token_ids, head_mask = to_indices_and_mask(
        [torch.LongTensor(ex['head_token_ids']) for ex in batch_data],
        pad_token_id=get_tokenizer().pad_token_id)
    head_token_type_ids = to_indices_and_mask(
        [torch.LongTensor(ex['head_token_type_ids']) for ex in batch_data],
        need_mask=False)

    batch_exs = [ex['obj'] for ex in batch_data]
    batch_dict = {
        'hr_token_ids': hr_token_ids,
        'hr_mask': hr_mask,
        'hr_token_type_ids': hr_token_type_ids,
        'tail_token_ids': tail_token_ids,
        'tail_mask': tail_mask,
        'tail_token_type_ids': tail_token_type_ids,
        'head_token_ids': head_token_ids,
        'head_mask': head_mask,
        'head_token_type_ids': head_token_type_ids,
        'batch_data': batch_exs,
        'triplet_mask': construct_mask(row_exs=batch_exs) if not args.is_test else None,
        'self_negative_mask': construct_self_negative_mask(batch_exs) if not args.is_test else None,
    }
    return batch_dict

def rel_gen_collate(batch_data: List[dict]) -> dict:
    batch_data = batch_data[0]
    hr_token_ids, hr_mask = to_indices_and_mask(
        [torch.LongTensor(ex['hr_token_ids']) for ex in batch_data],
        pad_token_id=get_tokenizer().pad_token_id)
    hr_token_type_ids = to_indices_and_mask(
        [torch.LongTensor(ex['hr_token_type_ids']) for ex in batch_data],
        need_mask=False)

    tail_token_ids, tail_mask = to_indices_and_mask(
        [torch.LongTensor(ex['tail_token_ids']) for ex in batch_data],
        pad_token_id=get_tokenizer().pad_token_id)
    tail_token_type_ids = to_indices_and_mask(
        [torch.LongTensor(ex['tail_token_type_ids']) for ex in batch_data],
        need_mask=False)

    head_token_ids, head_mask = to_indices_and_mask(
        [torch.LongTensor(ex['head_token_ids']) for ex in batch_data],
        pad_token_id=get_tokenizer().pad_token_id)
    head_token_type_ids = to_indices_and_mask(
        [torch.LongTensor(ex['head_token_type_ids']) for ex in batch_data],
        need_mask=False)

    batch_exs = [ex['obj'] for ex in batch_data]
    batch_dict = {
        'hr_token_ids': hr_token_ids,
        'hr_mask': hr_mask,
        'hr_token_type_ids': hr_token_type_ids,
        'tail_token_ids': tail_token_ids,
        'tail_mask': tail_mask,
        'tail_token_type_ids': tail_token_type_ids,
        'head_token_ids': head_token_ids,
        'head_mask': head_mask,
        'head_token_type_ids': head_token_type_ids,
        'batch_data': batch_exs,
        'triplet_mask': construct_mask(row_exs=batch_exs) if not args.is_test else None,
        'self_negative_mask': construct_self_negative_mask(batch_exs) if not args.is_test else None,
    }

    return batch_dict

def to_indices_and_mask(batch_tensor, pad_token_id=0, need_mask=True):
    mx_len = max([t.size(0) for t in batch_tensor])
    batch_size = len(batch_tensor)
    indices = torch.LongTensor(batch_size, mx_len).fill_(pad_token_id)
    # For BERT, mask value of 1 corresponds to a valid position
    if need_mask:
        mask = torch.ByteTensor(batch_size, mx_len).fill_(0)
    for i, t in enumerate(batch_tensor):
        indices[i, :len(t)].copy_(t)
        if need_mask:
            mask[i, :len(t)].fill_(1)
    if need_mask:
        return indices, mask
    else:
        return indices
    
    
class RelationBatchSampler:   
        
    def __init__(self, batch_size, commonsense_path, cake_ratio, ds_info):
        self.commonsense_path = commonsense_path
        self.ent_dom = json.load(open(os.path.join(commonsense_path,"ent_dom.json"), 'r', encoding='utf-8'))
        self.dom_ent = json.load(open(os.path.join(commonsense_path,"dom_ent.json"), 'r', encoding='utf-8'))
        self.rel2dom_h = json.load(open(os.path.join(commonsense_path,"rel2dom_h.json"), 'r', encoding='utf-8'))
        self.rel2dom_t = json.load(open(os.path.join(commonsense_path,"rel2dom_t.json"), 'r', encoding='utf-8'))
        self.rel2nn = json.load(open(os.path.join(commonsense_path,"rel2nn.json"), 'r', encoding='utf-8'))
        self.batch_size = batch_size
        self.cake_ratio = cake_ratio   
            
        self.rels = ds_info["rels"] 
        self.rel2ent_t = ds_info["rel2ent_t"] 
        self.rel_ex_cnt = ds_info["rel_ex_cnt"]    
        self.length = len(self.rel_ex_cnt.keys())
        self.ex_cnt = ds_info["ex_cnt"] 
        
        self.ex_hid = ds_info["ex_hid"]
        self.id2example = ds_info["id2example"]
        
    def __iter__(self):
        for i in range(self.length):
            sampled_exs = []
            sampled_relations = []
            cake_sample_cnt = int(args.batch_size * args.cake_ratio)
            while len(sampled_exs) < cake_sample_cnt:
                sampled_relation = random.choices(
                    list(self.rel_ex_cnt.keys()),
                    list(self.rel_ex_cnt.values()),
                    k=1
                )[0]
                if sampled_relation not in sampled_relations:
                    temp = self.concept_filter(sampled_relation, self.batch_size//8)
                    sampled_exs += temp
                    head_id_list = [self.ex_hid[ex] for ex in temp]
                    if len(set(head_id_list)) > 20:
                        print("================================")
                        for i in temp:
                            example = self.id2example[i]
                            print(example.head, example.relation, example.tail)                            
                        print("================================")
                    sampled_exs = list(set(sampled_exs))
                    sampled_relations.append(sampled_relation)
            random_sample_cnt = args.batch_size - cake_sample_cnt
            sampled_exs += random.choices(range(self.ex_cnt), k=random_sample_cnt)
            
            yield sampled_exs[:self.batch_size]
            
    def __len__(self):
        return self.length
        
    def concept_filter(self, relation, example_size):
        # Relation_complexity given in CAKE
        # 0 : 1-1
        # 1 : 1-N
        # 2 : N-1
        # 3 : N-N    
        reverse_dict = {
        0 : 0,
        1 : 2,
        2 : 1,
        3 : 3
        }
        if relation[:8] == "inverse ":
            rel2nn = reverse_dict[self.rel2nn[relation[8:]]]
            relation_concepts = self.rel2dom_h[relation[8:]]
        else:
            rel2nn = self.rel2nn[relation]
            relation_concepts = self.rel2dom_t[relation]
        relation_ents = self.rel2ent_t[relation]
        rel_tail_ents = [ent for concept in relation_concepts for ent in self.dom_ent[str(concept)] if ent in relation_ents]
        rel_tail_exs = [ex for ent in rel_tail_ents for ex in relation_ents[ent]]
        rel_tail_exs = list(set(rel_tail_exs))
        corrupted_examples = random.choices(rel_tail_exs, k = example_size)
        return corrupted_examples
            